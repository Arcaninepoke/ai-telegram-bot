# AI Telegram Bot

Асинхронный интеллектуальный Telegram-бот для групповых чатов и личных сообщений на базе LLM (OpenRouter / LM Studio).

## Зависимости
* **Python 3.10+**
* **Фреймворк:** aiogram 3.x
* **БД:** SQLite + SQLAlchemy 2.0 (aiosqlite)
* **LLM API:** openai
* **Дополнительно:** thefuzz

## Установка и запуск

**1. Клонирование репозитория**
```bash
git clone https://github.com/Arcaninepoke/ai-telegram-bot
cd ai-telegram-bot
```

**2. Виртуальное окружение и зависимости**
```bash
python -m venv venv
source venv/bin/activate  # Для Windows: venv\Scripts\activate
pip install -r requirements.txt
```

**3. Конфигурация**
Создайте файл .env на основе шаблона:
```bash
cp .env.example .env
```

Отредактируйте .env, указав ваши токены:
```ini
BOT_TOKEN=ваш_токен_telegram_бота
ADMIN_ID=ваш_telegram_id

# Настройки ИИ
USE_OPENROUTER=True
OPENROUTER_API_KEY=ваш_ключ_openrouter
OPENROUTER_MODEL=deepseek/deepseek-v4-flash # или другая модель

# Инструменты (опционально)
TOOLS_ENABLED=True
WEB_SEARCH_ENABLED=True
TAVILY_API_KEY=ваш_ключ_tavily
```

**4. Запуск**
```bash
python main.py
```

При первом запуске бот автоматически создаст базу данных bot_database.db. После этого его можно добавить в беседу.

## Базовые команды (Telegram)
 * /manage (в группе) - Получить ссылку для настройки бота администратором.
 * /my_groups (в ЛС) - Открыть панель управления группами.
 * /sleep N (в группе) - Отправить бота в спящий режим на N минут.
 * /dismiss (в группе) - Принудительно отключить активный режим.