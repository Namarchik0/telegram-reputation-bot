import os
import re
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
    await msg.reply_text(
        f"{'🟢' if delta > 0 else '🔴'} Репутация {sign}1 для @{target_username}\n"
        f"📝 Причина: {reason}\n"
        f"👤 От: {display_user(user)}\n"
        f"📊 Итог: {total} (плюсов: {positives}, минусов: {negatives})"
        f"{photo_note}"
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
        await msg.reply_text("Использование: /rep @username")
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
        await msg.reply_text(f"Для @{username} записей пока нет.")
        return
    await msg.reply_text(
        f"📋 Репутация @{username}\n"
        f"⭐ Итог: {total} | 🟢 {plus} | 🔴 {minus}\n\n"
        "Последние записи:\n" +
        "\n".join(
            f"{'+' if r['delta'] > 0 else '−'}1 · {r['giver_name']}"
            f"{' (@' + r['giver_username'] + ')' if r['giver_username'] else ''}\n"
            f"Причина: {r['reason']}\n"
            f"Дата: {r['created_at'][:10]}"
            for r in rows
        ) +
        "\n\nЧтобы открыть приложенный скриншот, используй /history @username."
    )

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat or not context.args:
        if msg:
            await msg.reply_text("Использование: /history @username")
        return
    username = context.args[0].lstrip("@").casefold()
    with connect() as db:
        rows = db.execute("""
            SELECT delta, reason, giver_name, photo_file_id, created_at
            FROM reputation WHERE chat_id=? AND target_key=? AND photo_file_id IS NOT NULL
            ORDER BY id DESC LIMIT 10
        """, (chat.id, username)).fetchall()
    if not rows:
        await msg.reply_text(f"Для @{username} нет записей со скриншотами.")
        return
    await msg.reply_text(f"📷 Скриншоты репутации @{username} (последние 10):")
    for r in rows:
        caption = f"{'🟢 +1' if r['delta'] > 0 else '🔴 −1'} · {r['reason']}\nОт: {r['giver_name']}\nДата: {r['created_at'][:10]}"
        try:
            await context.bot.send_photo(chat_id=chat.id, photo=r["photo_file_id"], caption=caption[:1000])
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
            SELECT target_username, SUM(delta) AS score, COUNT(*) AS entries
            FROM reputation WHERE chat_id=?
            GROUP BY target_key ORDER BY score DESC, entries DESC LIMIT 10
        """, (chat.id,)).fetchall()
    if not rows:
        await msg.reply_text("Пока репутации нет.")
        return
    lines = [f"{i}. @{r['target_username']} — {r['score']} ({r['entries']} записей)"
             for i, r in enumerate(rows, 1)]
    await msg.reply_text("🏆 Топ репутации\n\n" + "\n".join(lines))

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_message:
        await update.effective_message.reply_text(
            "Бот репутации готов.\n\n"
            "Фото с подписью: +реп @username причина\n"
            "Минус: -реп @username причина\n"
            "Профиль: /rep @username\n"
            "Скриншоты: /history @username\n"
            "Топ: /top\n\n"
            "Команды репутации работают в группах."
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
    app.add_handler(CommandHandler("rep", rep_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("top", top_command))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
