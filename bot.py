import os
import re
import json
import logging
import httpx
import html
from datetime import date, time
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from notion_client import Client
import anthropic

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '.env'), override=True)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "3141fdf17934804ba372c57d0dabe7ae")
DAILY_CHECK_HOUR = int(os.getenv("DAILY_CHECK_HOUR", 6))
DAILY_CHECK_MINUTE = int(os.getenv("DAILY_CHECK_MINUTE", 0))

# Файл для хранения chat_id и уже отправленных уведомлений
STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

notion = Client(auth=NOTION_TOKEN)
claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# ─── State helpers ────────────────────────────────────────────────────────────

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"chat_ids": [], "notified_ids": []}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def register_chat(chat_id: int):
    state = load_state()
    if chat_id not in state["chat_ids"]:
        state["chat_ids"].append(chat_id)
        save_state(state)


# ─── Telegram link helpers ─────────────────────────────────────────────────────

def get_tg_message_link(origin) -> str | None:
    if origin is None:
        return None
    chat = getattr(origin, "chat", None)
    message_id = getattr(origin, "message_id", None)
    if not chat or not message_id:
        return None
    username = getattr(chat, "username", None)
    if username:
        return f"https://t.me/{username}/{message_id}"
    chat_id = str(getattr(chat, "id", ""))
    if chat_id.startswith("-100"):
        return f"https://t.me/c/{chat_id[4:]}/{message_id}"
    return None


# ─── URL fetcher ──────────────────────────────────────────────────────────────

def fetch_job_page(url: str) -> str:
    """Загружает страницу вакансии и возвращает очищенный текст."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
    except Exception as e:
        logger.warning(f"fetch_job_page failed for {url}: {e}")
        return ""

    soup = BeautifulSoup(resp.text, "lxml")

    # 1. JSON-LD структурированные данные (LinkedIn, Glassdoor, hh.ru и др.)
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, dict) and data.get("@type") == "JobPosting":
                parts = []
                if data.get("title"):
                    parts.append(f"Позиция: {data['title']}")
                org = data.get("hiringOrganization", {})
                if isinstance(org, dict) and org.get("name"):
                    parts.append(f"Компания: {org['name']}")
                loc = data.get("jobLocation", {})
                if isinstance(loc, dict):
                    addr = loc.get("address", {})
                    if isinstance(addr, dict):
                        city = addr.get("addressLocality", "")
                        country = addr.get("addressCountry", "")
                        if city or country:
                            parts.append(f"Локация: {', '.join(filter(None, [city, country]))}")
                if data.get("employmentType"):
                    parts.append(f"Тип: {data['employmentType']}")
                if data.get("description"):
                    desc_text = BeautifulSoup(data["description"], "lxml").get_text(
                        separator="\n", strip=True
                    )
                    parts.append(f"\n{desc_text}")
                if parts:
                    return "\n".join(parts)
        except Exception:
            continue

    # 2. LinkedIn-специфичные селекторы
    if "linkedin.com" in url:
        for sel in [
            ".description__text",
            ".job-description",
            ".jobs-description__content",
            "[class*='description']",
        ]:
            el = soup.select_one(sel)
            if el:
                return el.get_text(separator="\n", strip=True)

    # 3. Универсальные селекторы
    for sel in ["main", "article", "[class*='job']", "[class*='vacancy']", ".content"]:
        el = soup.select_one(sel)
        if el:
            t = el.get_text(separator="\n", strip=True)
            if len(t) > 300:
                return t[:8000]

    return ""


def text_to_blocks(text: str) -> list:
    """Конвертирует текст в Notion paragraph blocks (макс. 2000 символов каждый)."""
    blocks = []
    lines = text.split("\n")
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 1990:
            if chunk.strip():
                blocks.append({
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": chunk.strip()}}]
                    },
                })
            chunk = line
        else:
            chunk = (chunk + "\n" + line) if chunk else line
    if chunk.strip():
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": chunk.strip()}}]
            },
        })
    return blocks[:99]  # Notion API: максимум 100 блоков за запрос


# ─── Parsing & Notion ─────────────────────────────────────────────────────────

def parse_vacancy(text: str) -> dict:
    url_match = re.search(r'https?://[^\s\)\]\>\,]+', text)
    url = url_match.group(0) if url_match else ""

    response = claude.messages.create(
        model="claude-haiku-4-5",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"""Извлеки из текста вакансии следующие поля и верни ТОЛЬКО JSON без пояснений:
