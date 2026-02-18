import os
import re
import random
import asyncio
import logging
import httpx
import psycopg2
from datetime import datetime
from threading import Thread

from aiohttp import web
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties

# ─────────────────────────────────────────────
#  Конфигурация
# ─────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
QUROX_API_KEY = os.getenv("QUROX_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")  # Neon PostgreSQL
QUROX_BASE_URL = "https://api.qurox.ai/v1"
MODEL_NAME = "llama-3"
MEMORY_LIMIT = 20

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан!")
if not QUROX_API_KEY:
    raise RuntimeError("QUROX_API_KEY не задан!")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL не задан! Создай бесплатную БД на neon.tech")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("NeuroDeep")

# ─────────────────────────────────────────────
#  Keep-Alive сервер (чтобы Replit не засыпал)
# ─────────────────────────────────────────────
async def handle_ping(request):
    return web.Response(text="NeuroDeep is alive! 🧠🔥")

def run_keepalive():
    """Запускает HTTP-сервер на порту 8080 для UptimeRobot."""
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    runner = web.AppRunner(app)
    loop = asyncio.new_event_loop()
    loop.run_until_complete(runner.setup())
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    loop.run_until_complete(site.start())
    logger.info(f"Keep-alive сервер запущен на порту {port}")
    loop.run_forever()

# ─────────────────────────────────────────────
#  Инициализация клиентов
# ─────────────────────────────────────────────
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

router = Router()
dp = Dispatcher()
dp.include_router(router)

BOT_INFO = None  # будет заполнено при старте

# ─────────────────────────────────────────────
#  Qurox API — прямые запросы через httpx
# ─────────────────────────────────────────────
async def qurox_chat(messages, max_tokens=300, temperature=0.9):
    """Отправляет запрос к Qurox API напрямую через httpx."""
    url = f"{QUROX_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {QUROX_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"].strip()

