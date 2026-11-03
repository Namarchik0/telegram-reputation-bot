import os
import re
import math
import sqlite3
import logging
import asyncio
import json
import urllib.error
import urllib.request
import subprocess
import shutil
from io import BytesIO
from datetime import datetime, timezone
from html import escape

from PIL import Image, ImageDraw, ImageFont, ImageOps
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                       ReplyKeyboardMarkup, KeyboardButton)
from telegram.ext import (
    Application, MessageHandler, CommandHandler, CallbackQueryHandler,
    ContextTypes, filters
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO
)

TOKEN = os.environ.get("BOT_TOKEN")
DB_PATH = os.environ.get("DB_PATH", "/var/data/reputation.db")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WELCOME_IMAGE = os.path.join(BASE_DIR, "welcome_chats.png")
START_IMAGE = os.path.join(BASE_DIR, "welcome_start.jpeg")
CHATS_URL = "https://t.me/calvinhodnik"
SUPPORT_URL = "https://t.me/luminbad"

# Crypto Pay API (mainnet)
CRYPTOBOT_API_TOKEN = os.environ.get("CRYPTOBOT_API_TOKEN", "").strip()
CRYPTOBOT_API_URL = "https://pay.crypt.bot/api"
DEPOSIT_ASSET = os.environ.get("DEPOSIT_ASSET", "USDT").strip().upper()
DEPOSIT_MIN = float(os.environ.get("DEPOSIT_MIN", "1"))
DEPOSIT_MAX = float(os.environ.get("DEPOSIT_MAX", "1000000"))
ADMIN_CHAT_ID_RAW = os.environ.get("ADMIN_CHAT_ID", "").strip()
ADMIN_CHAT_IDS = [x.strip() for x in ADMIN_CHAT_ID_RAW.split(",") if x.strip()]

if not TOKEN:
    raise RuntimeError("Set BOT_TOKEN in the hosting service environment variables.")

os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)


