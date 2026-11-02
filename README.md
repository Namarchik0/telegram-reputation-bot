# Telegram Reputation Bot (iPhone-friendly deployment)

## Features
- Reads `+реп @username reason` and `-реп @username reason`.
- Works when the command is the caption of a photo/screenshot.
- Stores each entry, sender, reason, date, and Telegram photo file_id in SQLite.
- `/rep @username` shows score and latest entries.
- `/history @username` resends saved screenshots.
- `/top` shows top usernames in the current group.

## Important limitations
- This starter tracks recipients by username text, because Telegram bots cannot resolve every arbitrary `@username` into a Telegram user ID from a group caption alone. Usernames can change; stable identity support needs users to interact with the bot or be mentioned as a Telegram entity.
- Add anti-spam, admin moderation, and data-retention rules before using in a large public group.
- Photo file IDs are stored in the database; the image itself remains on Telegram. Keep the database disk persistent.
- The bot only processes group/supergroup messages.
- Set BotFather `/setprivacy` to Disable, then re-add the bot to the group (or update its membership) so it can read regular group messages.

## Deploy with Render from iPhone
1. Create a GitHub repository in Safari.
2. Upload `main.py`, `requirements.txt`, and `render.yaml` to the repository.
3. In Render, create a new Blueprint from that GitHub repository.
4. Add `BOT_TOKEN` as a secret environment variable using the token from @BotFather. Never commit the token to GitHub.
5. Deploy the worker. Keep the persistent disk mounted at `/var/data`.
6. Add the bot to the Telegram group and disable Privacy Mode via @BotFather `/setprivacy` -> select bot -> Disable, then re-add it to the group.
7. Test with a photo caption: `+реп @username Сделал хорошо`.

## Local environment
- `BOT_TOKEN`: token from @BotFather
- `DB_PATH`: database path (default `/var/data/reputation.db`)
