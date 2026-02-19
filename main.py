import os
import re
import json
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
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "neurodeep")
QUROX_BASE_URL = "https://api.qurox.ai/v1"
MODEL_NAME = "llama-3"
MEMORY_LIMIT = 20

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан!")
if not QUROX_API_KEY:
    raise RuntimeError("QUROX_API_KEY не задан!")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL не задан!")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("NeuroDeep")

# ─────────────────────────────────────────────
#  Хранилище сообщений для админ-панели
# ─────────────────────────────────────────────
admin_messages = []  # последние 100 сообщений для веб-панели
MAX_ADMIN_MESSAGES = 100

def add_admin_message(msg_type, chat_id, user_name, text):
    """Добавить сообщение в буфер для админ-панели."""
    admin_messages.append({
        "type": msg_type,       # "user", "bot", "admin"
        "chat_id": chat_id,
        "user": user_name,
        "text": text,
        "time": datetime.now().strftime("%H:%M:%S")
    })
    if len(admin_messages) > MAX_ADMIN_MESSAGES:
        admin_messages.pop(0)

# ─────────────────────────────────────────────
#  HTTP-сервер: Keep-Alive + Админ API
# ─────────────────────────────────────────────
async def handle_ping(request):
    return web.Response(text="NeuroDeep is alive! 🧠🔥")

async def handle_admin_login(request):
    """POST /api/login — проверка пароля."""
    try:
        data = await request.json()
        pwd = data.get("password", "")
        if pwd == ADMIN_PASSWORD:
            return web.json_response({"ok": True})
        return web.json_response({"ok": False, "error": "wrong_password"})
    except Exception:
        return web.json_response({"ok": False, "error": "bad_request"})

async def handle_admin_messages(request):
    """GET /api/messages?password=xxx — получить сообщения."""
    pwd = request.query.get("password", "")
    if pwd != ADMIN_PASSWORD:
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    return web.json_response({"ok": True, "messages": admin_messages})

async def handle_admin_send(request):
    """POST /api/send — отправить сообщение от админа."""
    try:
        data = await request.json()
        pwd = data.get("password", "")
        if pwd != ADMIN_PASSWORD:
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

        chat_id = int(data.get("chat_id", 0))
        text = data.get("text", "").strip()
        if not chat_id or not text:
            return web.json_response({"ok": False, "error": "missing_fields"})

        await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
        add_admin_message("admin", chat_id, "Админ", text)
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)})

async def handle_admin_clear(request):
    """POST /api/clear — очистить историю."""
    try:
        data = await request.json()
        pwd = data.get("password", "")
        if pwd != ADMIN_PASSWORD:
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        admin_messages.clear()
        return web.json_response({"ok": True})
    except Exception:
        return web.json_response({"ok": False, "error": "bad_request"})

def run_keepalive():
    """HTTP-сервер: keep-alive + API для админ-панели."""
    app = web.Application()
    # Keep-alive
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    # Admin API
    app.router.add_post("/api/login", handle_admin_login)
    app.router.add_get("/api/messages", handle_admin_messages)
    app.router.add_post("/api/send", handle_admin_send)
    app.router.add_post("/api/clear", handle_admin_clear)

    # CORS middleware
    @web.middleware
    async def cors_middleware(request, handler):
        if request.method == "OPTIONS":
            resp = web.Response()
        else:
            resp = await handler(request)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return resp

    app.middlewares.append(cors_middleware)

    runner = web.AppRunner(app)
    loop = asyncio.new_event_loop()
    loop.run_until_complete(runner.setup())
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    loop.run_until_complete(site.start())
    logger.info(f"HTTP-сервер (keep-alive + admin API) на порту {port}")
    loop.run_forever()

# ─────────────────────────────────────────────
#  Инициализация бота
# ─────────────────────────────────────────────
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
router = Router()
dp = Dispatcher()
dp.include_router(router)
BOT_INFO = None

