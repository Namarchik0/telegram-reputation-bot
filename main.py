import os
import re
import math
import sqlite3
import logging
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
    """Render live profile values over the user's exact CALVINE artwork."""
    template_path = os.path.join(BASE_DIR, "profile_template.jpeg")
    im = Image.open(template_path).convert("RGB")
    d = ImageDraw.Draw(im)
    user = profile["user"]
    uname = "@" + user["username"] if user["username"] else user["full_name"]
    prefix = user["prefix"] or "НЕТ"
    plus = int(profile["plus"])
    minus = int(profile["minus"])
    deposit = float(user["deposit"] or 0)

    serif_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSerif-Regular.ttf",
    ]
    bold_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSerif-Bold.ttf",
    ]

    def serif(size, bold=False):
        for path in (bold_paths if bold else serif_paths):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                pass
        return ImageFont.load_default()

    def fit_serif(text, max_width, size, bold=True, minimum=18):
        while size > minimum:
            f = serif(size, bold)
            if d.textbbox((0, 0), text, font=f)[2] <= max_width:
                return f
            size -= 1
        return serif(minimum, bold)

    def usd(value):
        return f"{value:,.2f}$"

    # Repaint only the original value lines, leaving all printed labels and artwork.
    # Coordinates are for the supplied 784x1168 original template.
    cream_info = (232, 215, 187)
    cream_stats = (226, 215, 190)
    d.rectangle((145, 466, 388, 520), fill=cream_info)   # username value
    d.rectangle((116, 557, 376, 623), fill=cream_info)   # prefix value line
    d.rectangle((177, 774, 618, 850), fill=cream_stats)  # reputation values
    d.rectangle((234, 908, 552, 954), fill=cream_stats)  # deposit value

    d.text((265, 494), uname,
           font=fit_serif(uname, 225, 31, True, 17),
           fill=(105, 72, 22), anchor="mm")

    prefix_text = f"ПРЕФИКС: {prefix}"
    d.text((246, 589), prefix_text,
           font=fit_serif(prefix_text, 245, 30, True, 17),
           fill=(105, 72, 22), anchor="mm")

    plus_text = f"+ {plus} ({usd(plus * 3)})"
    minus_text = f"− {minus} ({usd(minus * 3)})"
    d.text((295, 813), plus_text,
           font=fit_serif(plus_text, 205, 29, True, 17),
           fill=(35, 91, 43), anchor="mm")
    d.text((493, 813), minus_text,
           font=fit_serif(minus_text, 205, 29, True, 17),
           fill=(125, 35, 29), anchor="mm")

    deposit_text = f"ДЕПОЗИТ: {usd(deposit)}"
    d.text((393, 932), deposit_text,
           font=fit_serif(deposit_text, 295, 31, True, 17),
           fill=(45, 38, 29), anchor="mm")

    output = BytesIO()
    im.save(output, format="JPEG", quality=95, optimize=True)
    output.seek(0)
    output.name = "calvine_profile.jpg"
    return output


def profile_keyboard(user_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👤 Перейти в профиль", callback_data=f"profile:{user_id}"),
            InlineKeyboardButton("🤝 Сделка 0%", callback_data=f"deal:{user_id}")
        ]
    ])


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
        registered = datetime.fromisoformat(registered.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        registered = registered[:10] if registered else "—"

    # Keep the descriptive profile text below the image as in the previous layout.
    await message.reply_photo(photo=make_profile_card(profile))
    info_text = (
        "👤 **Информация о пользователе**\\n"
        f"@{user['username']}" if user["username"] else
        "👤 **Информация о пользователе**\\n" + user["full_name"]
    )
    info_text += (
        f"\\n📈 Сделок: {profile['deal_count']} · Сумма: {money(profile['deal_amount'])}"
        f"\\n◇ Депозит: {money(float(user['deposit'] or 0))}"
        f"\\n🌐 Регистрация: {registered}"
        f"\\n🎓 Интересовались: {int(user['interested'] or 0)} раз"
    )
    await message.reply_text(
        info_text,
        parse_mode="Markdown",
        reply_markup=profile_keyboard(user_id)
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
        [[KeyboardButton("чаты💎"), KeyboardButton("поддержка")]],
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
        db.execute("UPDATE users SET deposit=? WHERE chat_id=? AND user_id=?",
                   (amount, chat.id, target["user_id"]))
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
        await query.message.reply_photo(
            photo=make_profile_card(profile),
            reply_markup=profile_keyboard(user_id)
        )
    elif data.startswith("deal:"):
        await query.message.reply_text(
            "Кнопка сделки пока не проводит оплату и не удерживает деньги. "
            "После подтверждённой сделки администратор может добавить её командой "
            "/deal @username сумма."
        )


async def register_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, chat = update.effective_user, update.effective_chat
    if user and chat and chat.type in ("group", "supergroup"):
        register_user(chat.id, user)


def main():
    init_db()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler(["p", "profile"], profile_command))
    app.add_handler(CommandHandler("deal", deal_command))
    app.add_handler(CommandHandler("deposit", deposit_command))
    app.add_handler(CommandHandler("prefix", prefix_command))
    app.add_handler(CommandHandler("interest", interest_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("top", top_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^(чаты💎|поддержка|назад)$"), menu_text_handler), group=-1)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, rep_message), group=0)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, register_messages), group=1)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
