import os
import re
import math
import sqlite3
import logging
from io import BytesIO
from datetime import datetime, timezone
from html import escape

from PIL import Image, ImageDraw, ImageFont
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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


def make_profile_card(profile):
    W, H = 1080, 760
    im = Image.new("RGB", (W, H), (239, 234, 220))
    d = ImageDraw.Draw(im)

    # Warm paper-like background and subtle decorative framing.
    for y in range(H):
        shade = int(247 - 10 * y / H)
        d.line((0, y, W, y), fill=(shade, shade - 3, shade - 12))
    d.rounded_rectangle((22, 22, W - 22, H - 22), radius=34,
                        outline=(205, 190, 164), width=3)

    ink = (67, 62, 53)
    muted = (125, 115, 99)
    green = (42, 145, 72)
    red = (199, 75, 58)
    panel = (250, 247, 239)
    d.text((W // 2, 65), "HUB Reputation", font=load_font(34, True),
           fill=ink, anchor="mm")
    d.line((70, 105, W - 70, 105), fill=(211, 198, 174), width=2)

    user = profile["user"]
    uname = "@" + user["username"] if user["username"] else user["full_name"]
    d.rounded_rectangle((60, 135, 1020, 300), radius=24,
                        fill=panel, outline=(220, 207, 184), width=3)
    d.text((95, 170), "ИНФОРМАЦИЯ О ПОЛЬЗОВАТЕЛЕ",
           font=load_font(22, True), fill=muted)
    d.text((95, 220), uname, font=fit_text(d, uname, 860, 42, True), fill=ink)
    prefix = "ПРЕФИКС: " + (user["prefix"] or "НЕТ")
    d.text((95, 270), prefix, font=load_font(23), fill=muted)

    d.rounded_rectangle((60, 325, 1020, 500), radius=24,
                        fill=panel, outline=(220, 207, 184), width=3)
    d.text((95, 365), "РЕПУТАЦИЯ", font=load_font(23, True), fill=ink)
    d.text((95, 425), f"+ {profile['plus']} ({money(profile['plus'] * 3)})",
           font=load_font(27, True), fill=green)
    d.text((550, 425), f"− {profile['minus']} ({money(profile['minus'] * 3)})",
           font=load_font(27, True), fill=red)
    d.line((95, 465, 985, 465), fill=(220, 207, 184), width=2)

    d.rounded_rectangle((60, 525, 1020, 690), radius=24,
                        fill=panel, outline=(220, 207, 184), width=3)
    d.text((95, 560), f"СДЕЛОК: {profile['deal_count']}",
           font=load_font(25, True), fill=ink)
    d.text((550, 560), f"СУММА: {money(profile['deal_amount'])}",
           font=load_font(25, True), fill=ink)
    d.text((95, 620), f"ДЕПОЗИТ: {money(float(user['deposit']))}",
           font=load_font(25, True), fill=ink)
    try:
        date_text = datetime.fromisoformat(user["registered_at"]).astimezone().strftime("%d.%m.%Y")
    except Exception:
        date_text = user["registered_at"][:10]
    d.text((550, 620), f"РЕГИСТРАЦИЯ: {date_text}",
           font=load_font(22, True), fill=ink)
    d.text((W - 95, 720), f"ИНТЕРЕСОВАЛИСЬ: {user['interested']} РАЗ",
           font=load_font(18, True), fill=muted, anchor="rm")

    output = BytesIO()
    im.save(output, format="PNG")
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
    await message.reply_photo(
        photo=make_profile_card(profile),
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
    if update.effective_user and update.effective_chat:
        register_user(update.effective_chat.id, update.effective_user)
    await update.effective_message.reply_text(
        "<b>HUB Reputation запущен.</b>\n\n"
        "Профиль: /p или /p @username\n"
        "Поставить плюс: +реп @username причина\n"
        "Поставить минус: -реп @username причина\n\n"
        "<b>Команды администраторов:</b>\n"
        "/deal @username сумма [описание]\n"
        "/deposit @username сумма\n"
        "/prefix @username текст\n"
        "/interest @username",
        parse_mode="HTML"
    )


async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, chat, sender = update.effective_message, update.effective_chat, update.effective_user
    if not msg or not chat or not sender:
        return
    register_user(chat.id, sender)
    if context.args:
        target = get_user_by_username(chat.id, context.args[0])
        if not target:
            await msg.reply_text("Не нашёл пользователя. Он должен хотя бы раз написать в этом чате.")
            return
        user_id = target["user_id"]
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
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, rep_message), group=0)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, register_messages), group=1)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
