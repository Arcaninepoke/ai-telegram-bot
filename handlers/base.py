from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from config.config import config
from database.engine import AsyncSessionLocal
from database.models import GlobalSettings
from sqlalchemy import select

router = Router()
router.message.filter(F.chat.type == "private")

def get_main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    kb_list = [
        [KeyboardButton(text="Мои группы")]
    ]

    if user_id in config.admin_ids:
        kb_list.append([KeyboardButton(text="Глобальные настройки")])
        
    kb_list.append([KeyboardButton(text="Помощь")])
    
    return ReplyKeyboardMarkup(
        keyboard=kb_list,
        resize_keyboard=True,
        input_field_placeholder="Выберите действие..."
    )

@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        f"Привет, {message.from_user.first_name}!\n\n"
        f"Я твой локальный ИИ-ассистент. Используй кнопки ниже для навигации.",
        reply_markup=get_main_keyboard(message.from_user.id)
    )

@router.message(F.text == "Помощь")
@router.message(Command("help"))
async def cmd_help(message: Message):
    help_text = (
        "Справочная информация:\n\n"
        "В личных сообщениях:\n"
        "/my_groups или кнопка 'Мои группы' - Настройка подключенных бесед.\n"
        "/cancel - Отмена любого текущего действия (ввода текста).\n\n"
        "В группах (только для администраторов):\n"
        "/manage - Получить ссылку на настройку текущей группы.\n"
        "/dismiss - Принудительно отключить активный режим ИИ.\n"
        "/clean - Полностью очистить память диалога для этой группы.\n"
        "/sleep N - Отправить бота в режим тишины на N минут."
    )
    await message.answer(help_text)

@router.message(F.text == "Глобальные настройки")
async def btn_global_settings(message: Message):
    if message.from_user.id not in config.admin_ids:
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(GlobalSettings).where(GlobalSettings.id == 1))
        settings = result.scalar_one_or_none()
        is_allowed = settings.allow_all_pms if settings else False

    status = "ВКЛЮЧЕНО" if is_allowed else "ВЫКЛЮЧЕНО"
    
    await message.answer(
        f"Панель главного администратора.\n\n"
        f"Свободное общение в ЛС для всех пользователей сейчас: {status}\n\n"
        f"Чтобы изменить статус, введите команду /toggle_pm"
    )