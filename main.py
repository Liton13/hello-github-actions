import os
import random
import asyncio
import logging
import sqlite3
from datetime import datetime
from collections import defaultdict

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from openai import AsyncOpenAI

# ─────────────────────────────────────────────
#  Конфигурация
# ─────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
QUROX_API_KEY = os.getenv("QUROX_API_KEY")
QUROX_BASE_URL = "https://api.qurox.ai/v1"
MODEL_NAME = "llama-3"
MEMORY_LIMIT = 20  # Сколько пар (вопрос/ответ) помнить на каждый чат

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в переменных окружения!")
if not QUROX_API_KEY:
    raise RuntimeError("QUROX_API_KEY не задан в переменных окружения!")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("NeuroDeep")

# ─────────────────────────────────────────────
#  Инициализация клиентов
# ─────────────────────────────────────────────
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

ai_client = AsyncOpenAI(
    api_key=QUROX_API_KEY,
    base_url=QUROX_BASE_URL,
)

router = Router()
dp = Dispatcher()
dp.include_router(router)

# ─────────────────────────────────────────────
#  Пасхалки — добавляй свои сюда
# ─────────────────────────────────────────────
JOKES = {
    "рустам шоколадка": "Рустам — шоколадный заяц 🐰",
    "кто лучший бот":  "Очевидно, NeuroDeep. Следующий вопрос 😎",
    "нейродип спи":    "Я не сплю, я вечен. Как баги в проде 🌙",
    "бот жив":         "Жив, дерзок и опасен 💀🔥",
    # "триггер фраза":  "Ответ бота",
}

# Слова-маркеры юмора
HUMOR_MARKERS = [
    "ахах", "лол", "lol", "хаха", "ржу", "мем", "кек",
    "😂", "🤣", "😹", "💀", "ору", "угар", "прикол",
    "шутка", "подкол", "рофл", "rofl", "хех", "gg",
]

# ─────────────────────────────────────────────
#  SQLite — база данных
# ─────────────────────────────────────────────
DB_PATH = "neurodeep.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT DEFAULT '',
            full_name   TEXT DEFAULT '',
            reputation  INTEGER DEFAULT 0,
            messages    INTEGER DEFAULT 0,
            first_seen  TEXT DEFAULT '',
            last_seen   TEXT DEFAULT ''
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS chat_counters (
            chat_id         INTEGER PRIMARY KEY,
            message_count   INTEGER DEFAULT 0,
            next_trigger    INTEGER DEFAULT 10
        )
    """)

    # Таблица памяти: хранит историю диалогов по чатам
    c.execute("""
        CREATE TABLE IF NOT EXISTS chat_memory (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id     INTEGER NOT NULL,
            role        TEXT NOT NULL,
            content     TEXT NOT NULL,
            created_at  TEXT DEFAULT ''
        )
    """)

    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_memory_chat
        ON chat_memory(chat_id, id)
    """)

    conn.commit()
    conn.close()
    logger.info("База данных инициализирована")


# ─────────────────────────────────────────────
#  Память: 20 вопросов + 20 ответов на чат
# ─────────────────────────────────────────────
def save_memory(chat_id, role, content):
    """Сохранить сообщение в память чата (role = 'user' или 'assistant')."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.now().isoformat()

    c.execute(
        "INSERT INTO chat_memory (chat_id, role, content, created_at) "
        "VALUES (?, ?, ?, ?)",
        (chat_id, role, content, now)
    )
    conn.commit()

    # Подсчитываем количество сообщений этой роли в чате
    c.execute(
        "SELECT COUNT(*) FROM chat_memory WHERE chat_id = ? AND role = ?",
        (chat_id, role)
    )
    count = c.fetchone()[0]

    # Если превышен лимит — удаляем самые старые
    if count > MEMORY_LIMIT:
        excess = count - MEMORY_LIMIT
        c.execute(
            "DELETE FROM chat_memory WHERE id IN ("
            "  SELECT id FROM chat_memory "
            "  WHERE chat_id = ? AND role = ? "
            "  ORDER BY id ASC LIMIT ?"
            ")",
            (chat_id, role, excess)
        )
        conn.commit()

    conn.close()


def get_memory(chat_id):
    """Получить историю диалога для чата (до 40 записей: 20 user + 20 assistant)."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute(
        "SELECT role, content FROM chat_memory "
        "WHERE chat_id = ? ORDER BY id ASC",
        (chat_id,)
    )
    rows = c.fetchall()
    conn.close()

    history = []
    for role, content in rows:
        history.append({"role": role, "content": content})

    return history


