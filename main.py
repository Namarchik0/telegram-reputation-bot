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
    """Render a high-resolution HUB-style profile card in a wide square format."""
    W, H = 1536, 1358
    base_color = (241, 235, 223)
    im = Image.new("RGB", (W, H), base_color)

    # Subtle warm marble grain, kept low-contrast so text remains readable.
    noise = Image.effect_noise((W, H), 13).convert("L")
    texture = Image.merge("RGB", (noise, noise, noise))
    texture = ImageOps.colorize(noise, black=(226, 217, 199), white=(255, 252, 244))
    im = Image.blend(im, texture, 0.24)
    d = ImageDraw.Draw(im)

    ink = (61, 59, 54)
    muted = (132, 119, 98)
    gold = (190, 158, 105)
    pale_gold = (222, 204, 169)
    green = (48, 145, 75)
    red = (194, 67, 53)
    panel = (247, 243, 235)

    # Outer frame and small decorative gold details.
    d.rounded_rectangle((22, 22, W - 22, H - 22), radius=42,
                        outline=(202, 185, 153), width=4)
    d.rounded_rectangle((35, 35, W - 35, H - 35), radius=36,
                        outline=(255, 253, 247), width=3)

    # Decorative chain-like rings in the upper-left corner.
    for cx, cy, r in [(35, 70, 112), (160, 54, 112), (280, 18, 112),
                      (390, -35, 112)]:
        d.ellipse((cx-r, cy-r, cx+r, cy+r), outline=(211, 205, 190), width=18)
        d.ellipse((cx-r+8, cy-r+8, cx+r-8, cy+r-8), outline=(250, 247, 238), width=5)

    # Banknote-inspired accent at the upper-right, abstract and decorative.
    d.rounded_rectangle((1110, -55, 1570, 165), radius=22,
                        fill=(224, 216, 199), outline=(194, 181, 155), width=4)
    d.rounded_rectangle((1130, -38, 1550, 145), radius=16,
                        outline=(248, 244, 233), width=3)
    d.ellipse((1270, -20, 1450, 150), outline=(190, 178, 154), width=4)
    d.text((1360, 54), "$100", font=load_font(58, True), fill=(139, 130, 112), anchor="mm")

    # Header.
    d.line((90, 118, 500, 118), fill=pale_gold, width=3)
    d.line((1035, 118, 1445, 118), fill=pale_gold, width=3)
    d.text((W // 2, 118), "HUB Reputation", font=load_font(48, True),
           fill=ink, anchor="mm")

    user = profile["user"]
    uname = "@" + user["username"] if user["username"] else user["full_name"]
    prefix = user["prefix"] or "НЕТ"

    def panel_box(box, radius=30):
        x0, y0, x1, y1 = box
        # Soft offset shadow + cream panel.
        d.rounded_rectangle((x0 + 7, y0 + 10, x1 + 7, y1 + 10),
                            radius=radius, fill=(211, 201, 184))
        d.rounded_rectangle(box, radius=radius, fill=panel,
                            outline=(215, 199, 170), width=3)
        d.rounded_rectangle((x0 + 10, y0 + 10, x1 - 10, y1 - 10),
                            radius=max(12, radius - 8), outline=(255, 252, 245), width=2)

    # Upper-left: account information.
    info_box = (165, 225, 835, 735)
    panel_box(info_box, 32)
    d.text((220, 285), "ИНФОРМАЦИЯ:", font=load_font(34, True), fill=ink)
    # Minimal round line icons.
    for cy, symbol in [(425, "@"), (510, "♙")]:
        d.ellipse((220, cy - 25, 270, cy + 25), outline=pale_gold, width=3)
        d.text((245, cy), symbol, font=load_font(27, True), fill=gold, anchor="mm")
    d.text((310, 425), uname, font=fit_text(d, uname, 475, 40, True, 22),
           fill=ink, anchor="lm")
    d.text((310, 510), "ПРЕФИКС:", font=load_font(32, True), fill=ink, anchor="lm")
    d.text((500, 510), prefix, font=fit_text(d, prefix, 270, 32, True, 20),
           fill=gold, anchor="lm")
    d.line((310, 555, 790, 555), fill=gold, width=2)

    # Upper-right: real Telegram profile photo if available, otherwise branded placeholder.
    photo_box = (860, 225, 1370, 735)
    panel_box(photo_box, 32)
    px0, py0, px1, py1 = 885, 250, 1345, 710
    if avatar_bytes:
        try:
            avatar = Image.open(BytesIO(avatar_bytes)).convert("RGB")
            avatar = ImageOps.fit(avatar, (px1-px0, py1-py0), method=Image.Resampling.LANCZOS)
            mask = Image.new("L", avatar.size, 0)
            md = ImageDraw.Draw(mask)
            md.rounded_rectangle((0, 0, avatar.width-1, avatar.height-1),
                                 radius=20, fill=255)
            im.paste(avatar, (px0, py0), mask)
            d = ImageDraw.Draw(im)
        except Exception:
            d.rounded_rectangle((px0, py0, px1, py1), radius=22,
                                fill=(231, 223, 208), outline=pale_gold, width=2)
            d.text(((px0+px1)//2, (py0+py1)//2), "HUB",
                   font=load_font(76, True), fill=gold, anchor="mm")
    else:
        d.rounded_rectangle((px0, py0, px1, py1), radius=22,
                            fill=(231, 223, 208), outline=pale_gold, width=2)
        d.ellipse((1030, 320, 1200, 490), outline=gold, width=5)
        d.text((1115, 405), "HUB", font=load_font(65, True), fill=ink, anchor="mm")
        d.text((1115, 555), "USER PROFILE", font=load_font(23, True),
               fill=muted, anchor="mm")

    # Reputation panel.
    rep_box = (165, 770, 1370, 1115)
    panel_box(rep_box, 32)
    d.text((220, 830), "РЕПУТАЦИЯ:", font=load_font(36, True), fill=ink)
    plus = int(profile["plus"])
    minus = int(profile["minus"])
    d.text((220, 950), f"+ {plus} ({money(plus * 3)})",
           font=load_font(40, True), fill=green)
    d.line((220, 995, 1305, 995), fill=gold, width=2)
    d.text((220, 1060), f"- {minus} ({money(minus * 3)})",
           font=load_font(40, True), fill=red)

    # Deposit + account statistics panel.
    stats_box = (165, 1150, 1370, 1300)
    panel_box(stats_box, 28)
    d.text((220, 1225), f"ДЕПОЗИТ: {money(float(user['deposit']))}",
           font=load_font(35, True), fill=ink, anchor="lm")
    d.text((850, 1225), f"СДЕЛОК: {profile['deal_count']}  ·  СУММА: {money(profile['deal_amount'])}",
           font=fit_text(d, f"СДЕЛОК: {profile['deal_count']}  ·  СУММА: {money(profile['deal_amount'])}",
                         470, 27, True, 18), fill=ink, anchor="lm")

    # Small decorative gold studs around the edges.
    for cx, cy in [(82, 450), (1450, 445), (75, 930), (1450, 1000), (95, 1240)]:
        d.ellipse((cx-22, cy-22, cx+22, cy+22), fill=(199, 164, 99),
                  outline=(247, 229, 184), width=4)
        d.ellipse((cx-8, cy-8, cx+8, cy+8), outline=(139, 107, 61), width=3)

    output = BytesIO()
    im.save(output, format="PNG", optimize=True)
    output.seek(0)
    output.name = "hub_profile.png"
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
    name = ("@" + user["username"]) if user["username"] else user["full_name"]
    avatar_bytes = await get_avatar_bytes(message.get_bot(), user_id)
    await message.reply_photo(
        photo=make_profile_card(profile, avatar_bytes),
        caption=(
            f"<b>👤 Информация о пользователе</b>\n"
            f"{escape(name)}\n"
            f"📈 Сделок: {profile['deal_count']} · Сумма: {money(profile['deal_amount'])}\n"
            f"◇ Депозит: {money(float(user['deposit']))}\n"
            f"🌐 Регистрация: {escape(user['registered_at'][:10])}\n"
            f"🎓 Интересовались: {user['interested']} раз"
        ),
        parse_mode="HTML",
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



async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not msg or not chat:
        return
    if user:
        register_user(chat.id, user)

    keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("чаты💎"), KeyboardButton("поддержка")]],
        resize_keyboard=True,
        is_persistent=True
    )
    support_link = InlineKeyboardMarkup([[
        InlineKeyboardButton("Поддержка · @luminbad", url=SUPPORT_URL)
    ]])
    caption = (
        "Добро пожаловать в бота репутации чата CALVINE\\n"
        "Пожалуйста выбери нужный раздел"
    )
    try:
        with open(START_IMAGE, "rb") as photo:
            await context.bot.send_photo(
                chat_id=chat.id,
                photo=photo,
                caption=caption,
                reply_markup=support_link
            )
    except FileNotFoundError:
        logging.exception("Start image not found: %s", START_IMAGE)
        await msg.reply_text(caption, reply_markup=support_link)

    await msg.reply_text("Выбери нужный раздел 👇", reply_markup=keyboard)


