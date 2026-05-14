import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand
import aiohttp

from config.config import config
from database.engine import init_db
from handlers import base, group, settings, chat

async def main():
    logging.basicConfig(level=logging.INFO)

    await init_db()

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode='HTML')
    )
    dp = Dispatcher()

    chat.http_session = aiohttp.ClientSession()

    dp.include_router(group.router)
    dp.include_router(settings.router)
    dp.include_router(base.router)
    dp.include_router(chat.router)

    await bot.set_my_commands([
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="my_groups", description="Управление группами"),
        BotCommand(command="help", description="Справка")
    ])

    await bot.delete_webhook(drop_pending_updates=True)
    
    try:
        await dp.start_polling(bot)
    finally:
        await chat.http_session.close()
        await bot.session.close()
        print("[INFO] Сессия бота закрыта.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[INFO] Бот успешно остановлен вручную (Ctrl+C).")
    except Exception as e:
        print(f"\n[ERROR] Бот упал с ошибкой: {e}")