def connect():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with connect() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT NOT NULL,
                prefix TEXT NOT NULL DEFAULT 'НЕТ',
                deposit REAL NOT NULL DEFAULT 0,
                deposit_date TEXT,
                interested INTEGER NOT NULL DEFAULT 0,
                registered_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS deals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)
        # Keep compatibility with the original bot's reputation history.
        db.execute("""
            CREATE TABLE IF NOT EXISTS reputation (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                giver_id INTEGER NOT NULL,
                giver_name TEXT NOT NULL,
                giver_username TEXT,
                target_key TEXT NOT NULL,
                target_username TEXT NOT NULL,
                delta INTEGER NOT NULL,
                reason TEXT NOT NULL,
                photo_file_id TEXT,
                created_at TEXT NOT NULL
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_rep_target ON reputation(chat_id, target_key, id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_deals_user ON deals(chat_id, user_id, id)")
        cols = {row[1] for row in db.execute("PRAGMA table_info(users)").fetchall()}
        if "deposit_date" not in cols:
            db.execute("ALTER TABLE users ADD COLUMN deposit_date TEXT")
        db.execute("""
            CREATE TABLE IF NOT EXISTS payment_events (
                invoice_id TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                asset TEXT NOT NULL,
                paid_at TEXT NOT NULL
            )
        """)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def money(value):
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") + "$"


def register_user(chat_id, user):
    with connect() as db:
        db.execute("""
            INSERT INTO users (chat_id, user_id, username, full_name, registered_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                username=excluded.username,
                full_name=excluded.full_name
        """, (chat_id, user.id, user.username, user.full_name or str(user.id), now_iso()))


def get_user_by_username(chat_id, username):
    username = username.lstrip("@").casefold()
    with connect() as db:
        return db.execute("""
            SELECT * FROM users
            WHERE chat_id=? AND lower(username)=?
            LIMIT 1
        """, (chat_id, username)).fetchone()


def get_profile(chat_id, user_id):
    with connect() as db:
        user = db.execute(
            "SELECT * FROM users WHERE chat_id=? AND user_id=?",
            (chat_id, user_id)
        ).fetchone()
        if not user:
            return None
        deal_data = db.execute("""
            SELECT COUNT(*) AS count, COALESCE(SUM(amount), 0) AS amount
            FROM deals WHERE chat_id=? AND user_id=?
        """, (chat_id, user_id)).fetchone()
        username_key = (user["username"] or "").casefold()
        if username_key:
            rep = db.execute("""
                SELECT
                    SUM(CASE WHEN delta=1 THEN 1 ELSE 0 END) AS plus_count,
                    SUM(CASE WHEN delta=-1 THEN 1 ELSE 0 END) AS minus_count,
                    SUM(CASE WHEN delta=1 THEN 1 ELSE 0 END) AS plus_amount,
                    SUM(CASE WHEN delta=-1 THEN 1 ELSE 0 END) AS minus_amount
                FROM reputation WHERE chat_id=? AND target_key=?
            """, (chat_id, username_key)).fetchone()
        else:
            rep = {"plus_count": 0, "minus_count": 0, "plus_amount": 0, "minus_amount": 0}
    return {
        "user": user,
        "deal_count": deal_data["count"],
        "deal_amount": float(deal_data["amount"] or 0),
        "plus": int(rep["plus_count"] or 0),
        "minus": int(rep["minus_count"] or 0),
    }


def load_font(size, bold=False):
    paths = (
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"]
        if bold else
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"]
    )
    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def fit_text(draw, text, max_width, start_size, bold=False, min_size=15):
    size = start_size
    while size > min_size:
        font = load_font(size, bold)
        if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
            return font
        size -= 1
    return load_font(min_size, bold)






def make_profile_card(profile, avatar_bytes=None):
    """Create a clean, minimal CALVINE reputation card with a dynamic scale."""
    plus = max(0, int(profile.get("plus", 0)))
    minus = max(0, int(profile.get("minus", 0)))
    total = plus + minus
    positive_pct = round(plus * 100 / total) if total else 0
    negative_pct = 100 - positive_pct if total else 0

    template_path = os.path.join(BASE_DIR, "reputation_template.jpeg")
    im = Image.open(template_path).convert("RGB")
    d = ImageDraw.Draw(im)

    green = (92, 160, 83)
    red = (205, 103, 103)
    dark = (50, 76, 51)
    muted = (91, 119, 88)
    track = (232, 236, 219)

    def font(size, bold=False):
        paths = (
            ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
             "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"]
            if bold else
            ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
             "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"]
        )
        for path in paths:
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                pass
        return ImageFont.load_default()

    # Minimal centered reputation scale. Large stats sit directly above the bar.
    x1, y1, x2, y2 = 205, 390, 1235, 460

    # Large, bold numbers above the scale.
    d.text((205, 325), f"+{plus}", font=font(96, True), fill=green, anchor="lm")
    d.text((720, 325), f"{positive_pct}%", font=font(64, True), fill=dark, anchor="mm")
    d.text((1235, 325), f"−{minus}", font=font(96, True), fill=red, anchor="rm")

    d.rounded_rectangle((x1, y1, x2, y2), radius=35, fill=track)
    if total:
        fill_w = int((x2 - x1) * plus / total)
        if fill_w > 0:
            d.rounded_rectangle((x1, y1, x1 + fill_w, y2), radius=35, fill=green)
            if fill_w < (x2 - x1):
                d.rectangle((x1 + fill_w - 35, y1, x1 + fill_w, y2), fill=green)
        if fill_w < (x2 - x1):
            d.rounded_rectangle((x1 + fill_w, y1, x2, y2), radius=35, fill=red)
            if fill_w > 0:
                d.rectangle((x1 + fill_w, y1, x1 + fill_w + 35, y2), fill=red)

    d.text((720, 535), f"Всего оценок: {total}", font=font(32, True),
           fill=muted, anchor="mm")

    output = BytesIO()
    im.save(output, format="JPEG", quality=95, optimize=True)
    output.seek(0)
    output.name = "calvine_reputation.jpg"
    return output


def profile_keyboard(user_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("💰 Депозит", callback_data=f"deposit:{user_id}")
    ]])

def deposit_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Внести депозит", callback_data="deposit_pay")],
        [InlineKeyboardButton("◀️ Назад", callback_data="deposit_back")],
    ])

def deposit_text(user):
    current = money(float(user["deposit"] or 0))
    date = user["deposit_date"] or "—"
    return (
        "💰 <b>Депозит</b>\n\n"
        f"<b>Текущий депозит:</b> {current}\n"
        f"<b>Дата внесения депозита:</b> {escape(date)}"
    )

def parse_amount(text):
    value = float(text.strip().replace(" ", "").replace(",", "."))
    if not math.isfinite(value):
        raise ValueError
    if value < DEPOSIT_MIN or value > DEPOSIT_MAX:
        raise ValueError
    return round(value, 8)

def cryptobot_request(method, payload=None):
    if not CRYPTOBOT_API_TOKEN:
        raise RuntimeError("CRYPTOBOT_API_TOKEN не задан")
    url = f"{CRYPTOBOT_API_URL}/{method}"
    body = json.dumps(payload or {}).encode("utf-8")
    headers = {
        "Crypto-Pay-API-Token": CRYPTOBOT_API_TOKEN,
        "Content-Type": "application/json",
        "Accept": "application/json",
        # CryptoBot is protected by Cloudflare. Some hosting IPs get a 403/1010
        # when Python's default User-Agent (Python-urllib/...) is used.
        "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 Chrome/131.0.0.0 Mobile Safari/537.36",
    }
    request = urllib.request.Request(
        url, data=body, method="POST", headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        # Cloudflare error 1010 means the edge blocked the request before
        # Crypto Pay API could process it. Try curl once if the host provides it.
        if exc.code == 403 and "1010" in detail:
            curl = shutil.which("curl")
            if curl:
                try:
                    result = subprocess.run(
                        [curl, "-sS", "--max-time", "20", "-X", "POST", url,
                         "-H", f"Crypto-Pay-API-Token: {CRYPTOBOT_API_TOKEN}",
                         "-H", "Content-Type: application/json",
                         "-H", "Accept: application/json",
                         "-A", headers["User-Agent"],
                         "--data-binary", body],
                        capture_output=True, text=True, timeout=25, check=False,
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        raw = result.stdout
                    else:
                        raise RuntimeError(
                            f"CryptoBot HTTP {exc.code}: {detail}"
                        ) from exc
                except Exception:
                    raise RuntimeError(
                        "CryptoBot HTTP 403 / Cloudflare 1010: хостинг отклоняет запрос. "
                        "Если после перезапуска ошибка сохраняется, нужен другой исходящий IP/маршрут "
                        "или обращение в поддержку BotHost/Crypto Pay."
                    ) from exc
            else:
                raise RuntimeError(
                    "CryptoBot HTTP 403 / Cloudflare 1010: запрос блокируется на стороне Cloudflare. "
                    "В код добавлен User-Agent, но на этом хостинге curl недоступен."
                ) from exc
        else:
            raise RuntimeError(f"CryptoBot HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"CryptoBot connection error: {exc.reason}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"CryptoBot returned invalid JSON: {raw[:500]}") from exc
    if not data.get("ok"):
        error = data.get("error") or data.get("description") or "unknown_error"
        raise RuntimeError(f"CryptoBot API error: {error}")
    return data.get("result")

def create_deposit_invoice(chat_id, user_id, username, amount):
    payload = {
        "currency_type": "crypto",
        "asset": DEPOSIT_ASSET,
        "amount": f"{amount:.8f}".rstrip("0").rstrip("."),
        "description": "Страховой депозит CALVINE",
        "hidden_message": "Страховой депозит CALVINE успешно внесён.",
        "payload": json.dumps({
            "type": "deposit",
            "chat_id": chat_id,
            "user_id": user_id,
            "username": username or "",
        }, ensure_ascii=False),
        "allow_comments": False,
        "allow_anonymous": False,
        "expires_in": 3600,
    }
    return cryptobot_request("createInvoice", payload)

def register_paid_invoice(invoice_id, chat_id, user_id, amount, asset, paid_at):
    with connect() as db:
        cur = db.execute(
            "INSERT OR IGNORE INTO payment_events(invoice_id, chat_id, user_id, amount, asset, paid_at) VALUES (?, ?, ?, ?, ?, ?)",
            (str(invoice_id), chat_id, user_id, amount, asset, paid_at),
        )
        if cur.rowcount == 0:
            return False
        db.execute(
            "UPDATE users SET deposit=?, deposit_date=? WHERE chat_id=? AND user_id=?",
            (amount, paid_at, chat_id, user_id),
        )
        return True


async def get_avatar_bytes(bot, user_id):
    """Return the user's Telegram profile photo bytes, if they have one."""
    try:
        photos = await bot.get_user_profile_photos(user_id=user_id, limit=1)
        if not photos.total_count or not photos.photos:
            return None
        photo = photos.photos[0][-1]
        file = await bot.get_file(photo.file_id)
        return bytes(await file.download_as_bytearray())
    except Exception:
        logging.exception("Could not load profile photo for user %s", user_id)
        return None


async def send_profile(message, chat_id, user_id):
    profile = get_profile(chat_id, user_id)
    if not profile:
        await message.reply_text(
            "Пользователь ещё не зарегистрирован в этом чате. "
            "Попросите его написать любое сообщение."
        )
        return

    user = profile["user"]
    registered = user["registered_at"] or ""
    try:
        registered = datetime.fromisoformat(
            registered.replace("Z", "+00:00")
        ).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        registered = registered[:10] if registered else "—"

    display_name = (
        "@" + user["username"] if user["username"] else user["full_name"]
    )
    safe_name = escape(display_name)
    info_text = (
        "<b>👤 Информация о пользователе</b>\n"
        f"<b>{safe_name}</b>\n"
        f"<b>➕ Положительных репутаций: {profile['plus']}</b>\n"
        f"<b>➖ Отрицательных репутаций: {profile['minus']}</b>\n"
        f"<b>◇ Депозит: {money(float(user['deposit'] or 0))}</b>\n"
        f"<b>🌐 Регистрация: {registered}</b>\n"
        f"<b>🎓 Интересовались: {int(user['interested'] or 0)} раз</b>"
    )
    await message.reply_photo(
        photo=make_profile_card(profile),
        caption=info_text,
        parse_mode="HTML",
        reply_markup=profile_keyboard(user_id),
    )


async def is_chat_admin(update, context):
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return False
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False



async def remember_menu_message(context, message):
    """Remember menu-related messages so /start can clean up the old menu."""
    if message:
        ids = context.chat_data.setdefault("menu_message_ids", [])
        ids.append(message.message_id)
        # Keep the list bounded; Telegram only allows deletion for recent messages.
        context.chat_data["menu_message_ids"] = ids[-100:]


async def delete_old_menu_messages(context, chat_id, current_message=None):
    ids = context.chat_data.pop("menu_message_ids", [])
    if current_message:
        ids.append(current_message.message_id)
    for message_id in dict.fromkeys(ids):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            # Some messages may be too old or no longer deletable by the bot.
            logging.debug("Could not delete menu message %s", message_id, exc_info=True)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not msg or not chat:
        return

    if user:
        register_user(chat.id, user)

    # Clear the previous menu conversation, including the user's old menu taps.
    await delete_old_menu_messages(context, chat.id, msg)

    keyboard = ReplyKeyboardMarkup(
        [
            [KeyboardButton("👤 Профиль"), KeyboardButton("💰 Депозит")],
            [KeyboardButton("чаты💎"), KeyboardButton("поддержка")],
        ],
        resize_keyboard=True,
        is_persistent=True
    )
    caption = (
        "Добро пожаловать в бота репутации чата CALVINE\n"
        "Пожалуйста выбери нужный раздел"
    )
    try:
        with open(START_IMAGE, "rb") as photo:
            sent = await context.bot.send_photo(
                chat_id=chat.id,
                photo=photo,
                caption=caption,
                reply_markup=keyboard
            )
    except FileNotFoundError:
        logging.exception("Start image not found: %s", START_IMAGE)
        sent = await context.bot.send_message(
            chat_id=chat.id, text=caption, reply_markup=keyboard
        )
    await remember_menu_message(context, sent)


async def menu_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text:
        return
    text = msg.text.strip().casefold()

    # Remember the user's menu selection for cleanup on the next /start.
    await remember_menu_message(context, msg)

    if text == "👤 профиль":
        await send_profile(msg, msg.chat_id, msg.from_user.id)
        return

    if text == "💰 депозит":
        user = get_profile(msg.chat_id, msg.from_user.id)
        if not user:
            register_user(msg.chat_id, msg.from_user)
            user = get_profile(msg.chat_id, msg.from_user.id)
        sent = await msg.reply_text(
            deposit_text(user["user"]),
            parse_mode="HTML",
            reply_markup=deposit_keyboard(),
        )
        await remember_menu_message(context, sent)
        return

    if text == "чаты💎":
        back_keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton("назад")]],
            resize_keyboard=True,
            is_persistent=True
        )
        inline = InlineKeyboardMarkup([[
            InlineKeyboardButton("Залетай🥵", url=CHATS_URL)
        ]])
        try:
            with open(WELCOME_IMAGE, "rb") as photo:
                sent = await context.bot.send_photo(
                    chat_id=msg.chat_id,
                    photo=photo,
                    caption="Присоединяйся к чату CALVINE 💎",
                    reply_markup=inline
                )
        except FileNotFoundError:
            logging.exception("Welcome image not found: %s", WELCOME_IMAGE)
            sent = await context.bot.send_message(
                chat_id=msg.chat_id,
                text="Присоединяйся к чату CALVINE 💎",
                reply_markup=inline
            )
        await remember_menu_message(context, sent)
        back_msg = await msg.reply_text(
            "Чтобы вернуться в меню, нажми «назад».", reply_markup=back_keyboard
        )
        await remember_menu_message(context, back_msg)

    elif text == "поддержка":
        back_keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton("назад")]],
            resize_keyboard=True,
            is_persistent=True
        )
        support_link = InlineKeyboardMarkup([[
            InlineKeyboardButton("Написать в поддержку · @luminbad", url=SUPPORT_URL)
        ]])
        # One combined support message: text and the clickable support link.
        support_msg = await msg.reply_text(
            "Если нужна помощь, нажми кнопку ниже 👇",
            reply_markup=support_link
        )
        await remember_menu_message(context, support_msg)
        back_msg = await msg.reply_text(
            "Чтобы вернуться в меню, нажми «назад».", reply_markup=back_keyboard
        )
        await remember_menu_message(context, back_msg)

    elif text == "назад":
        main_keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton("чаты💎"), KeyboardButton("поддержка")]],
            resize_keyboard=True,
            is_persistent=True
        )
        back_msg = await msg.reply_text(
            "Добро пожаловать в меню CALVINE. Выбери нужный раздел 👇",
            reply_markup=main_keyboard
        )
        await remember_menu_message(context, back_msg)


