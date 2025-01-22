# Telegram Бот для Транскрибации Аудио

Telegram бот для преобразования голосовых и аудио сообщений в текст с умным форматированием.

## Возможности

- Асинхронная обработка нескольких запросов
- Поддержка форматов: mp3, wav, ogg, m4a
- Умное форматирование текста
- Отображение прогресса обработки

## Установка через Docker

1. Создайте .env файл:

```env
BOT_TOKEN=ваш_токен_бота
FAL_KEY=ваш_ключ_fal
OPENROUTER_API_KEY=ваш_ключ_openrouter
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
TRANSFER_SH_HOST=https://transfer.sh
TRANSFER_SH_LOGIN=ваш_логин_transfer.sh (опционально)
TRANSFER_SH_PASSWORD=ваш_пароль_transfer.sh (опционально)
```

2. Запустите через Docker:

```bash
docker-compose up -d
```

## Обычная установка

1. Установите зависимости:

```bash
pip install -r requirements.txt
```

2. Запустите бота:

```bash
python bot/bot.py
```

## Использование

1. Отправьте `/start` для начала работы
2. Отправьте голосовое сообщение или аудио файл
3. Дождитесь обработки и получите текст

## Технологии

- aiogram 3.x
- FAL AI (транскрибация)
- OpenRouter (Gemini)
- transfer.sh