- company: название компании (строка, только название без лишних слов)
- position: должность/позиция (строка)

Если поле не найдено — верни пустую строку "".

Текст вакансии:
{text}

Ответ строго в формате JSON:
{{"company": "...", "position": "..."}}"""
        }]
    )

    raw = response.content[0].text.strip()
    json_match = re.search(r'\{.*?\}', raw, re.DOTALL)
    if json_match:
        data = json.loads(json_match.group(0))
    else:
        data = {"company": "", "position": ""}

    return {
        "company": (data.get("company") or "Не указано")[:100],
        "position": (data.get("position") or "")[:500],
        "url": url,
        "comment": text.strip()[:2000],
    }


def add_to_notion(
    company: str, position: str, url: str, comment: str, description: str = ""
) -> tuple[str, str]:
    """Возвращает (notion_url, page_id). description сохраняется в тело страницы."""
    properties = {
        "Компания": {"title": [{"text": {"content": company}}]},
        "Статус": {"select": {"name": "Откликнуться"}},
        "Дата отклика": {"date": {"start": date.today().isoformat()}},
    }
    if position:
        properties["Позиция"] = {"rich_text": [{"text": {"content": position}}]}
    if url:
        properties["Ссылка на вакансию"] = {"url": url}
    if comment:
        properties["Комментарий"] = {"rich_text": [{"text": {"content": comment[:2000]}}]}

    children = text_to_blocks(description) if description else []

    response = notion.pages.create(
        parent={"database_id": NOTION_DATABASE_ID},
        properties=properties,
        children=children,
    )
    return response["url"], response["id"]


def update_notion_status(page_id: str, status: str):
    notion.pages.update(
        page_id=page_id,
        properties={"Статус": {"select": {"name": status}}}
    )


def _rich_text_content(rich_text_list: list) -> str:
    """Надёжно извлекает текст из Notion rich text массива."""
    if not rich_text_list:
        return ""
    item = rich_text_list[0]
    return item.get("plain_text") or item.get("text", {}).get("content", "")


def get_pending_vacancies() -> list[dict]:
    """Возвращает вакансии со статусом 'Откликнуться'."""
    results = notion.databases.query(
        database_id=NOTION_DATABASE_ID,
        filter={"property": "Статус", "select": {"equals": "Откликнуться"}}
    )
    vacancies = []
    for page in results.get("results", []):
        props = page["properties"]
        company = _rich_text_content(props.get("Компания", {}).get("title", []))
        position = _rich_text_content(props.get("Позиция", {}).get("rich_text", []))
        url = props.get("Ссылка на вакансию", {}).get("url", "") or ""

        vacancies.append({
            "id": page["id"],
            "notion_url": page["url"],
            "company": company or "Без названия",
            "position": position,
            "url": url,
        })
    return vacancies


# ─── Cowork prompt builder ─────────────────────────────────────────────────────

def build_cowork_prompt(company: str, position: str, url: str, notion_url: str) -> str:
    vacancy_ref = url or notion_url
    return (
        f"Я хочу откликнуться на вакансию.\n\n"
        f"Компания: {company}\n"
        f"Позиция: {position}\n"
        f"Ссылка: {vacancy_ref}\n\n"
        f"Мои навыки и профиль — в Notion: https://www.notion.so/Master-Profile-Form-Fill-32c1fdf179348195afe3c060db133b35?source=copy_link\n\n"
        f"Пожалуйста:\n"
        f"1. Прочитай вакансию\n"
        f"2. Сделай review моего CV под эту вакансию и предложи изменения\n"
        f"3. Напиши Cover Letter\n"
        f"4. Укажи, какие пункты требований совпадают с моим опытом, а какие — нет"
    )


# ─── Daily check job ───────────────────────────────────────────────────────────

def build_vacancy_list_message(vacancies: list[dict]) -> tuple[str, InlineKeyboardMarkup]:
    """Компактный список вакансий — одна кнопка на каждую."""
    text = f"📋 <b>Вакансии для отклика</b> — {len(vacancies)} шт.\n\nВыбери вакансию чтобы раскрыть детали:"
    buttons = []
    for v in vacancies:
        label = f"🏢 {v['company']}  •  {v['position'] or '—'}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"open:{v['id']}")])
    return text, InlineKeyboardMarkup(buttons)


async def daily_check(context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    chat_ids = state.get("chat_ids", [])
    if not chat_ids:
        logger.info("No registered chats, skipping daily check.")
        return

    try:
        vacancies = get_pending_vacancies()
    except Exception as e:
        logger.error(f"Error fetching Notion vacancies: {e}")
        return

    notified = set(state.get("notified_ids", []))
    new_vacancies = [v for v in vacancies if v["id"] not in notified]

    if not new_vacancies:
        logger.info("Daily check: no new vacancies.")
        return

    text, keyboard = build_vacancy_list_message(new_vacancies)
    for chat_id in chat_ids:
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard
        )

    for v in new_vacancies:
        notified.add(v["id"])
    state["notified_ids"] = list(notified)
    save_state(state)
    logger.info(f"Daily check: notified {len(new_vacancies)} vacancies.")


# ─── Callback handlers ─────────────────────────────────────────────────────────

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, page_id = query.data.split(":", 1)

    if action == "open":
        try:
            page = notion.pages.retrieve(page_id=page_id)
            props = page["properties"]

            company = _rich_text_content(props.get("Компания", {}).get("title", [])) or "Без названия"
            position = _rich_text_content(props.get("Позиция", {}).get("rich_text", []))
            url = props.get("Ссылка на вакансию", {}).get("url", "") or ""
            notion_url = page["url"]

            display_url = url or notion_url
            text = (
                f"🏢 <b>{html.escape(company)}</b>\n"
                f"💼 {html.escape(position or '—')}\n"
                f"🔗 <a href=\"{html.escape(display_url)}\">{html.escape(display_url)}</a>"
            )
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("▶️ Промпт для Cowork", callback_data=f"cowork:{page_id}"),
                    InlineKeyboardButton("✅ Отправлено", callback_data=f"done:{page_id}"),
                ],
                [InlineKeyboardButton("⏭ Пропустить", callback_data=f"skip:{page_id}")]
            ])
            await query.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)
        except Exception as e:
            await query.message.reply_text(f"Ошибка: {e}")

    elif action == "cowork":
        try:
            page = notion.pages.retrieve(page_id=page_id)
            props = page["properties"]

            company = _rich_text_content(props.get("Компания", {}).get("title", [])) or "Без названия"
            position = _rich_text_content(props.get("Позиция", {}).get("rich_text", []))
            url = props.get("Ссылка на вакансию", {}).get("url", "") or ""
            notion_url = page["url"]

            prompt = build_cowork_prompt(company, position, url, notion_url)
            await query.message.reply_text(
                f"📝 <b>Промпт для Claude Cowork:</b>\n\n<pre>{html.escape(prompt)}</pre>",
                parse_mode="HTML"
            )
        except Exception as e:
            await query.message.reply_text(f"Ошибка: {e}")

    elif action == "done":
        try:
            update_notion_status(page_id, "Отклик отправлен")
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("✅ Статус обновлён: <b>Отклик отправлен</b>", parse_mode="HTML")
        except Exception as e:
            await query.message.reply_text(f"Ошибка обновления статуса: {e}")

    elif action == "skip":
        try:
            update_notion_status(page_id, "Отменено")
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("⏭ Статус обновлён: <b>Отменено</b>", parse_mode="HTML")
        except Exception as e:
            await query.message.reply_text(f"Ошибка обновления статуса: {e}")


# ─── Command handlers ──────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_chat(update.effective_chat.id)
    await update.message.reply_text(
        "Привет! Пересылай мне сообщения с вакансиями — добавлю в Notion со статусом «Откликнуться».\n\n"
        "Каждый день в 10:00 (Тбилиси) пришлю список вакансий для отклика с кнопками.\n\n"
        "Команды:\n"
        "/check — проверить вакансии прямо сейчас\n"
        "/start — зарегистрироваться"
    )


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_chat(update.effective_chat.id)
    try:
        vacancies = get_pending_vacancies()
    except Exception as e:
        await update.message.reply_text(f"Ошибка при запросе Notion: {e}")
        return

    if not vacancies:
        await update.message.reply_text("✅ Нет вакансий со статусом «Откликнуться».")
        return

    text, keyboard = build_vacancy_list_message(vacancies)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


# ─── Message handler ───────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return

    register_chat(update.effective_chat.id)
    text = message.text or message.caption or ""

    entity_urls = []
    entities = message.entities or message.caption_entities or []
    for entity in entities:
        if entity.type == "url":
            entity_urls.append(text[entity.offset: entity.offset + entity.length])
        elif entity.type == "text_link" and entity.url:
            entity_urls.append(entity.url)

    tg_link = None
    source_name = None
    if message.forward_origin:
        origin = message.forward_origin
        tg_link = get_tg_message_link(origin)
        if hasattr(origin, "chat") and origin.chat:
            source_name = getattr(origin.chat, "title", None) or getattr(origin.chat, "username", None)
        elif hasattr(origin, "sender_user") and origin.sender_user:
            source_name = origin.sender_user.full_name or origin.sender_user.username
        elif hasattr(origin, "sender_user_name") and origin.sender_user_name:
            source_name = origin.sender_user_name

    if not text.strip():
        await message.reply_text("Не могу прочитать сообщение. Пришли текст или ссылку на вакансию.")
        return

    # Определяем URL из текста или entities
    raw_url = (entity_urls[0] if entity_urls else None) or (
        (lambda m: m.group(0) if m else None)(re.search(r'https?://[^\s\)\]\>\,]+', text))
    )

    # Если сообщение — только ссылка, загружаем страницу вакансии
    fetched_description = ""
    content_for_parsing = text
    is_url_only = bool(raw_url) and len(text.strip().split()) <= 3

    status_msg = None
    if is_url_only and raw_url:
        status_msg = await message.reply_text("⏳ Загружаю вакансию по ссылке…")
        fetched_description = fetch_job_page(raw_url)
        if fetched_description:
            content_for_parsing = fetched_description
            await status_msg.edit_text("⏳ Распознаю детали…")
        else:
            await status_msg.edit_text("⚠️ Не удалось загрузить страницу, пробую разобрать URL…")

    try:
        data = parse_vacancy(content_for_parsing)

        if data["company"] == "Не указано" and not data["position"]:
            reply_text = (
                "Не удалось распознать вакансию — не нашёл ни компанию, ни позицию.\n"
                "Попробуй переслать сообщение с более подробным описанием."
            )
            if status_msg:
                await status_msg.edit_text(reply_text)
            else:
                await message.reply_text(reply_text)
            return

        final_url = tg_link or raw_url or data["url"]

        comment = data["comment"]
        if source_name:
            comment = f"Источник: {source_name}\n\n{comment}"

        notion_url, page_id = add_to_notion(
            company=data["company"],
            position=data["position"],
            url=final_url,
            comment=comment[:2000],
            description=fetched_description,
        )

        # Обновляем notified_ids чтобы не дублировать в daily check
        state = load_state()
        if page_id not in state["notified_ids"]:
            state["notified_ids"].append(page_id)
            save_state(state)

        desc_note = " + полное описание" if fetched_description else ""
        reply = (
            f"✅ Добавлено в Notion{html.escape(desc_note)}!\n\n"
            f"🏢 Компания: {html.escape(data['company'])}\n"
            f"💼 Позиция: {html.escape(data['position'] or '—')}\n"
            f"🔗 Ссылка: {html.escape(final_url or '—')}\n"
            f"📊 Статус: Откликнуться\n\n"
            f"<a href=\"{html.escape(notion_url)}\">Открыть в Notion</a>"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("▶️ Промпт для Cowork", callback_data=f"cowork:{page_id}"),
                InlineKeyboardButton("✅ Отправлено", callback_data=f"done:{page_id}"),
            ],
            [InlineKeyboardButton("⏭ Пропустить", callback_data=f"skip:{page_id}")]
        ])
        if status_msg:
            await status_msg.edit_text(reply, parse_mode="HTML", reply_markup=keyboard)
        else:
            await message.reply_text(reply, parse_mode="HTML", reply_markup=keyboard)

    except Exception as e:
        logger.error(f"Error: {e}")
        await message.reply_text(f"Ошибка при добавлении в Notion: {e}")


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, handle_message))

    # Ежедневная проверка
    app.job_queue.run_daily(
        daily_check,
        time=time(hour=DAILY_CHECK_HOUR, minute=DAILY_CHECK_MINUTE),
        name="daily_notion_check"
    )

    logger.info(f"Bot started. Daily check at {DAILY_CHECK_HOUR:02d}:{DAILY_CHECK_MINUTE:02d} UTC")
    app.run_polling()


if __name__ == "__main__":
    main()
