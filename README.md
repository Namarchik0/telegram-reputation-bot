# CALVINE bot

Версия с депозитом через Crypto Pay API.

## Переменные окружения

- `BOT_TOKEN` — токен Telegram-бота.
- `CRYPTOBOT_API_TOKEN` — API Token из @CryptoBot → Crypto Pay → My Apps → Create App.
- `ADMIN_CHAT_ID` — Telegram ID администратора; можно указать несколько ID через запятую.
- `DEPOSIT_ASSET` — по умолчанию `USDT`.
- `DEPOSIT_MIN` — минимум депозита, по умолчанию `1`.
- `DEPOSIT_MAX` — максимум депозита, по умолчанию `1000000`.
- `DB_PATH` — путь к SQLite, по умолчанию `/var/data/reputation.db`.

## Логика

После `/start` в меню есть `👤 Профиль` и `💰 Депозит`.

Пользователь нажимает `💰 Депозит` → `💳 Внести депозит` → вводит сумму → бот создаёт персональный Crypto Pay invoice на эту сумму.

Оплата проверяется через `getInvoices` каждые 10 секунд. Один invoice учитывается только один раз.

Не добавляйте `CRYPTOBOT_API_TOKEN` в GitHub.