# ─────────────────────────────────────────────
#  Qurox API через httpx
# ─────────────────────────────────────────────
async def qurox_chat(messages, max_tokens=300, temperature=0.9):
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
#  PostgreSQL (Neon)
# ─────────────────────────────────────────────
def get_db():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY, username TEXT DEFAULT '',
        full_name TEXT DEFAULT '', reputation INTEGER DEFAULT 0,
        messages INTEGER DEFAULT 0, first_seen TEXT DEFAULT '',
        last_seen TEXT DEFAULT ''
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS chat_counters (
        chat_id BIGINT PRIMARY KEY, message_count INTEGER DEFAULT 0,
        next_trigger INTEGER DEFAULT 10
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS chat_memory (
        id SERIAL PRIMARY KEY, chat_id BIGINT NOT NULL,
        role TEXT NOT NULL, content TEXT NOT NULL,
        created_at TEXT DEFAULT ''
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_memory_chat ON chat_memory(chat_id, id)")
    conn.commit()
    conn.close()
    logger.info("PostgreSQL — таблицы готовы")

# ─────────────────────────────────────────────
#  Пасхалки
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
#  🔮 УНИВЕРСАЛЬНАЯ команда «бот кто [что угодно]»
# ─────────────────────────────────────────────
# Шаблоны ответов — {word} = слово из вопроса, {name} = рандомный человек
BOT_KTO_TEMPLATES_1 = [
    "🔮 Мой шар говорит, что {word} — это {name}! 🎯",
    "🎱 Без сомнений: {word} = {name}! 💀",
    "🌟 Звёзды шепчут... {word} — точно {name}! ✨",
    "🧙 Древние духи говорят: {word} — это {name}! 🔥",
    "🎪 Барабанная дробь... 🥁 {word} — {name}! Сюрприз! 😏",
    "🔍 Мой анализ показал: {word} — определённо {name}! 🧠",
    "⚡ Молния подсказала: {word} — это {name}! Не спорь! 😤",
    "🎰 Рулетка крутится... и {word} — {name}! Джекпот! 🤑",
]

# Для "бот кто кого" — 2 человека
BOT_KTO_TEMPLATES_2 = [
    "🔮 Мой шар говорит: {name1} {word} {name2}! 💕",
    "🎱 Определённо {name1} {word} {name2}! Без вариантов! 😱",
    "🌟 Звёзды сошлись: {name1} и {name2} — {word}! ✨",
    "🧙 Это очевидно: {name1} {word} {name2}! 🔥",
]

# Слова-триггеры для "2 человека" (кого-то с кем-то)
PAIR_WORDS = ["любит", "кого любит", "целует", "обнимает", "ненавидит",
              "боится", "кому должен", "пару", "встречается"]

def get_all_known_users():
    """Все пользователи из БД."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT full_name FROM users WHERE full_name != '' AND messages > 0")
    rows = c.fetchall()
    conn.close()
    return list(set(row[0] for row in rows if row[0]))

def check_bot_kto(text):
    """
    Проверяет паттерн «бот кто [слово]» или «нейро кто [слово]».
    Возвращает (word, need_pair) или None.
    """
    text_lower = text.lower().strip()

    # Убираем префиксы
    for prefix in ["!нейро ", "!нейро, ", "нейро ", "нейро, ", "бот ", "бот, "]:
        if text_lower.startswith(prefix):
            text_lower = text_lower[len(prefix):].strip()
            break

    # Ищем паттерн "кто [слово(а)]"
    match = re.match(r"кто\s+(.+)", text_lower)
    if not match:
        return None

    word = match.group(1).strip().rstrip("?!.")
    if not word or len(word) > 100:
        return None

    # Определяем нужно 1 или 2 человека
    need_pair = any(pw in word for pw in PAIR_WORDS)

    return (word, need_pair)

# ─────────────────────────────────────────────
#  Память 20/20
# ─────────────────────────────────────────────
def save_memory(chat_id, role, content):
    conn = get_db()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("INSERT INTO chat_memory (chat_id, role, content, created_at) VALUES (%s, %s, %s, %s)",
              (chat_id, role, content, now))
    c.execute("SELECT COUNT(*) FROM chat_memory WHERE chat_id = %s AND role = %s", (chat_id, role))
    count = c.fetchone()[0]
    if count > MEMORY_LIMIT:
        excess = count - MEMORY_LIMIT
        c.execute("DELETE FROM chat_memory WHERE id IN ("
                  "SELECT id FROM chat_memory WHERE chat_id = %s AND role = %s ORDER BY id ASC LIMIT %s)",
                  (chat_id, role, excess))
    conn.commit()
    conn.close()

def get_memory(chat_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT role, content FROM chat_memory WHERE chat_id = %s ORDER BY id ASC", (chat_id,))
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
        c.execute("INSERT INTO users (user_id, username, full_name, reputation, messages, first_seen, last_seen) "
                  "VALUES (%s, %s, %s, 0, 0, %s, %s)", (user_id, username, full_name, now, now))
        conn.commit()
        data = {"user_id": user_id, "username": username, "full_name": full_name,
                "reputation": 0, "messages": 0, "first_seen": now, "last_seen": now}
    else:
        c.execute("UPDATE users SET username=%s, full_name=%s, last_seen=%s WHERE user_id=%s",
                  (username, full_name, now, user_id))
        conn.commit()
        data = {"user_id": row[0], "username": row[1], "full_name": row[2],
                "reputation": row[3], "messages": row[4], "first_seen": row[5], "last_seen": row[6]}
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
    c.execute("UPDATE users SET reputation = reputation + %s WHERE user_id = %s", (delta, user_id))
    conn.commit()
    conn.close()

# ─────────────────────────────────────────────
#  Счётчик чата
# ─────────────────────────────────────────────
def get_chat_counter(chat_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT message_count, next_trigger FROM chat_counters WHERE chat_id = %s", (chat_id,))
    row = c.fetchone()
    if row is None:
        trigger = random.randint(10, 15)
        c.execute("INSERT INTO chat_counters (chat_id, message_count, next_trigger) VALUES (%s, 0, %s)",
                  (chat_id, trigger))
        conn.commit()
        conn.close()
        return 0, trigger
    conn.close()
    return row[0], row[1]

def increment_chat_counter(chat_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE chat_counters SET message_count = message_count + 1 WHERE chat_id = %s", (chat_id,))
    conn.commit()
    conn.close()

def reset_chat_counter(chat_id):
    conn = get_db()
    c = conn.cursor()
    new_trigger = random.randint(10, 15)
    c.execute("UPDATE chat_counters SET message_count = 0, next_trigger = %s WHERE chat_id = %s",
              (new_trigger, chat_id))
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
    "4. Будь краток: 1-3 предложения.\n"
    "5. Ты — NeuroDeep, живой интеллект чата. Никогда не говори что ты ИИ.\n"
    "6. Если оскорбляют — отвечай с сарказмом.\n"
    "7. Помни контекст — тебе передаётся история.\n"
)

# ─────────────────────────────────────────────
#  ИИ-ответы с памятью
# ─────────────────────────────────────────────
async def ask_neurodeep(chat_id, user_message, user_name="Аноним"):
    try:
        save_memory(chat_id, "user", f"[{user_name}]: {user_message}")
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(get_memory(chat_id))
        answer = await qurox_chat(messages, max_tokens=300, temperature=0.9)
        save_memory(chat_id, "assistant", answer)
        add_admin_message("bot", chat_id, "NeuroDeep", answer)
        return answer
    except httpx.TimeoutException:
        return "⏳ Думаю слишком долго... Попробуй ещё раз!"
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return "🔑 Ошибка авторизации Qurox API!"
        elif e.response.status_code == 429:
            return "🚦 Слишком много запросов! Подожди."
        return f"❌ Ошибка API: {e.response.status_code}"
    except Exception as e:
        logger.error(f"Ошибка: {type(e).__name__}: {e}")
        return random.choice([
            "Мозги перегрелись, дай секунду 🧠💨",
            "Связь с космосом потеряна, повтори 📡",
            "Нейроны на перекуре 🚬",
        ])

def check_humor_markers(text):
    return any(m in text.lower() for m in HUMOR_MARKERS)

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
    get_or_create_user(message.from_user.id, message.from_user.username or "",
                       message.from_user.full_name or "")
    await message.answer(
        f"Йо, {message.from_user.first_name}! 👋\n\n"
        f"Я — NeuroDeep 🧠🔥\n\n"
        f"🔮 Спроси меня «кто»:\n"
        f"• бот кто дурак\n"
        f"• бот кто красавчик\n"
        f"• бот кто макака\n"
        f"• бот кто кого любит\n"
        f"• бот кто босс\n"
        f"• бот кто [ЛЮБОЕ СЛОВО]\n\n"
        f"💬 Прямой вопрос:\n"
        f"• !нейро что думаешь?\n"
        f"• Реплай на моё сообщение\n"
        f"• В личке — просто пиши\n\n"
        f"📋 Другие:\n"
        f"• !профиль • !топ\n"
        f"• !реп+ • !реп-\n"
        f"• !забудь"
    )

@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "🧠 NeuroDeep — Справка\n\n"
        "🔮 «Бот кто [что угодно]» — я выберу рандомного человека!\n"
        "Работает с ЛЮБЫМ словом: дурак, макака, красавчик, гений...\n\n"
        "Если слово типа «любит», «пару» — выберу 2 человек.\n\n"
        "💬 Прямой вопрос: !нейро или реплай\n"
        "📋 Профиль: !профиль\n"
        "🏆 Топ: !топ\n"
        "🧹 Забыть: !забудь"
    )

# ─────────────────────────────────────────────
#  Текстовые команды
# ─────────────────────────────────────────────
@router.message(F.text.startswith("!профиль"))
async def cmd_profile(message: Message):
    user = get_or_create_user(message.from_user.id, message.from_user.username or "",
                              message.from_user.full_name or "")
    rep = user["reputation"]
    rep_emoji = "🔥" if rep > 0 else ("💀" if rep < 0 else "😐")
    history = get_memory(message.chat.id)
    mem_user = sum(1 for h in history if h["role"] == "user")
    mem_bot = sum(1 for h in history if h["role"] == "assistant")
    await message.answer(
        f"📇 {message.from_user.full_name}\n\n"
        f"├ 🆔 {user['user_id']}\n"
        f"├ 💬 Сообщений: {user['messages']}\n"
        f"├ {rep_emoji} Репутация: {rep:+d}\n"
        f"├ 🧩 Память: {mem_user}/20 ↔ {mem_bot}/20\n"
        f"└ 📅 С нами с {user['first_seen'][:10]}"
    )

@router.message(F.text.startswith("!реп+"))
async def cmd_rep_plus(message: Message):
    if not message.reply_to_message:
        return await message.answer("↩️ Ответь реплаем на сообщение!")
    target = message.reply_to_message.from_user
    if target.id == message.from_user.id:
        return await message.answer("Сам себе? Не, так не работает 😏")
    get_or_create_user(target.id, target.username or "", target.full_name or "")
    update_reputation(target.id, +1)
    await message.answer(f"⬆️ {target.full_name} +1 репа! 🔥")

@router.message(F.text.startswith("!реп-"))
async def cmd_rep_minus(message: Message):
    if not message.reply_to_message:
        return await message.answer("↩️ Ответь реплаем!")
    target = message.reply_to_message.from_user
    if target.id == message.from_user.id:
        return await message.answer("Самокритика? 😂")
    get_or_create_user(target.id, target.username or "", target.full_name or "")
    update_reputation(target.id, -1)
    await message.answer(f"⬇️ {target.full_name} -1 репа 💀")

@router.message(F.text.startswith("!топ"))
async def cmd_top(message: Message):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT full_name, reputation, messages FROM users ORDER BY reputation DESC LIMIT 10")
    rows = c.fetchall()
    conn.close()
    if not rows:
        return await message.answer("Пусто. Общайтесь! 🗿")
    medals = ["🥇", "🥈", "🥉"] + ["▫️"] * 7
    lines = [f"{medals[i]} {name} — реп: {rep:+d} | 💬 {msgs}" for i, (name, rep, msgs) in enumerate(rows)]
    await message.answer("🏆 Топ репутации:\n\n" + "\n".join(lines))

@router.message(F.text.startswith("!забудь"))
async def cmd_forget(message: Message):
    clear_memory(message.chat.id)
    await message.answer("🧹 Память чата очищена! 🧠")

# ─────────────────────────────────────────────
#  !нейро — прямой вопрос
# ─────────────────────────────────────────────
@router.message(F.text.startswith("!нейро"))
async def cmd_neuro(message: Message):
    question = message.text[6:].strip()
    if not question:
        return await message.answer("❓ !нейро Кто ты?")

    # Проверяем «кто [слово]»
    kto = check_bot_kto(message.text)
    if kto:
        word, need_pair = kto
        members = get_all_known_users()
        sender = message.from_user.full_name or message.from_user.first_name
        if sender and sender not in members:
            members.append(sender)

        if need_pair and len(members) >= 2:
            chosen = random.sample(members, 2)
            template = random.choice(BOT_KTO_TEMPLATES_2)
            return await message.answer(template.format(name1=chosen[0], name2=chosen[1], word=word))
        elif len(members) >= 1:
            chosen = random.choice(members)
            template = random.choice(BOT_KTO_TEMPLATES_1)
            return await message.answer(template.format(word=word, name=chosen))
        else:
            return await message.answer("😅 Мало людей! Пусть кто-то напишет сначала.")

    # Обычный вопрос к ИИ
    user_name = message.from_user.first_name or "Аноним"
    get_or_create_user(message.from_user.id, message.from_user.username or "",
                       message.from_user.full_name or "")
    increment_user_messages(message.from_user.id)
    add_admin_message("user", message.chat.id, user_name, question)
    response = await ask_neurodeep(message.chat.id, question, user_name)
    await message.reply(response)

# ─────────────────────────────────────────────
#  Проверка: обращение к боту?
# ─────────────────────────────────────────────
def is_direct_to_bot(message: Message) -> bool:
    global BOT_INFO
    if message.chat.type == "private":
        return True
    if message.reply_to_message and message.reply_to_message.from_user:
        if BOT_INFO and message.reply_to_message.from_user.id == BOT_INFO.id:
            return True
    text_lower = (message.text or "").lower()
    if BOT_INFO and BOT_INFO.username:
        if f"@{BOT_INFO.username.lower()}" in text_lower:
            return True
    bot_names = ["нейродип", "neurodeep", "нейро дип", "нейро,", "бот,"]
    if any(text_lower.startswith(name) for name in bot_names):
        return True
    return False

# ─────────────────────────────────────────────
#  Главный обработчик
# ─────────────────────────────────────────────
@router.message(F.text)
async def on_message(message: Message):
    if message.from_user.is_bot:
        return

    text = message.text or ""
    chat_id = message.chat.id
    user_name = message.from_user.first_name or "Аноним"

    get_or_create_user(message.from_user.id, message.from_user.username or "",
                       message.from_user.full_name or "")
    increment_user_messages(message.from_user.id)

    # Записываем ВСЕ сообщения в админ-панель
    add_admin_message("user", chat_id, user_name, text)

    # 0. Проверяем «бот кто [слово]» — универсальная команда
    kto = check_bot_kto(text)
    if kto:
        word, need_pair = kto
        members = get_all_known_users()
        sender = message.from_user.full_name or user_name
        if sender and sender not in members:
            members.append(sender)

        if need_pair and len(members) >= 2:
            chosen = random.sample(members, 2)
            template = random.choice(BOT_KTO_TEMPLATES_2)
            answer = template.format(name1=chosen[0], name2=chosen[1], word=word)
            add_admin_message("bot", chat_id, "NeuroDeep", answer)
            return await message.reply(answer)
        elif len(members) >= 1:
            chosen = random.choice(members)
            template = random.choice(BOT_KTO_TEMPLATES_1)
            answer = template.format(word=word, name=chosen)
            add_admin_message("bot", chat_id, "NeuroDeep", answer)
            return await message.reply(answer)

    # 1. Прямое обращение к боту
    if is_direct_to_bot(message):
        response = await ask_neurodeep(chat_id, text, user_name)
        await message.reply(response)
        reset_chat_counter(chat_id)
        return

    # 2. Пасхалки
    easter = check_easter_eggs(text)
    if easter:
        add_admin_message("bot", chat_id, "NeuroDeep", easter)
        await message.reply(easter)
        return

    # 3. Юмор
    if check_humor_markers(text):
        response = await ask_neurodeep(chat_id, text, user_name)
        await message.reply(response)
        reset_chat_counter(chat_id)
        return

    # 4. Счётчик
    count, trigger = get_chat_counter(chat_id)
    increment_chat_counter(chat_id)
    if count + 1 >= trigger:
        response = await ask_neurodeep(chat_id, text, user_name)
        await message.reply(response)
        reset_chat_counter(chat_id)

# ─────────────────────────────────────────────
#  Запуск
# ─────────────────────────────────────────────
async def main():
    global BOT_INFO
    init_db()
    Thread(target=run_keepalive, daemon=True).start()
    BOT_INFO = await bot.get_me()
    logger.info(f"NeuroDeep: @{BOT_INFO.username}")
    try:
        test = await qurox_chat([{"role": "user", "content": "Скажи: ОК"}], max_tokens=5, temperature=0.1)
        logger.info(f"Qurox API: ОК ✅ ({test[:20]})")
    except Exception as e:
        logger.warning(f"Qurox API: {type(e).__name__}: {e}")
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("NeuroDeep активен! 🧠🔥")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