async def menu_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text:
        return
    text = msg.text.strip().casefold()

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
                await context.bot.send_photo(
                    chat_id=msg.chat_id,
                    photo=photo,
                    caption="Присоединяйся к чату CALVINE 💎",
                    reply_markup=inline
                )
        except FileNotFoundError:
            logging.exception("Welcome image not found: %s", WELCOME_IMAGE)
            await msg.reply_text(
                "Присоединяйся к чату CALVINE 💎",
                reply_markup=inline
            )
        await msg.reply_text("Чтобы вернуться в меню, нажми «назад».",
                             reply_markup=back_keyboard)

    elif text == "поддержка":
        back_keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton("назад")]],
            resize_keyboard=True,
            is_persistent=True
        )
        support_link = InlineKeyboardMarkup([[
            InlineKeyboardButton("Написать в поддержку · @luminbad", url=SUPPORT_URL)
        ]])
        await msg.reply_text(
            "Если нужна помощь, нажми кнопку ниже 👇",
            reply_markup=support_link
        )
        await msg.reply_text("Чтобы вернуться в меню, нажми «назад».",
                             reply_markup=back_keyboard)

    elif text == "назад":
        main_keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton("чаты💎"), KeyboardButton("поддержка")]],
            resize_keyboard=True,
            is_persistent=True
        )
        await msg.reply_text(
            "Добро пожаловать в меню CALVINE. Выбери нужный раздел 👇",
            reply_markup=main_keyboard
        )


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
        avatar_bytes = await get_avatar_bytes(context.bot, user_id)
        await query.message.reply_photo(
            photo=make_profile_card(profile, avatar_bytes),
            caption=f"Профиль пользователя {escape(profile['user']['full_name'])}",
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