# ─────────────────────────────────────────────
#  PostgreSQL — подключение к Neon
# ─────────────────────────────────────────────
def get_db():
    """Получить соединение к Neon PostgreSQL."""
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def init_db():
    """Создать таблицы при первом запуске."""
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     BIGINT PRIMARY KEY,
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
            chat_id         BIGINT PRIMARY KEY,
            message_count   INTEGER DEFAULT 0,
            next_trigger    INTEGER DEFAULT 10
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS chat_memory (
            id          SERIAL PRIMARY KEY,
            chat_id     BIGINT NOT NULL,
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
    logger.info("PostgreSQL (Neon) — таблицы готовы")


# ─────────────────────────────────────────────
#  Пасхалки — добавляй свои сюда
# ─────────────────────────────────────────────
JOKES = {
    "рустам шоколадка": "Рустам — шоколадный заяц 🐰",
    "кто лучший бот":  "Очевидно, NeuroDeep. Следующий вопрос 😎",
    "нейродип спи":    "Я не сплю, я вечен. Как баги в проде 🌙",
    "бот жив":         "Жив, дерзок и опасен 💀🔥",
}

HUMOR_MARKERS = [
    "ахах", "лол", "lol", "хаха", "ржу", "мем", "кек",
    "😂", "🤣", "😹", "💀", "ору", "угар", "прикол",
    "шутка", "подкол", "рофл", "rofl", "хех", "gg",
]

# ─────────────────────────────────────────────
#  🎲 Весёлые рандомные команды
# ─────────────────────────────────────────────
FUN_COMMANDS = {
    "кто дурачок": {
        "pick": 1,
        "templates": [
            "🤡 Главный дурачок чата — {0}! Поздравляю! 🎉",
            "🧠➡️🗑 Ну тут без вариантов — {0} 😂",
            "💀 Официально: {0} — дурачок дня!",
            "🎪 Барабанная дробь... 🥁 {0}! Сюрприз! 😏",
        ]
    },
    "кто кого любит": {
        "pick": 2,
        "templates": [
            "💕 {0} тайно влюблён(а) в {1}! Шок! 😱",
            "❤️‍🔥 {0} + {1} = ❤️ Я всё вижу! 👀",
            "💘 Стрела Купидона: {0} → {1}! Сладкая парочка 🥰",
            "🔥 {0} и {1} — это мэтч! Свадьба когда? 💒",
        ]
    },
    "кто самый умный": {
        "pick": 1,
        "templates": [
            "🧠 Гений чата — {0}! Аплодисменты! 👏",
            "🎓 {0} — IQ зашкаливает! Или нет... 😏",
            "💡 Самый умный тут — {0}. Остальные не обижайтесь 😂",
        ]
    },
    "кто красавчик": {
        "pick": 1,
        "templates": [
            "😍 Красавчик дня — {0}! Зеркало подтверждает 🪞",
            "🔥 {0} — огонь! Модельное агентство уже звонит 📞",
            "✨ {0} сегодня неотразим(а)! Факт! 💅",
        ]
    },
    "кто кому должен": {
        "pick": 2,
        "templates": [
            "💸 {0} должен {1} массу денег! Верни! 😤",
            "🏦 {0} задолжал(а) {1}. Проценты капают! 📈",
            "💰 Долг {0} перед {1} — это уже легенда чата 😂",
        ]
    },
    "кто тут босс": {
        "pick": 1,
        "templates": [
            "👑 Босс этого чата — {0}! Все поклонитесь! 🫡",
            "🦁 {0} — альфа чата. Без вопросов! 💪",
            "🏆 Тут правит {0}. Остальные — подчинённые 😏",
        ]
    },
    "кто врёт": {
        "pick": 1,
        "templates": [
            "🤥 Главный врун — {0}! Нос уже как у Пиноккио 👃",
            "🧢 {0} — кэпчик детектед! Не верьте ни слову 😂",
            "🔍 Детектор лжи показывает на {0}! Запалился! 💀",
        ]
    },
    "кто будет миллионером": {
        "pick": 1,
        "templates": [
            "💰 Будущий миллионер — {0}! Уже можно просить в долг 😏",
            "🤑 {0} разбогатеет! Запомните это имя! 📝",
            "💎 {0} — будущий олигарх чата! 🏦",
        ]
    },
    "кто пару": {
        "pick": 2,
        "templates": [
            "💑 Идеальная пара: {0} и {1}! Совет да любовь! 💍",
            "❤️ {0} + {1} — корабль отплывает! 🚢",
            "🥂 {0} и {1} — чин-чин за эту парочку! 🍷",
        ]
    },
}


def get_chat_members_from_db(chat_id):
    """Получить список участников чата из БД (кто писал в этот чат)."""
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT DISTINCT u.full_name FROM users u "
        "INNER JOIN chat_memory m ON TRUE "
        "WHERE m.chat_id = %s AND m.role = 'user' "
        "AND u.full_name != '' "
        "GROUP BY u.full_name",
        (chat_id,)
    )
    rows = c.fetchall()
    conn.close()

    names = list(set(row[0] for row in rows if row[0]))
    return names if names else []


