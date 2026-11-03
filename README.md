# HUB Reputation Bot

## Deploy on Render
1. Upload these files to your GitHub repository.
2. Create a Render Blueprint from the repository (or create a Python Worker).
3. Add `BOT_TOKEN` in Render Environment.
4. Keep the persistent disk mounted at `/var/data`.
5. Deploy.

## Commands
- `/p` — your profile
- `/p @username` — another member's profile
- `+реп @username reason` — add positive reputation
- `-реп @username reason` — add negative reputation
- `/deal @username 3.00 [note]` — add a completed deal (chat admins only)
- `/deposit @username 10.00` — set deposit (chat admins only)
- `/prefix @username text` — set profile prefix (chat admins only)
- `/interest @username` — increment interested counter (chat admins only)

Members are added to the local database when they send a message in the group.
The bot can only show profiles for users it has seen in that group.

## Important limitations
This is a locally managed profile system. It does not connect to HUB or any payment provider.
The “Сделка 0%” button is informational only; it does not create escrow, process payments,
verify transactions, or guarantee a deal. Administrators must add completed deals manually.
The existing SQLite database is retained if its `reputation` table has the original schema.


## Chat menu
- `/start` shows the persistent `чаты💎` keyboard button.
- Pressing it switches the keyboard to `назад` and sends a welcome image.
- The welcome image message has an inline URL button `Залетай🥵` linking to https://t.me/calvinhodnik.
- Pressing `назад` restores the `чаты💎` button.


## CALVINE start menu
- `/start` sends the supplied CK image and the welcome text:
  "Добро пожаловать в бота репутации чата CALVINE / Пожалуйста выбери нужный раздел".
- Reply keyboard contains `чаты💎` and `поддержка`.
- `чаты💎` opens the chat image with the `Залетай🥵` URL button.
- `поддержка` displays a direct Telegram URL button to `https://t.me/luminbad`.
- `назад` returns to the two-button main menu.

The `welcome_start.jpeg` file must stay beside `main.py`.


### Profile by replying to a message
In a group, reply to any member's message with `/p` (or `/profile`).
The bot opens the profile of the person whose message you replied to.
`/p @username` continues to work, and `/p` without a reply opens your own profile.


Для динамической карточки профиля `/p` в корне проекта должен находиться файл `profile_template.jpeg` (фон карточки CALVINE). Не переименовывайте его.