async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, chat, sender = update.effective_message, update.effective_chat, update.effective_user
    if not msg or not chat or not sender:
        return

    register_user(chat.id, sender)

    # Priority: /p @username, otherwise use the author of the message
    # to which /p is replying, otherwise show the command sender's profile.
    if context.args:
        target = get_user_by_username(chat.id, context.args[0])
        if not target:
            await msg.reply_text(
                "Не нашёл пользователя по username. Он должен хотя бы раз написать в этом чате."
            )
            return
        user_id = target["user_id"]

    elif msg.reply_to_message and msg.reply_to_message.from_user:
        replied_user = msg.reply_to_message.from_user
        if replied_user.is_bot:
            await msg.reply_text("Нельзя открыть профиль бота.")
            return
        register_user(chat.id, replied_user)
        user_id = replied_user.id

    else:
        user_id = sender.id

    await send_profile(msg, chat.id, user_id)


async def rep_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, user, chat = update.effective_message, update.effective_user, update.effective_chat
    if not msg or not user or not chat or chat.type not in ("group", "supergroup"):
        return
    register_user(chat.id, user)
    raw = msg.caption or msg.text or ""
    match = re.match(r"^\s*([+-])\s*реп\s+@([A-Za-z0-9_]{5,32})(?:\s+([\s\S]*))?\s*$",
                     raw, re.IGNORECASE)
    if not match:
        return
    sign, username, reason = match.groups()
    if user.username and user.username.casefold() == username.casefold():
        await msg.reply_text("Нельзя поставить репутацию самому себе.")
        return
    target = get_user_by_username(chat.id, username)
    if not target:
        await msg.reply_text("Пользователь не найден. Он должен написать хотя бы одно сообщение в чате.")
        return
    photo_file_id = msg.photo[-1].file_id if msg.photo else None
    if not photo_file_id and msg.reply_to_message and msg.reply_to_message.photo:
        photo_file_id = msg.reply_to_message.photo[-1].file_id
    with connect() as db:
        db.execute("""
            INSERT INTO reputation
            (chat_id, giver_id, giver_name, giver_username, target_key,
             target_username, delta, reason, photo_file_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            chat.id, user.id, user.full_name or str(user.id), user.username,
            username.casefold(), username, 1 if sign == "+" else -1,
            (reason or "Причина не указана").strip(), photo_file_id, now_iso()
        ))
    await msg.reply_text(f"Репутация для @{escape(username)} обновлена: {'+1' if sign == '+' else '−1'}.",
                         parse_mode="HTML")


async def deal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    if not msg or not chat:
        return
    if not await is_chat_admin(update, context):
        await msg.reply_text("Эта команда доступна только администраторам чата.")
        return
    if len(context.args) < 2:
        await msg.reply_text("Использование: /deal @username сумма [описание]")
        return
    target = get_user_by_username(chat.id, context.args[0])
    try:
        amount = float(context.args[1].replace(",", "."))
        if amount < 0:
            raise ValueError()
    except ValueError:
        await msg.reply_text("Укажи корректную сумму, например: /deal @user 3.00")
        return
    if not target:
        await msg.reply_text("Пользователь не найден в базе этого чата.")
        return
    note = " ".join(context.args[2:])
    with connect() as db:
        db.execute(
            "INSERT INTO deals (chat_id, user_id, amount, note, created_at) VALUES (?, ?, ?, ?, ?)",
            (chat.id, target["user_id"], amount, note, now_iso())
        )
    await msg.reply_text(f"Сделка для @{escape(target['username'] or target['full_name'])} добавлена: {money(amount)}.",
                         parse_mode="HTML")


async def deposit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    if not msg or not chat:
        return
    if not await is_chat_admin(update, context):
        await msg.reply_text("Эта команда доступна только администраторам чата.")
        return
    if len(context.args) != 2:
        await msg.reply_text("Использование: /deposit @username сумма")
        return
    target = get_user_by_username(chat.id, context.args[0])
    try:
        amount = float(context.args[1].replace(",", "."))
        if amount < 0:
            raise ValueError()
    except ValueError:
        await msg.reply_text("Укажи корректную сумму.")
        return
    if not target:
        await msg.reply_text("Пользователь не найден в базе этого чата.")
        return
    with connect() as db:
        db.execute("UPDATE users SET deposit=?, deposit_date=? WHERE chat_id=? AND user_id=?",
                   (amount, now_iso(), chat.id, target["user_id"]))
    await msg.reply_text(f"Депозит обновлён: {money(amount)}.")


async def prefix_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    if not msg or not chat:
        return
    if not await is_chat_admin(update, context):
        await msg.reply_text("Эта команда доступна только администраторам чата.")
        return
    if len(context.args) < 2:
        await msg.reply_text("Использование: /prefix @username текст")
        return
    target = get_user_by_username(chat.id, context.args[0])
    if not target:
        await msg.reply_text("Пользователь не найден в базе этого чата.")
        return
    value = " ".join(context.args[1:])[:40]
    with connect() as db:
        db.execute("UPDATE users SET prefix=? WHERE chat_id=? AND user_id=?",
                   (value, chat.id, target["user_id"]))
    await msg.reply_text("Префикс обновлён.")


async def interest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    if not msg or not chat:
        return
    if not await is_chat_admin(update, context):
        await msg.reply_text("Эта команда доступна только администраторам чата.")
        return
    if len(context.args) != 1:
        await msg.reply_text("Использование: /interest @username")
        return
    target = get_user_by_username(chat.id, context.args[0])
    if not target:
        await msg.reply_text("Пользователь не найден в базе этого чата.")
        return
    with connect() as db:
        db.execute("UPDATE users SET interested=interested+1 WHERE chat_id=? AND user_id=?",
                   (chat.id, target["user_id"]))
    await msg.reply_text("Счётчик заинтересовавшихся увеличен на 1.")



async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    if not msg or not chat or not context.args:
        if msg:
            await msg.reply_text("Использование: /history @username")
        return
    username = context.args[0].lstrip("@").casefold()
    with connect() as db:
        rows = db.execute("""
            SELECT delta, reason, giver_name, photo_file_id, created_at
            FROM reputation
            WHERE chat_id=? AND target_key=? AND photo_file_id IS NOT NULL
            ORDER BY id DESC LIMIT 10
        """, (chat.id, username)).fetchall()
    if not rows:
        await msg.reply_text(f"Для @{username} нет записей со скриншотами.")
        return
    await msg.reply_text(f"📷 Скриншоты репутации @{username} (последние 10):")
    for row in rows:
        caption = (
            f"{'＋1' if row['delta'] > 0 else '−1'} · {row['reason']}\n"
            f"От: {row['giver_name']}\nДата: {row['created_at'][:10]}"
        )
        try:
            await context.bot.send_photo(
                chat_id=chat.id, photo=row["photo_file_id"],
                caption=caption[:1000]
            )
        except Exception:
            logging.exception("Could not resend saved photo for %s", username)


async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    if not msg or not chat:
        return
    with connect() as db:
        rows = db.execute("""
            SELECT target_username, SUM(delta) AS score, COUNT(*) AS entries,
                   SUM(CASE WHEN delta=1 THEN 1 ELSE 0 END) AS positives,
                   SUM(CASE WHEN delta=-1 THEN 1 ELSE 0 END) AS negatives
            FROM reputation WHERE chat_id=?
            GROUP BY target_key ORDER BY score DESC, entries DESC LIMIT 10
        """, (chat.id,)).fetchall()
    if not rows:
        await msg.reply_text("Пока репутации нет.")
        return
    lines = [
        f"{i}. @{row['target_username']}\\n"
        f"Баланс: {row['score']} · +{row['positives'] or 0} / −{row['negatives'] or 0}"
        for i, row in enumerate(rows, 1)
    ]
    await msg.reply_text("🏆 Топ репутации\\n\\n" + "\\n\\n".join(lines))



async def edit_caption_or_text(message, text, reply_markup=None):
    try:
        if message.photo:
            await message.edit_caption(caption=text, parse_mode="HTML", reply_markup=reply_markup)
        else:
            await message.edit_text(text=text, parse_mode="HTML", reply_markup=reply_markup)
    except Exception:
        logging.exception("Could not edit deposit/profile message")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data or ""

    if data.startswith("profile:"):
        user_id = int(data.split(":", 1)[1])
        profile = get_profile(chat_id, user_id)
        if not profile:
            await query.message.reply_text("Профиль не найден.")
            return
        user = profile["user"]
        registered = user["registered_at"] or ""
        try:
            registered = datetime.fromisoformat(registered.replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            registered = registered[:10] if registered else "—"
        display_name = "@" + user["username"] if user["username"] else user["full_name"]
        info_text = (
            "<b>👤 Информация о пользователе</b>\n"
            f"<b>{escape(display_name)}</b>\n"
            f"<b>➕ Положительных репутаций: {profile['plus']}</b>\n"
            f"<b>➖ Отрицательных репутаций: {profile['minus']}</b>\n"
            f"<b>◇ Депозит: {money(float(user['deposit'] or 0))}</b>\n"
            f"<b>🌐 Регистрация: {registered}</b>\n"
            f"<b>🎓 Интересовались: {int(user['interested'] or 0)} раз</b>"
        )
        await query.message.reply_photo(
            photo=make_profile_card(profile),
            caption=info_text,
            parse_mode="HTML",
            reply_markup=profile_keyboard(user_id),
        )
        return

    if data.startswith("deposit:"):
        owner_id = int(data.split(":", 1)[1])
        if query.from_user.id != owner_id:
            await query.answer("Это меню доступно только владельцу профиля.", show_alert=True)
            return
        profile = get_profile(chat_id, owner_id)
        if not profile:
            await query.message.reply_text("Профиль не найден.")
            return
        await edit_caption_or_text(query.message, deposit_text(profile["user"]), deposit_keyboard())
        return

    if data == "deposit_back":
        user_id = query.from_user.id
        profile = get_profile(chat_id, user_id)
        if not profile:
            await query.message.reply_text("Профиль не найден.")
            return
        user = profile["user"]
        registered = user["registered_at"] or ""
        try:
            registered = datetime.fromisoformat(registered.replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            registered = registered[:10] if registered else "—"
        display_name = "@" + user["username"] if user["username"] else user["full_name"]
        info_text = (
            "<b>👤 Информация о пользователе</b>\n"
            f"<b>{escape(display_name)}</b>\n"
            f"<b>➕ Положительных репутаций: {profile['plus']}</b>\n"
            f"<b>➖ Отрицательных репутаций: {profile['minus']}</b>\n"
            f"<b>◇ Депозит: {money(float(user['deposit'] or 0))}</b>\n"
            f"<b>🌐 Регистрация: {registered}</b>\n"
            f"<b>🎓 Интересовались: {int(user['interested'] or 0)} раз</b>"
        )
        await edit_caption_or_text(query.message, info_text, profile_keyboard(user_id))
        return

    if data == "deposit_pay":
        context.user_data["awaiting_deposit_amount"] = True
        context.user_data["deposit_message_id"] = query.message.message_id
        await edit_caption_or_text(
            query.message,
            "💳 <b>Внести депозит</b>\n\n"
            f"Введите сумму депозита в {DEPOSIT_ASSET}.\n"
            f"Минимум: <b>{DEPOSIT_MIN:g}</b> {DEPOSIT_ASSET}\n"
            f"Максимум: <b>{DEPOSIT_MAX:g}</b> {DEPOSIT_ASSET}",
            InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="deposit_back")]]),
        )
        return

    if data == "deal:":
        await query.message.reply_text(
            "Кнопка сделки пока не проводит оплату и не удерживает деньги. "
            "После подтверждённой сделки администратор может добавить её командой "
            "/deal @username сумма."
        )


async def deposit_amount_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text or not context.user_data.get("awaiting_deposit_amount"):
        return
    text = msg.text.strip()
    if text.startswith("/"):
        return
    try:
        amount = parse_amount(text)
    except (ValueError, TypeError):
        await msg.reply_text(
            f"Введите корректную сумму от {DEPOSIT_MIN:g} до {DEPOSIT_MAX:g} {DEPOSIT_ASSET}.\n"
            "Например: 100 или 250.50"
        )
        return

    context.user_data.pop("awaiting_deposit_amount", None)
    user = update.effective_user
    chat = update.effective_chat
    username = user.username or ""
    try:
        invoice = await asyncio.to_thread(
            create_deposit_invoice, chat.id, user.id, username, amount
        )
    except Exception as exc:
        logging.exception("CryptoBot invoice creation failed")
        await msg.reply_text(
            "❌ Не удалось создать счёт в CryptoBot.\n\n"
            "Проверьте, что в настройках хостинга указан правильный "
            "CRYPTOBOT_API_TOKEN и что приложение создано в CryptoBot."
        )
        return

    invoice_url = (
        invoice.get("bot_invoice_url")
        or invoice.get("mini_app_invoice_url")
        or invoice.get("web_app_invoice_url")
    )
    if not invoice_url:
        logging.error("CryptoBot invoice has no payment URL: %r", invoice)
        await msg.reply_text("❌ CryptoBot не вернул ссылку на оплату. Проверьте логи бота.")
        return

    pay_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💳 Оплатить {amount:g} {DEPOSIT_ASSET}", url=invoice_url)],
        [InlineKeyboardButton("◀️ Назад", callback_data="deposit_back")],
    ])
    payment_text = (
        "💳 <b>Счёт на оплату</b>\n\n"
        f"Сумма: <b>{amount:g} {DEPOSIT_ASSET}</b>\n\n"
        "Нажмите кнопку ниже, чтобы открыть счёт CryptoBot."
    )
    await msg.reply_text(payment_text, parse_mode="HTML", reply_markup=pay_keyboard)


async def payment_polling_loop(app):
    while True:
        try:
            if CRYPTOBOT_API_TOKEN:
                invoices = await asyncio.to_thread(
                    cryptobot_request, "getInvoices", {"status": "paid", "count": 100}
                )
                for invoice in invoices or []:
                    try:
                        payload = json.loads(invoice.get("payload") or "{}")
                    except json.JSONDecodeError:
                        continue
                    if payload.get("type") != "deposit":
                        continue
                    chat_id = int(payload["chat_id"])
                    user_id = int(payload["user_id"])
                    amount = float(invoice.get("amount", 0))
                    asset = invoice.get("asset") or DEPOSIT_ASSET
                    paid_at = invoice.get("paid_at") or now_iso()
                    if not register_paid_invoice(invoice["invoice_id"], chat_id, user_id, amount, asset, paid_at):
                        continue
                    username = payload.get("username") or str(user_id)
                    notice = (
                        "💰 <b>Пользователь внес страховой депозит</b>\n\n"
                        f"Пользователь: @{escape(username.lstrip('@'))}\n"
                        f"Депозит: <b>{amount:g} {escape(asset)}</b>\n"
                        f"Дата вноса депозита: {escape(paid_at)}"
                    )
                    for target_chat in [str(chat_id)] + ADMIN_CHAT_IDS:
                        try:
                            await app.bot.send_message(chat_id=int(target_chat), text=notice, parse_mode="HTML")
                        except Exception:
                            logging.exception("Could not send deposit notice to %s", target_chat)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("CryptoBot payment polling failed")
        await asyncio.sleep(10)


async def app_post_init(app):
    if CRYPTOBOT_API_TOKEN:
        try:
            me = await asyncio.to_thread(cryptobot_request, "getMe")
            logging.info("CryptoBot API connected: %s", me)
        except Exception:
            logging.exception("CryptoBot API token check failed")
    app.bot_data["payment_task"] = asyncio.create_task(payment_polling_loop(app))


async def app_post_shutdown(app):
    task = app.bot_data.get("payment_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def register_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, chat = update.effective_user, update.effective_chat
    if user and chat and chat.type in ("group", "supergroup"):
        register_user(chat.id, user)


def main():
    init_db()
    app = (Application.builder().token(TOKEN)
           .post_init(app_post_init)
           .post_shutdown(app_post_shutdown)
           .build())
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler(["p", "profile"], profile_command))
    app.add_handler(CommandHandler("deal", deal_command))
    app.add_handler(CommandHandler("deposit", deposit_command))
    app.add_handler(CommandHandler("prefix", prefix_command))
    app.add_handler(CommandHandler("interest", interest_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("top", top_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^(чаты💎|поддержка|назад|👤 Профиль|💰 Депозит)$"), menu_text_handler), group=-2)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, deposit_amount_handler), group=-1)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, rep_message), group=0)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, register_messages), group=1)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