def get_all_known_users():
    """Получить всех пользователей из БД."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT full_name FROM users WHERE full_name != '' AND messages > 0")
    rows = c.fetchall()
    conn.close()
    return list(set(row[0] for row in rows if row[0]))


def check_fun_command(text):
    """Проверяет, является ли текст весёлой рандомной командой."""
    text_lower = text.lower().strip()

    # Убираем "!нейро " или "нейро " из начала
    for prefix in ["!нейро ", "!нейро, ", "нейро ", "нейро, "]:
        if text_lower.startswith(prefix):
            text_lower = text_lower[len(prefix):].strip()
            break

    for trigger, config in FUN_COMMANDS.items():
        if trigger in text_lower:
            return config
    return None


# ─────────────────────────────────────────────
#  Память: 20 вопросов + 20 ответов на чат
# ─────────────────────────────────────────────
def save_memory(chat_id, role, content):
    conn = get_db()
    c = conn.cursor()
    now = datetime.now().isoformat()

    c.execute(
        "INSERT INTO chat_memory (chat_id, role, content, created_at) "
        "VALUES (%s, %s, %s, %s)",
        (chat_id, role, content, now)
    )

    c.execute(
        "SELECT COUNT(*) FROM chat_memory WHERE chat_id = %s AND role = %s",
        (chat_id, role)
    )
    count = c.fetchone()[0]

    if count > MEMORY_LIMIT:
        excess = count - MEMORY_LIMIT
        c.execute(
            "DELETE FROM chat_memory WHERE id IN ("
            "  SELECT id FROM chat_memory "
            "  WHERE chat_id = %s AND role = %s "
            "  ORDER BY id ASC LIMIT %s"
            ")",
            (chat_id, role, excess)
        )

    conn.commit()
    conn.close()


def get_memory(chat_id):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT role, content FROM chat_memory "
        "WHERE chat_id = %s ORDER BY id ASC",
        (chat_id,)
    )
    rows = c.fetchall()
    conn.close()
    return [{"role": r, "content": ct} for r, ct in rows]


def clear_memory(chat_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM chat_memory WHERE chat_id = %s", (chat_id,))
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────
#  Пользователи
# ─────────────────────────────────────────────
def get_or_create_user(user_id, username="", full_name=""):
    conn = get_db()
    c = conn.cursor()
    now = datetime.now().isoformat()

    c.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
    row = c.fetchone()

    if row is None:
        c.execute(
            "INSERT INTO users (user_id, username, full_name, reputation, "
            "messages, first_seen, last_seen) VALUES (%s, %s, %s, 0, 0, %s, %s)",
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
            "UPDATE users SET username = %s, full_name = %s, last_seen = %s "
            "WHERE user_id = %s",
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
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE users SET messages = messages + 1 WHERE user_id = %s", (user_id,))
    conn.commit()
    conn.close()


def update_reputation(user_id, delta):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "UPDATE users SET reputation = reputation + %s WHERE user_id = %s",
        (delta, user_id)
    )
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────
#  Счётчик чата
# ─────────────────────────────────────────────
def get_chat_counter(chat_id):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT message_count, next_trigger FROM chat_counters WHERE chat_id = %s",
        (chat_id,)
    )
    row = c.fetchone()

    if row is None:
        trigger = random.randint(10, 15)
        c.execute(
            "INSERT INTO chat_counters (chat_id, message_count, next_trigger) "
            "VALUES (%s, 0, %s)",
            (chat_id, trigger)
        )
        conn.commit()
        conn.close()
        return 0, trigger

    conn.close()
    return row[0], row[1]


def increment_chat_counter(chat_id):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "UPDATE chat_counters SET message_count = message_count + 1 WHERE chat_id = %s",
        (chat_id,)
    )
    conn.commit()
    conn.close()


def reset_chat_counter(chat_id):
    conn = get_db()
    c = conn.cursor()
    new_trigger = random.randint(10, 15)
    c.execute(
        "UPDATE chat_counters SET message_count = 0, next_trigger = %s WHERE chat_id = %s",
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
#  Запросы к ИИ (с памятью) — через httpx
# ─────────────────────────────────────────────
async def ask_neurodeep(chat_id, user_message, user_name="Аноним"):
    try:
        save_memory(chat_id, "user", f"[{user_name}]: {user_message}")

        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        history = get_memory(chat_id)
        messages.extend(history)

        answer = await qurox_chat(messages, max_tokens=300, temperature=0.9)
        save_memory(chat_id, "assistant", answer)
        return answer

    except httpx.TimeoutException:
        logger.error("Qurox API: таймаут (30 сек)")
        return "⏳ Qurox думает слишком долго... Попробуй ещё раз!"
    except httpx.HTTPStatusError as e:
        logger.error(f"Qurox API HTTP ошибка: {e.response.status_code} — {e.response.text[:200]}")
        if e.response.status_code == 401:
            return "🔑 Ошибка авторизации Qurox API! Проверь QUROX_API_KEY."
        elif e.response.status_code == 429:
            return "🚦 Слишком много запросов! Подожди минутку и попробуй снова."
        elif e.response.status_code >= 500:
            return "💥 Сервер Qurox лежит... Попробуй через пару минут."
        return f"❌ Ошибка API: {e.response.status_code}"
    except Exception as e:
        logger.error(f"Ошибка Qurox API: {type(e).__name__}: {e}")
        return random.choice([
            "Мозги перегрелись, дай секунду 🧠💨",
            "Связь с космосом потеряна, повтори 📡",
            "Нейроны на перекуре, попробуй позже 🚬",
        ])


async def is_humor_by_ai(text):
    try:
        messages = [
            {
                "role": "system",
                "content": (
                    "Ты анализатор текста. Определи, содержит ли "
                    "сообщение шутку, подкол, сарказм или юмор. "
                    "Ответь ОДНИМ словом: ДА или НЕТ."
                )
            },
            {"role": "user", "content": text},
        ]
        answer = await qurox_chat(messages, max_tokens=5, temperature=0.1)
        return "ДА" in answer.upper()
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
        f"Я помню последние 20 сообщений — контекст не теряю 🧩\n"
        f"Данные в облачной БД — ничего не пропадёт! 🐘\n\n"
        f"💬 Как задать мне вопрос напрямую:\n"
        f"• !нейро твой вопрос — прямой вопрос\n"
        f"• Ответь (реплай) на моё сообщение — отвечу сразу\n"
        f"• В личке — просто пиши, отвечу на всё\n\n"
        f"🎲 Весёлые команды:\n"
        f"• !нейро кто дурачок\n"
        f"• !нейро кто кого любит\n"
        f"• !нейро кто самый умный\n"
        f"• !нейро кто красавчик\n"
        f"• !нейро кто тут босс\n"
        f"• !нейро кто врёт\n"
        f"• !нейро кто пару\n"
        f"• !нейро кто кому должен\n"
        f"• !нейро кто будет миллионером\n\n"
        f"📋 Другие команды:\n"
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
        "🎲 Весёлые команды:\n"
        "• !нейро кто дурачок — рандомный дурачок\n"
        "• !нейро кто кого любит — рандомная парочка\n"
        "• !нейро кто самый умный — гений чата\n"
        "• !нейро кто красавчик — красавчик дня\n"
        "• !нейро кто тут босс — босс чата\n"
        "• !нейро кто врёт — детектор лжи\n"
        "• !нейро кто пару — идеальная пара\n"
        "• !нейро кто кому должен — кто кому должен\n"
        "• !нейро кто будет миллионером — будущий богач\n\n"
        "📋 Остальные команды:\n"
        "• !профиль — статистика\n"
        "• !реп+ @user — +1 к репутации\n"
        "• !реп- @user — -1 к репутации\n"
        "• !топ — лидерборд\n"
        "• !забудь — очистить память чата\n"
    )


# ─────────────────────────────────────────────
#  Текстовые команды
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
        await message.answer("↩️ Ответь на сообщение того, кому хочешь поднять репу!")
        return
    target = message.reply_to_message.from_user
    if target.id == message.from_user.id:
        await message.answer("Сам себе репу крутить? Не, так не работает 😏")
        return
    get_or_create_user(target.id, target.username or "", target.full_name or "")
    update_reputation(target.id, +1)
    await message.answer(f"⬆️ {target.full_name} получает +1 к репутации! 🔥")


@router.message(F.text.startswith("!реп-"))
async def cmd_rep_minus(message: Message):
    if not message.reply_to_message:
        await message.answer("↩️ Ответь на сообщение того, кому хочешь понизить репу!")
        return
    target = message.reply_to_message.from_user
    if target.id == message.from_user.id:
        await message.answer("Самокритика — это хорошо, но не тут 😂")
        return
    get_or_create_user(target.id, target.username or "", target.full_name or "")
    update_reputation(target.id, -1)
    await message.answer(f"⬇️ {target.full_name} теряет 1 очко репутации 💀")


@router.message(F.text.startswith("!топ"))
async def cmd_top(message: Message):
    conn = get_db()
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
        lines.append(f"{medals[i]} {name} — реп: {rep:+d} | 💬 {msgs}")

    await message.answer("🏆 Топ репутации чата:\n\n" + "\n".join(lines))


@router.message(F.text.startswith("!забудь"))
async def cmd_forget(message: Message):
    clear_memory(message.chat.id)
    await message.answer("🧹 Память очищена! Начинаем с чистого листа 🧠")


# ─────────────────────────────────────────────
#  !нейро — прямой вопрос + весёлые команды
# ─────────────────────────────────────────────
@router.message(F.text.startswith("!нейро"))
async def cmd_neuro(message: Message):
    question = message.text[6:].strip()  # убираем "!нейро"
    if not question:
        await message.answer(
            "❓ Напиши вопрос после команды!\n\n"
            "Пример: !нейро Кто ты такой?\n"
            "Или: !нейро кто дурачок 🎲"
        )
        return

    # Проверяем весёлые команды
    fun = check_fun_command(message.text)
    if fun:
        members = get_all_known_users()
        sender_name = message.from_user.full_name or message.from_user.first_name

        # Добавляем отправителя если его нет
        if sender_name and sender_name not in members:
            members.append(sender_name)

        pick_count = fun["pick"]

        if len(members) < pick_count:
            await message.answer(
                "😅 Мало людей в базе! Нужно минимум "
                f"{pick_count} чел. Пусть народ пообщается сначала!"
            )
            return

        chosen = random.sample(members, pick_count)
        template = random.choice(fun["templates"])
        text_answer = template.format(*chosen)
        await message.answer(text_answer)
        return

    # Обычный вопрос к ИИ
    user_name = message.from_user.first_name or "Аноним"
    get_or_create_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or ""
    )
    increment_user_messages(message.from_user.id)

    logger.info(f"Прямой вопрос от {user_name}: {question[:50]}...")
    response = await ask_neurodeep(message.chat.id, question, user_name)
    await message.reply(response)


# ─────────────────────────────────────────────
#  Проверка: обращение к боту?
# ─────────────────────────────────────────────
def is_direct_to_bot(message: Message) -> bool:
    """Проверяет, обращается ли пользователь напрямую к боту."""
    global BOT_INFO

    # 1. Личный чат (ЛС) — всегда отвечаем
    if message.chat.type == "private":
        return True

    # 2. Реплай на сообщение бота
    if message.reply_to_message and message.reply_to_message.from_user:
        if BOT_INFO and message.reply_to_message.from_user.id == BOT_INFO.id:
            return True

    # 3. Упоминание @username бота в тексте
    text_lower = (message.text or "").lower()
    if BOT_INFO and BOT_INFO.username:
        if f"@{BOT_INFO.username.lower()}" in text_lower:
            return True

    # 4. Ключевые слова обращения к боту
    bot_names = ["нейродип", "neurodeep", "нейро дип", "нейро,", "бот,", "бот "]
    if any(text_lower.startswith(name) for name in bot_names):
        return True

    return False


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

    get_or_create_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or ""
    )
    increment_user_messages(message.from_user.id)

    # 0. Прямое обращение к боту — мгновенный ответ!
    if is_direct_to_bot(message):
        # Сначала проверяем весёлые команды
        fun = check_fun_command(text)
        if fun:
            members = get_all_known_users()
            sender_name = message.from_user.full_name or user_name
            if sender_name and sender_name not in members:
                members.append(sender_name)

            pick_count = fun["pick"]
            if len(members) >= pick_count:
                chosen = random.sample(members, pick_count)
                template = random.choice(fun["templates"])
                await message.reply(template.format(*chosen))
                return

        logger.info(f"Прямой вопрос от {user_name}: {text[:50]}...")
        response = await ask_neurodeep(chat_id, text, user_name)
        await message.reply(response)
        reset_chat_counter(chat_id)
        return

    # 1. Пасхалки
    easter = check_easter_eggs(text)
    if easter:
        await message.reply(easter)
        return

    # 2. Маркеры юмора
    has_humor = check_humor_markers(text)

    # 3. Длинный текст — спрашиваем ИИ
    if not has_humor and len(text) > 30:
        has_humor = await is_humor_by_ai(text)

    # 4. Юмор — мгновенный ответ
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
        logger.info(f"Счётчик ({count}/{trigger}) в чате {chat_id}")
        response = await ask_neurodeep(chat_id, text, user_name)
        await message.reply(response)
        reset_chat_counter(chat_id)


# ─────────────────────────────────────────────
#  Запуск
# ─────────────────────────────────────────────
async def main():
    global BOT_INFO
    init_db()

    # Запускаем keep-alive сервер в отдельном потоке
    keepalive_thread = Thread(target=run_keepalive, daemon=True)
    keepalive_thread.start()

    # Получаем информацию о боте (username, id)
    BOT_INFO = await bot.get_me()
    logger.info(f"NeuroDeep запускается как @{BOT_INFO.username}")
    logger.info(f"PostgreSQL: Neon | Keep-alive: ON")

    # Тест подключения к Qurox API
    try:
        test = await qurox_chat(
            [{"role": "user", "content": "Скажи: ОК"}],
            max_tokens=5, temperature=0.1
        )
        logger.info(f"Qurox API: подключение ОК ✅ (ответ: {test[:20]})")
    except Exception as e:
        logger.warning(f"Qurox API: тест не прошёл — {type(e).__name__}: {e}")
        logger.warning("Бот запустится, но ответы ИИ могут не работать!")

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("NeuroDeep активен! 🧠🔥")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