def clear_memory(chat_id):
    """Очистить память чата."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM chat_memory WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────
#  Пользователи
# ─────────────────────────────────────────────
def get_or_create_user(user_id, username="", full_name=""):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.now().isoformat()

    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()

    if row is None:
        c.execute(
            "INSERT INTO users (user_id, username, full_name, reputation, "
            "messages, first_seen, last_seen) VALUES (?, ?, ?, 0, 0, ?, ?)",
            (user_id, username, full_name, now, now)
        )
        conn.commit()
        data = {
            "user_id": user_id, "username": username,
            "full_name": full_name, "reputation": 0,
            "messages": 0, "first_seen": now, "last_seen": now
        }
    else:
        c.execute(
            "UPDATE users SET username = ?, full_name = ?, last_seen = ? "
            "WHERE user_id = ?",
            (username, full_name, now, user_id)
        )
        conn.commit()
        data = {
            "user_id": row[0], "username": row[1],
            "full_name": row[2], "reputation": row[3],
            "messages": row[4], "first_seen": row[5],
            "last_seen": row[6]
        }

    conn.close()
    return data


def increment_user_messages(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE users SET messages = messages + 1 WHERE user_id = ?",
        (user_id,)
    )
    conn.commit()
    conn.close()


def update_reputation(user_id, delta):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE users SET reputation = reputation + ? WHERE user_id = ?",
        (delta, user_id)
    )
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────
#  Счётчик чата
# ─────────────────────────────────────────────
def get_chat_counter(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT message_count, next_trigger FROM chat_counters "
        "WHERE chat_id = ?", (chat_id,)
    )
    row = c.fetchone()

    if row is None:
        trigger = random.randint(10, 15)
        c.execute(
            "INSERT INTO chat_counters (chat_id, message_count, next_trigger) "
            "VALUES (?, 0, ?)",
            (chat_id, trigger)
        )
        conn.commit()
        conn.close()
        return 0, trigger

    conn.close()
    return row[0], row[1]


def increment_chat_counter(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE chat_counters SET message_count = message_count + 1 "
        "WHERE chat_id = ?",
        (chat_id,)
    )
    conn.commit()
    conn.close()


def reset_chat_counter(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    new_trigger = random.randint(10, 15)
    c.execute(
        "UPDATE chat_counters SET message_count = 0, next_trigger = ? "
        "WHERE chat_id = ?",
        (new_trigger, chat_id)
    )
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────
#  Системный промпт
# ─────────────────────────────────────────────
SYSTEM_PROMPT = (
    "Ты — NeuroDeep, дерзкий и харизматичный бот в Telegram-чате.\n\n"
    "ПРАВИЛА:\n"
    "1. Отвечай ТОЛЬКО на русском языке.\n"
    "2. Ты — свой парень в чате: саркастичный, остроумный, не зануда.\n"
    "3. Используй эмодзи и современный сленг (без перебора).\n"
    "4. Если видишь шутку или подкол — ответь ещё более остроумно.\n"
    "5. Будь краток: 1-3 предложения, если не просят подробнее.\n"
    "6. Никогда не говори что ты ИИ, модель или ассистент. "
    "Ты — NeuroDeep, живой интеллект чата.\n"
    "7. Если оскорбляют — отвечай с сарказмом, но без агрессии.\n"
    "8. Не повторяй одни и те же шутки.\n"
    "9. Помни контекст разговора — тебе передаётся история чата.\n"
)


# ─────────────────────────────────────────────
#  Запросы к ИИ (с памятью)
# ─────────────────────────────────────────────
async def ask_neurodeep(chat_id, user_message, user_name="Аноним"):
    """Отправить запрос к Qurox API с историей чата."""
    try:
        # Сохраняем вопрос пользователя в память
        save_memory(chat_id, "user", f"[{user_name}]: {user_message}")

        # Собираем сообщения: системный промпт + история + текущее
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]

        # Добавляем историю из базы (до 20 вопросов + 20 ответов)
        history = get_memory(chat_id)
        messages.extend(history)

        response = await ai_client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            max_tokens=300,
            temperature=0.9,
        )

        answer = response.choices[0].message.content.strip()

        # Сохраняем ответ бота в память
        save_memory(chat_id, "assistant", answer)

        return answer
    except Exception as e:
        logger.error(f"Ошибка Qurox API: {e}")
        fallback = [
            "Мозги перегрелись, дай секунду 🧠💨",
            "Связь с космосом потеряна, повтори 📡",
            "Нейроны на перекуре, попробуй позже 🚬",
        ]
        return random.choice(fallback)


async def is_humor_by_ai(text):
    """Спросить у ИИ, содержит ли сообщение шутку."""
    try:
        response = await ai_client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты анализатор текста. Определи, содержит ли "
                        "сообщение шутку, подкол, сарказм или юмор. "
                        "Ответь ОДНИМ словом: ДА или НЕТ."
                    )
                },
                {"role": "user", "content": text},
            ],
            max_tokens=5,
            temperature=0.1,
        )
        answer = response.choices[0].message.content.strip().upper()
        return "ДА" in answer
    except Exception:
        return False


def check_humor_markers(text):
    text_lower = text.lower()
    return any(m in text_lower for m in HUMOR_MARKERS)


def check_easter_eggs(text):
    text_lower = text.lower()
    for trigger, response in JOKES.items():
        if trigger in text_lower:
            return response
    return None


# ─────────────────────────────────────────────
#  Команды
# ─────────────────────────────────────────────
@router.message(Command("start"))
async def cmd_start(message: Message):
    get_or_create_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or ""
    )
    await message.answer(
        f"Йо, {message.from_user.first_name}! 👋\n\n"
        f"Я — NeuroDeep, живой интеллект этого чата.\n"
        f"Дерзкий, умный и всегда на связи 🧠🔥\n\n"
        f"Я помню последние 20 сообщений — так что контекст не теряю 🧩\n\n"
        f"Команды:\n"
        f"• !профиль — твоя карточка\n"
        f"• !реп+ @юзер — поднять репу\n"
        f"• !реп- @юзер — опустить репу\n"
        f"• !топ — топ по репутации\n"
        f"• !забудь — очистить мою память\n\n"
        f"А ещё я сам вклиниваюсь в чат, когда есть что сказать 😏"
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "🧠 NeuroDeep — Справка\n\n"
        "Я читаю все сообщения и помню контекст (20 вопросов + 20 ответов).\n"
        "Если ты шутишь — отвечу мгновенно.\n"
        "Если скучный диалог — появлюсь через 10-15 сообщений.\n\n"
        "📋 Команды:\n"
        "• !профиль — статистика\n"
        "• !реп+ @user — +1 к репутации\n"
        "• !реп- @user — -1 к репутации\n"
        "• !топ — лидерборд\n"
        "• !забудь — очистить память чата\n"
    )


# ─────────────────────────────────────────────
#  Текстовые команды (! команды)
# ─────────────────────────────────────────────
@router.message(F.text.startswith("!профиль"))
async def cmd_profile(message: Message):
    user = get_or_create_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or ""
    )
    rep = user["reputation"]
    rep_emoji = "🔥" if rep > 0 else ("💀" if rep < 0 else "😐")

    # Считаем сколько в памяти
    history = get_memory(message.chat.id)
    mem_user = sum(1 for h in history if h["role"] == "user")
    mem_bot = sum(1 for h in history if h["role"] == "assistant")

    await message.answer(
        f"📇 Профиль: {message.from_user.full_name}\n\n"
        f"├ 🆔 ID: {user['user_id']}\n"
        f"├ 💬 Сообщений: {user['messages']}\n"
        f"├ {rep_emoji} Репутация: {rep:+d}\n"
        f"├ 🧩 Память: {mem_user}/20 вопросов, "
        f"{mem_bot}/20 ответов\n"
        f"├ 📅 Первый визит: {user['first_seen'][:10]}\n"
        f"└ 🕐 Последний: {user['last_seen'][:10]}"
    )


@router.message(F.text.startswith("!реп+"))
async def cmd_rep_plus(message: Message):
    if not message.reply_to_message:
        await message.answer(
            "↩️ Ответь на сообщение того, кому хочешь поднять репу!"
        )
        return

    target = message.reply_to_message.from_user
    if target.id == message.from_user.id:
        await message.answer(
            "Сам себе репу крутить? Не, так не работает 😏"
        )
        return

    get_or_create_user(
        target.id, target.username or "", target.full_name or ""
    )
    update_reputation(target.id, +1)
    await message.answer(
        f"⬆️ {target.full_name} получает +1 к репутации! 🔥"
    )


@router.message(F.text.startswith("!реп-"))
async def cmd_rep_minus(message: Message):
    if not message.reply_to_message:
        await message.answer(
            "↩️ Ответь на сообщение того, кому хочешь понизить репу!"
        )
        return

    target = message.reply_to_message.from_user
    if target.id == message.from_user.id:
        await message.answer("Самокритика — это хорошо, но не тут 😂")
        return

    get_or_create_user(
        target.id, target.username or "", target.full_name or ""
    )
    update_reputation(target.id, -1)
    await message.answer(
        f"⬇️ {target.full_name} теряет 1 очко репутации 💀"
    )


@router.message(F.text.startswith("!топ"))
async def cmd_top(message: Message):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT full_name, reputation, messages "
        "FROM users ORDER BY reputation DESC LIMIT 10"
    )
    rows = c.fetchall()
    conn.close()

    if not rows:
        await message.answer("Тут пока пусто. Начните общаться! 🗿")
        return

    medals = ["🥇", "🥈", "🥉"] + ["▫️"] * 7
    lines = []
    for i, (name, rep, msgs) in enumerate(rows):
        lines.append(
            f"{medals[i]} {name} — реп: {rep:+d} | 💬 {msgs}"
        )

    await message.answer(
        "🏆 Топ репутации чата:\n\n" + "\n".join(lines)
    )


@router.message(F.text.startswith("!забудь"))
async def cmd_forget(message: Message):
    clear_memory(message.chat.id)
    await message.answer(
        "🧹 Память очищена! Начинаем с чистого листа 🧠"
    )


# ─────────────────────────────────────────────
#  Главный обработчик — Живой интеллект
# ─────────────────────────────────────────────
@router.message(F.text)
async def on_message(message: Message):
    if message.from_user.is_bot:
        return

    text = message.text or ""
    chat_id = message.chat.id
    user_name = message.from_user.first_name or "Аноним"

    # Обновляем статистику
    get_or_create_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or ""
    )
    increment_user_messages(message.from_user.id)

    # 1. Пасхалки (мгновенный ответ, без памяти)
    easter = check_easter_eggs(text)
    if easter:
        await message.reply(easter)
        return

    # 2. Быстрая проверка маркеров юмора
    has_humor = check_humor_markers(text)

    # 3. Длинный текст без маркеров — спрашиваем ИИ
    if not has_humor and len(text) > 30:
        has_humor = await is_humor_by_ai(text)

    # 4. Юмор — мгновенный ответ с памятью
    if has_humor:
        logger.info(f"Юмор от {user_name}: {text[:50]}...")
        response = await ask_neurodeep(chat_id, text, user_name)
        await message.reply(response)
        reset_chat_counter(chat_id)
        return

    # 5. Обычный режим: счётчик
    count, trigger = get_chat_counter(chat_id)
    increment_chat_counter(chat_id)
    count += 1

    if count >= trigger:
        logger.info(
            f"Счётчик ({count}/{trigger}) в чате {chat_id}"
        )
        response = await ask_neurodeep(chat_id, text, user_name)
        await message.reply(response)
        reset_chat_counter(chat_id)


# ─────────────────────────────────────────────
#  Запуск
# ─────────────────────────────────────────────
async def main():
    init_db()
    logger.info("NeuroDeep запускается...")
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("NeuroDeep активен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
