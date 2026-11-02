import os
import re
from html import escape
import sqlite3
import logging
import threading
from io import BytesIO
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timezone

from PIL import Image, ImageDraw, ImageFont

from telegram import Update
from telegram.ext import (
    Application, MessageHandler, CommandHandler, ContextTypes, filters
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
TOKEN = os.environ.get("BOT_TOKEN")
DB_PATH = os.environ.get("DB_PATH", "/tmp/reputation.db")
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
        db.execute("CREATE INDEX IF NOT EXISTS idx_rep_giver ON reputation(chat_id, giver_id, id)")

def display_user(user):
    if user.username:
        return "@" + user.username
    return user.full_name or str(user.id)

def reputation_bar(positives, negatives, width=8):
    """Compact, gapless eight-part bar: green for positive ratings, red for negative."""
    total = positives + negatives
    if total == 0:
        return "▫️" * width
    green = round(width * positives / total)
    green = max(0, min(width, green))
    red = width - green
    # No spaces: visually reads as one continuous horizontal bar in Telegram.
    return "🟩" * green + "🟥" * red

def _font(size, bold=False):
    candidates = (
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"] if bold else
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"]
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            pass
    return ImageFont.load_default()


def make_rep_card(username, positives, negatives, total):
    """Create a dynamic reputation banner PNG for the /p profile command."""
    W, H = 1000, 620
    im = Image.new("RGB", (W, H), (13, 16, 23))
    d = ImageDraw.Draw(im)
    white = (248, 250, 255)
    muted = (157, 167, 185)
    green = (25, 218, 91)
    red = (248, 42, 56)

    # Header banner with green and red halves.
    d.rounded_rectangle((28, 24, 972, 205), radius=44, fill=(28, 33, 43), outline=(65, 74, 87), width=3)
    d.rounded_rectangle((38, 34, 500, 195), radius=34, fill=(7, 105, 51))
    d.rectangle((465, 34, 535, 195), fill=(86, 48, 39))
    d.rounded_rectangle((500, 34, 962, 195), radius=34, fill=(139, 14, 31))
    d.rectangle((500, 34, 500, 195), fill=(105, 45, 34))
    # Plus/minus sides and numbered markers.
    d.ellipse((58, 76, 142, 160), fill=(28, 229, 99), outline=(151, 255, 185), width=4)
    d.text((100, 117), "+", font=_font(70, True), fill=(5, 52, 24), anchor="mm")
    d.ellipse((858, 76, 942, 160), fill=(255, 54, 67), outline=(255, 159, 165), width=4)
    d.text((900, 117), "−", font=_font(70, True), fill=(91, 8, 18), anchor="mm")
    for i, val in enumerate((1, 2, 3, 4)):
        y = 48 + i * 37
        d.rounded_rectangle((160, y, 270, y + 29), radius=14, fill=(10, 139, 62), outline=(65, 238, 127), width=2)
        d.text((215, y + 14), f"+{val}", font=_font(22, True), fill=white, anchor="mm")
        d.rounded_rectangle((730, y, 840, y + 29), radius=14, fill=(174, 18, 34), outline=(255, 94, 103), width=2)
        d.text((785, y + 14), f"−{val}", font=_font(22, True), fill=white, anchor="mm")
    # Central star badge.
    cx, cy, outer, inner = 500, 114, 62, 28
    pts = []
    for j in range(10):
        import math
        a = -math.pi / 2 + j * math.pi / 5
        r = outer if j % 2 == 0 else inner
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    d.polygon(pts, fill=(255, 190, 35), outline=(255, 229, 126))
    d.ellipse((480, 94, 520, 134), fill=(255, 235, 167))

    d.text((W // 2, 250), f"РЕПУТАЦИЯ @{username}", font=_font(42, True), fill=white, anchor="mm")
    # Eight contiguous rounded segments; green share represents plus ratio.
    x0, y0, x1, y1 = 45, 305, 955, 382
    d.rounded_rectangle((x0, y0, x1, y1), radius=39, fill=(39, 45, 57), outline=(69, 78, 93), width=3)
    inner_x0, inner_x1 = x0 + 9, x1 - 9
    inner_y0, inner_y1 = y0 + 9, y1 - 9
    n = positives + negatives
    green_count = 4 if n == 0 else max(0, min(8, round(8 * positives / n)))
    usable = inner_x1 - inner_x0
    gap = 4
    seg_w = (usable - gap * 7) / 8
    for i in range(8):
        left = int(inner_x0 + i * (seg_w + gap))
        right = int(inner_x0 + i * (seg_w + gap) + seg_w)
        color = green if i < green_count else red
        d.rounded_rectangle((left, inner_y0, right, inner_y1), radius=25, fill=color)
    if n == 0:
        status = "ПОКА НЕТ ОЦЕНОК"
    elif positives > negatives:
        status = f"ПОЛОЖИТЕЛЬНАЯ РЕПУТАЦИЯ · {green_count}/8"
    elif negatives > positives:
        status = f"ОТРИЦАТЕЛЬНАЯ РЕПУТАЦИЯ · {8-green_count}/8"
    else:
        status = "НЕЙТРАЛЬНАЯ РЕПУТАЦИЯ · 4/8"
    d.text((W // 2, 420), status, font=_font(25, True), fill=white, anchor="mm")
    d.line((55, 460, 945, 460), fill=(57, 64, 78), width=2)
    d.ellipse((150, 490, 204, 544), fill=green)
    d.text((177, 517), "+", font=_font(42, True), fill=(4, 55, 24), anchor="mm")
    d.text((225, 517), f"+{positives} плюсов", font=_font(28, True), fill=white, anchor="lm")
    d.ellipse((560, 490, 614, 544), fill=red)
    d.text((587, 517), "−", font=_font(42, True), fill=(89, 8, 17), anchor="mm")
    d.text((635, 517), f"−{negatives} минусов", font=_font(28, True), fill=white, anchor="lm")
    d.text((W // 2, 585), f"Итоговый баланс: {total:+d}", font=_font(24, True), fill=muted, anchor="mm")
    output = BytesIO()
    im.save(output, format="PNG")
    output.seek(0)
    output.name = "reputation.png"
    return output


def parse_rep(text):
    # Supports +реп @username reason, -реп @username reason, and /rep+ /rep- variants.
    if not text:
        return None
    pattern = r"^\s*([+-])\s*реп\s+@([A-Za-z0-9_]{5,32})(?:\s+([\s\S]*))?\s*$"
    m = re.match(pattern, text, re.IGNORECASE)
    if not m:
        return None
    delta = 1 if m.group(1) == "+" else -1
    username = m.group(2)
    reason = (m.group(3) or "").strip()
    return delta, username, reason

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not msg or not user or not chat or chat.type not in ("group", "supergroup"):
        return

    caption = msg.caption or ""
    body = msg.text or ""
    parsed = parse_rep(caption) or parse_rep(body)
    if not parsed:
        return

    delta, target_username, reason = parsed
    target_key = target_username.casefold()
    giver_username = user.username
    giver_name = user.full_name or display_user(user)

    if giver_username and giver_username.casefold() == target_key:
        await msg.reply_text("Нельзя поставить репутацию самому себе.")
        return

    photo_file_id = None
    if msg.photo:
        photo_file_id = msg.photo[-1].file_id
    elif msg.reply_to_message and msg.reply_to_message.photo:
        photo_file_id = msg.reply_to_message.photo[-1].file_id

    if not reason:
        reason = "Причина не указана"

    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect() as db:
        db.execute("""
            INSERT INTO reputation
            (chat_id, giver_id, giver_name, giver_username, target_key,
             target_username, delta, reason, photo_file_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            chat.id, user.id, giver_name, giver_username, target_key,
            target_username, delta, reason, photo_file_id, created_at
        ))
        total = db.execute(
            "SELECT COALESCE(SUM(delta), 0) FROM reputation WHERE chat_id=? AND target_key=?",
            (chat.id, target_key)
        ).fetchone()[0]
        positives = db.execute(
            "SELECT COUNT(*) FROM reputation WHERE chat_id=? AND target_key=? AND delta=1",
            (chat.id, target_key)
        ).fetchone()[0]
        negatives = db.execute(
            "SELECT COUNT(*) FROM reputation WHERE chat_id=? AND target_key=? AND delta=-1",
            (chat.id, target_key)
        ).fetchone()[0]

    sign = "+" if delta > 0 else "−"
    photo_note = "\n📷 Скриншот сохранён в истории." if photo_file_id else "\n⚠️ Фото к записи не приложено."
    bar = reputation_bar(positives, negatives)
    await msg.reply_text(
        f"<b>⭐ Репутация {sign}1 для @{escape(target_username)}\n"
        f"📝 Причина: {escape(reason)}\n"
        f"👤 От: {escape(display_user(user))}\n"
        f"📊 Баланс: {bar}\n"
        f"Плюсов: {positives} · Минусов: {negatives} · Итог: {total}"
        f"{photo_note}</b>",
        parse_mode="HTML",
    )

async def rep_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    if context.args:
        username = context.args[0].lstrip("@").casefold()
    elif update.effective_user and update.effective_user.username:
        username = update.effective_user.username.casefold()
    else:
        await msg.reply_text("<b>Использование: /p @username</b>", parse_mode="HTML")
        return

    with connect() as db:
        rows = db.execute("""
            SELECT delta, reason, giver_name, giver_username, photo_file_id, created_at
            FROM reputation WHERE chat_id=? AND target_key=? ORDER BY id DESC LIMIT 10
        """, (chat.id, username)).fetchall()
        total = db.execute(
            "SELECT COALESCE(SUM(delta), 0) FROM reputation WHERE chat_id=? AND target_key=?",
            (chat.id, username)
        ).fetchone()[0]
        plus = db.execute(
            "SELECT COUNT(*) FROM reputation WHERE chat_id=? AND target_key=? AND delta=1",
            (chat.id, username)
        ).fetchone()[0]
        minus = db.execute(
            "SELECT COUNT(*) FROM reputation WHERE chat_id=? AND target_key=? AND delta=-1",
            (chat.id, username)
        ).fetchone()[0]

    if not rows:
        await msg.reply_text(f"<b>Для @{escape(username)} записей пока нет.</b>", parse_mode="HTML")
        return
    card = make_rep_card(username, plus, minus, total)
    await msg.reply_photo(
        photo=card,
        caption=(f"<b>⭐ Репутация @{escape(username)}\n"
                 f"Итог: {total:+d} · Плюсов: {plus} · Минусов: {minus}\n"
                 "История последних записей доступна командой /history @username.</b>"),
        parse_mode="HTML",
    )
    history_lines = "\n".join(
        f"{'+' if r['delta'] > 0 else '−'}1 · {escape(r['giver_name'])}"
        f"{' (@' + escape(r['giver_username']) + ')' if r['giver_username'] else ''}\n"
        f"Причина: {escape(r['reason'])}\n"
        f"Дата: {escape(r['created_at'][:10])}"
        for r in rows
    )
    await msg.reply_text(f"<b>Последние записи:\n{history_lines}</b>", parse_mode="HTML")

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat or not context.args:
        if msg:
            await msg.reply_text("<b>Использование: /history @username</b>", parse_mode="HTML")
        return
    username = context.args[0].lstrip("@").casefold()
    with connect() as db:
        rows = db.execute("""
            SELECT delta, reason, giver_name, photo_file_id, created_at
            FROM reputation WHERE chat_id=? AND target_key=? AND photo_file_id IS NOT NULL
            ORDER BY id DESC LIMIT 10
        """, (chat.id, username)).fetchall()
    if not rows:
        await msg.reply_text(f"<b>Для @{escape(username)} нет записей со скриншотами.</b>", parse_mode="HTML")
        return
    await msg.reply_text(f"<b>📷 Скриншоты репутации @{escape(username)} (последние 10):</b>", parse_mode="HTML")
    for r in rows:
        caption = f"<b>{'＋1' if r['delta'] > 0 else '−1'} · {escape(r['reason'])}\nОт: {escape(r['giver_name'])}\nДата: {escape(r['created_at'][:10])}</b>"
        try:
            await context.bot.send_photo(chat_id=chat.id, photo=r["photo_file_id"], caption=caption[:1000], parse_mode="HTML")
        except Exception:
            logging.exception("Could not resend saved photo for %s", username)
            await msg.reply_text("Не удалось повторно отправить один из скриншотов.")

async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
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
        await msg.reply_text("<b>Пока репутации нет.</b>", parse_mode="HTML")
        return
    lines = [
        f"{i}. @{escape(r['target_username'])}\n{reputation_bar(r['positives'] or 0, r['negatives'] or 0)}\n"
        f"Баланс: {r['score']} · +{r['positives'] or 0} / −{r['negatives'] or 0}"
        for i, r in enumerate(rows, 1)
    ]
    await msg.reply_text("<b>🏆 Топ репутации\n\n" + "\n".join(lines) + "</b>", parse_mode="HTML")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_message:
        await update.effective_message.reply_text(
            "<b>Бот репутации готов.\n\n"
            "Фото с подписью: +реп @username причина\n"
            "Минус: -реп @username причина\n"
            "Профиль: /p @username\n"
            "Скриншоты: /history @username\n"
            "Топ: /top\n\n"
            "Команды репутации работают в группах.</b>",
            parse_mode="HTML",
        )

def start_health_server():
    # Render Web Service requires an HTTP port. This endpoint only confirms the bot process is alive.
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Telegram reputation bot is running")
        def log_message(self, format, *args):
            return

    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

def main():
    start_health_server()
    init_db()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("p", rep_command))
    app.add_handler(CommandHandler("rep", rep_command))  # legacy alias
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("top", top_command))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
