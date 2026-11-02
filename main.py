import os
import re
from html import escape
import sqlite3
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timezone

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
    bar = reputation_bar(plus, minus)
    history_lines = "\n".join(
        f"{'+' if r['delta'] > 0 else '−'}1 · {escape(r['giver_name'])}"
        f"{' (@' + escape(r['giver_username']) + ')' if r['giver_username'] else ''}\n"
        f"Причина: {escape(r['reason'])}\n"
        f"Дата: {escape(r['created_at'][:10])}"
        for r in rows
    )
    await msg.reply_text(
        f"<b>📋 Репутация @{escape(username)}\n"
        f"{bar}\n"
        f"⭐ Итог: {total} | Плюсов: {plus} | Минусов: {minus}\n\n"
        f"Последние записи:\n{history_lines}\n\n"
        "Чтобы открыть приложенные скриншоты, используй /history @username.</b>",
        parse_mode="HTML",
    )

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
