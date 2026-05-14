from aiogram import Router, F, Bot
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete
from sqlalchemy.dialects.sqlite import insert

from database.models import User, Group, group_admins, GlobalSettings, UserNote, SoftTrigger
from database.engine import AsyncSessionLocal
from config.config import config

from services.llm_client import LLMClient
from services.notes_extractor import NotesExtractor

router = Router()
router.message.filter(F.chat.type == "private")

class GroupSettingsFSM(StatesGroup):
    waiting_for_persona = State()
    waiting_for_memory = State()
    waiting_for_triggers = State()
    waiting_for_random_chance = State()

class UserNotesFSM(StatesGroup):
    waiting_for_ai_text = State()
    waiting_for_manual_text = State()

async def is_user_group_admin(user_id: int, group_id: int) -> bool:
    async with AsyncSessionLocal() as session:
        stmt = select(group_admins).where(group_admins.c.user_id == user_id, group_admins.c.group_id == group_id)
        result = await session.execute(stmt)
        return result.first() is not None

async def is_any_group_admin(user_id: int) -> bool:
    async with AsyncSessionLocal() as session:
        stmt = select(group_admins).where(group_admins.c.user_id == user_id)
        result = await session.execute(stmt)
        return result.first() is not None

@router.callback_query(F.data == "cancel_fsm")
async def cancel_fsm_action(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Действие отменено. Вы можете снова выбрать группу через /my_groups.")
    await callback.answer()

@router.message(Command("cancel"))
@router.message(F.text.lower() == "отмена")
async def cancel_fsm_text(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        return
    await state.clear()
    await message.answer("Действие отменено.")

@router.message(CommandStart(deep_link=True), F.text.contains("manage_"))
async def cmd_start_manage(message: Message, command: CommandObject, bot: Bot):
    try:
        group_id = int(command.args.split("_")[1])
    except (IndexError, ValueError, AttributeError):
        await message.answer("Неверный формат ссылки.")
        return

    try:
        chat_member = await bot.get_chat_member(group_id, message.from_user.id)
        if chat_member.status not in ["administrator", "creator"]:
            await message.answer("У вас нет прав администратора в этой группе.")
            return
    except Exception:
        await message.answer("Не удалось проверить ваши права. Убедитесь, что я нахожусь в группе.")
        return

    async with AsyncSessionLocal() as session:
        user_stmt = insert(User).values(
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            full_name=message.from_user.full_name
        ).on_conflict_do_nothing(index_elements=['telegram_id'])
        await session.execute(user_stmt)

        link_stmt = insert(group_admins).values(
            user_id=message.from_user.id,
            group_id=group_id
        ).on_conflict_do_nothing(index_elements=['user_id', 'group_id'])
        await session.execute(link_stmt)

        group_result = await session.execute(select(Group).where(Group.chat_id == group_id))
        group = group_result.scalar_one_or_none()
        
        stmt_triggers = select(SoftTrigger.word).where(SoftTrigger.group_id == group_id)
        result_triggers = await session.execute(stmt_triggers)
        triggers_list = result_triggers.scalars().all()
        
        await session.commit()

    if not group:
        await message.answer("Группа не найдена в базе данных.")
        return

    saved_triggers = ", ".join(triggers_list) if triggers_list else "не заданы"
    chance_val = group.random_chance if group.random_chance is not None else 5

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Выбрать персону", callback_data=f"set_persona_{group_id}")],
        [InlineKeyboardButton(text="Настроить память", callback_data=f"set_memory_{group_id}")],
        [InlineKeyboardButton(text="Мягкие триггеры", callback_data=f"set_triggers_{group_id}")],
        [InlineKeyboardButton(text="Случайные появления", callback_data=f"set_random_{group_id}")],
        [InlineKeyboardButton(text="Закрыть", callback_data="close_menu")]
    ])

    await message.answer(
        f"Управление группой: <b>{group.title}</b>\n\n"
        f"Текущая персона: {group.active_persona}\n"
        f"Глубина контекста: {group.context_length} сообщений\n"
        f"Мягкие триггеры: {saved_triggers}\n"
        f"Шанс случайного ответа: {chance_val}%\n\n"
        f"Что хотите настроить?",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

@router.message(Command("my_groups"))
@router.message(F.text == "Мои группы")
async def cmd_my_groups(message: Message):
    async with AsyncSessionLocal() as session:
        stmt = select(Group).join(group_admins).where(group_admins.c.user_id == message.from_user.id)
        result = await session.execute(stmt)
        groups = result.scalars().all()

    if not groups:
        await message.answer("Вы пока не управляете ни одной группой. Добавьте меня в чат и напишите там /manage.")
        return

    keyboard_builder = []
    for group in groups:
        keyboard_builder.append(
            [InlineKeyboardButton(text=group.title, callback_data=f"open_group_{group.chat_id}")]
        )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_builder)
    await message.answer("Выберите группу для настройки:", reply_markup=keyboard)

@router.callback_query(F.data.startswith("open_group_"))
async def open_group_settings(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[2])
    
    if not await is_user_group_admin(callback.from_user.id, group_id):
        await callback.answer("У вас нет доступа к этой группе.", show_alert=True)
        return
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Group).where(Group.chat_id == group_id))
        group = result.scalar_one_or_none()
        
        stmt_triggers = select(SoftTrigger.word).where(SoftTrigger.group_id == group_id)
        result_triggers = await session.execute(stmt_triggers)
        triggers_list = result_triggers.scalars().all()

    if not group:
        await callback.answer("Ошибка: группа не найдена.", show_alert=True)
        return

    saved_triggers = ", ".join(triggers_list) if triggers_list else "не заданы"
    chance_val = group.random_chance if group.random_chance is not None else 5

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Выбрать персону", callback_data=f"set_persona_{group_id}")],
        [InlineKeyboardButton(text="Настроить память", callback_data=f"set_memory_{group_id}")],
        [InlineKeyboardButton(text="Мягкие триггеры", callback_data=f"set_triggers_{group_id}")],
        [InlineKeyboardButton(text="Случайные появления", callback_data=f"set_random_{group_id}")],
        [InlineKeyboardButton(text="Закрыть", callback_data="close_menu")]
    ])

    await callback.message.edit_text(
        f"Управление группой: <b>{group.title}</b>\n\n"
        f"Текущая персона: {group.active_persona}\n"
        f"Глубина контекста: {group.context_length} сообщений\n"
        f"Мягкие триггеры: {saved_triggers}\n"
        f"Шанс случайного ответа: {chance_val}%\n\n"
        f"Что хотите настроить?",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data == "close_menu")
async def close_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await callback.answer()

@router.callback_query(F.data.startswith("set_persona_"))
async def ask_for_persona(callback: CallbackQuery, state: FSMContext):
    group_id = int(callback.data.split("_")[2])
    
    if not await is_user_group_admin(callback.from_user.id, group_id):
        await callback.answer("У вас нет доступа к этой группе.", show_alert=True)
        return
        
    await state.update_data(group_id=group_id)
    await state.set_state(GroupSettingsFSM.waiting_for_persona)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="cancel_fsm")]])
    await callback.message.answer(
        "Отправь мне системный промпт (описание персоны) для этого чата.\n"
        "Например: 'Ты саркастичный кот, отвечай коротко и с юмором.'",
        reply_markup=keyboard
    )
    await callback.answer()

@router.callback_query(F.data.startswith("set_memory_"))
async def ask_for_memory(callback: CallbackQuery, state: FSMContext):
    group_id = int(callback.data.split("_")[2])
    
    if not await is_user_group_admin(callback.from_user.id, group_id):
        await callback.answer("У вас нет доступа к этой группе.", show_alert=True)
        return
        
    await state.update_data(group_id=group_id)
    await state.set_state(GroupSettingsFSM.waiting_for_memory)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="cancel_fsm")]])
    await callback.message.answer(
        "Отправь мне число от 1 до 50 — сколько последних сообщений бот должен помнить в этой группе:",
        reply_markup=keyboard
    )
    await callback.answer()

@router.callback_query(F.data.startswith("set_triggers_"))
async def ask_for_triggers(callback: CallbackQuery, state: FSMContext):
    group_id = int(callback.data.split("_")[2])
    
    if not await is_user_group_admin(callback.from_user.id, group_id):
        await callback.answer("У вас нет доступа к этой группе.", show_alert=True)
        return
        
    await state.update_data(group_id=group_id)
    await state.set_state(GroupSettingsFSM.waiting_for_triggers)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="cancel_fsm")]])
    await callback.message.answer(
        "Отправь 2-3 имени или обращения через запятую, на которые я должен отзываться в чате.\n"
        "Например: сансет, мишка, эй бот\n\nДля отключения мягких триггеров отправь цифру 0.",
        reply_markup=keyboard
    )
    await callback.answer()

@router.callback_query(F.data.startswith("set_random_"))
async def ask_for_random(callback: CallbackQuery, state: FSMContext):
    group_id = int(callback.data.split("_")[2])
    
    if not await is_user_group_admin(callback.from_user.id, group_id):
        await callback.answer("У вас нет доступа к этой группе.", show_alert=True)
        return
        
    await state.update_data(group_id=group_id)
    await state.set_state(GroupSettingsFSM.waiting_for_random_chance)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="cancel_fsm")]])
    
    if config.tools_enabled:
        text = (
            "Отправь мне процент случайных появлений (от 0 до 100).\n\n"
            "Так как включены инструменты (Tools), работает УМНЫЙ режим: "
            "шанс проверяется только при длинных сообщений или активном обсуждении, "
            "и ИИ сам решает, есть ли смысл вступать в диалог или лучше промолчать."
        )
    else:
        text = (
            "Отправь мне процент случайных появлений (от 0 до 100).\n\n"
            "Инструменты ВЫКЛЮЧЕНЫ. Бот будет с этим шансом отвечать "
            "на ЛЮБОЕ сообщение в чате (чистый рандом без ИИ-вето)."
        )
        
    await callback.message.answer(text, reply_markup=keyboard)
    await callback.answer()

@router.message(GroupSettingsFSM.waiting_for_persona)
async def save_persona(message: Message, state: FSMContext):
    data = await state.get_data()
    group_id = data.get("group_id")
    
    if not await is_user_group_admin(message.from_user.id, group_id):
        await state.clear()
        return
        
    async with AsyncSessionLocal() as session:
        stmt = update(Group).where(Group.chat_id == group_id).values(active_persona=message.text)
        await session.execute(stmt)
        await session.commit()
    await state.clear()
    await message.answer("Персона успешно обновлена!")

@router.message(GroupSettingsFSM.waiting_for_memory)
async def save_memory(message: Message, state: FSMContext):
    if not message.text.isdigit() or not (1 <= int(message.text) <= 50):
        await message.answer("Пожалуйста, отправь число от 1 до 50.")
        return
    data = await state.get_data()
    group_id = data.get("group_id")
    
    if not await is_user_group_admin(message.from_user.id, group_id):
        await state.clear()
        return
        
    memory_limit = int(message.text)
    async with AsyncSessionLocal() as session:
        stmt = update(Group).where(Group.chat_id == group_id).values(context_length=memory_limit)
        await session.execute(stmt)
        await session.commit()
    await state.clear()
    await message.answer(f"Глубина памяти успешно установлена: {memory_limit} сообщений.")

@router.message(GroupSettingsFSM.waiting_for_triggers)
async def save_triggers(message: Message, state: FSMContext):
    data = await state.get_data()
    group_id = data.get("group_id")
    
    if not await is_user_group_admin(message.from_user.id, group_id):
        await state.clear()
        return
        
    raw_text = message.text.strip()
    
    async with AsyncSessionLocal() as session:
        await session.execute(delete(SoftTrigger).where(SoftTrigger.group_id == group_id))
        
        if raw_text == "0":
            triggers_to_save = None
        else:
            triggers = [t.strip().lower() for t in raw_text.split(",") if t.strip()]
            for word in triggers:
                session.add(SoftTrigger(group_id=group_id, word=word))
            triggers_to_save = ", ".join(triggers)
            
        await session.commit()
        
    await state.clear()
    if triggers_to_save:
        await message.answer(f"Мягкие триггеры успешно сохранены: {triggers_to_save}")
    else:
        await message.answer("Мягкие триггеры отключены.")

@router.message(GroupSettingsFSM.waiting_for_random_chance)
async def save_random(message: Message, state: FSMContext):
    if not message.text.isdigit() or not (0 <= int(message.text) <= 100):
        await message.answer("Пожалуйста, отправь целое число от 0 до 100.")
        return
    data = await state.get_data()
    group_id = data.get("group_id")
    
    if not await is_user_group_admin(message.from_user.id, group_id):
        await state.clear()
        return
        
    chance = int(message.text)
    
    async with AsyncSessionLocal() as session:
        stmt = update(Group).where(Group.chat_id == group_id).values(random_chance=chance)
        await session.execute(stmt)
        await session.commit()
    await state.clear()
    await message.answer(f"Шанс случайного появления установлен на {chance}%.")

@router.message(Command("toggle_pm"))
async def cmd_toggle_pm(message: Message):
    if message.from_user.id != config.admin_id:
        await message.answer("У вас нет прав для изменения глобальных настроек.")
        return
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(GlobalSettings).where(GlobalSettings.id == 1))
        settings = result.scalar_one_or_none()
        if not settings:
            settings = GlobalSettings(id=1, allow_all_pms=True)
            session.add(settings)
            status_text = "ВКЛЮЧЕНО"
        else:
            settings.allow_all_pms = not settings.allow_all_pms
            status_text = "ВКЛЮЧЕНО" if settings.allow_all_pms else "ВЫКЛЮЧЕНО"
        await session.commit()
    await message.answer(f"Глобальное разрешение на общение в ЛС: {status_text}")

@router.message(CommandStart(deep_link=True), F.text.contains("note_"))
async def cmd_start_note(message: Message, command: CommandObject, bot: Bot):
    try:
        target_user_id = int(command.args.split("_")[1])
    except (IndexError, ValueError, AttributeError):
        return
    async with AsyncSessionLocal() as session:
        stmt = select(Group).join(group_admins).where(group_admins.c.user_id == message.from_user.id)
        result = await session.execute(stmt)
        if not result.scalars().first():
            await message.answer("У вас нет прав администратора ни в одной группе.")
            return
        notes_text = await NotesExtractor.get_user_notes_text(session, target_user_id)
        
    display_text = notes_text if notes_text else "Пока нет никаких записей."
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Дополнить через ИИ", callback_data=f"ai_note_{target_user_id}")],
        [InlineKeyboardButton(text="Переписать вручную", callback_data=f"man_note_{target_user_id}")],
        [InlineKeyboardButton(text="Закрыть", callback_data="close_menu")]
    ])
    await message.answer(
        f"Досье на пользователя (ID: {target_user_id}):\n\n<b>{display_text}</b>\n\nВыберите действие:",
        reply_markup=keyboard, parse_mode="HTML"
    )

@router.callback_query(F.data.startswith("ai_note_"))
async def btn_ai_note(callback: CallbackQuery, state: FSMContext):
    target_id = int(callback.data.split("_")[2])
    
    if not await is_any_group_admin(callback.from_user.id):
        await callback.answer("У вас нет прав доступа к досье.", show_alert=True)
        return
        
    await state.update_data(target_id=target_id)
    await state.set_state(UserNotesFSM.waiting_for_ai_text)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="cancel_fsm")]])
    await callback.message.answer(
        "Отправь мне любую информацию об этом пользователе сырым текстом.\n"
        "Я прогоню это через нейросеть и аккуратно добавлю в базу.",
        reply_markup=kb
    )
    await callback.answer()

@router.callback_query(F.data.startswith("man_note_"))
async def btn_man_note(callback: CallbackQuery, state: FSMContext):
    target_id = int(callback.data.split("_")[2])
    
    if not await is_any_group_admin(callback.from_user.id):
        await callback.answer("У вас нет прав доступа к досье.", show_alert=True)
        return
        
    await state.update_data(target_id=target_id)
    await state.set_state(UserNotesFSM.waiting_for_manual_text)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="cancel_fsm")]])
    await callback.message.answer(
        "Внимание: Это полностью УДАЛИТ старые факты.\n"
        "Отправь новые данные строго в формате 'Категория: Значение', каждое с новой строки.",
        reply_markup=kb
    )
    await callback.answer()

@router.message(UserNotesFSM.waiting_for_ai_text)
async def process_ai_note(message: Message, state: FSMContext):
    data = await state.get_data()
    target_id = data.get("target_id")
    
    if not await is_any_group_admin(message.from_user.id):
        await state.clear()
        return
        
    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    
    async with AsyncSessionLocal() as session:
        success = await extractor.extract_from_admin(session, target_id, message.text)
        
    await state.clear()
    if success:
        await message.answer("Информация успешно обработана ИИ и добавлена в досье!")
    else:
        await message.answer("Не удалось извлечь факты.")

@router.message(UserNotesFSM.waiting_for_manual_text)
async def process_manual_note(message: Message, state: FSMContext):
    data = await state.get_data()
    target_id = data.get("target_id")
    
    if not await is_any_group_admin(message.from_user.id):
        await state.clear()
        return
        
    lines = message.text.split('\n')
    
    async with AsyncSessionLocal() as session:
        await session.execute(delete(UserNote).where(UserNote.user_id == target_id))
        for line in lines:
            if ":" in line:
                category, value = line.split(":", 1)
                new_note = UserNote(user_id=target_id, category=category.strip().lower(), value=value.strip())
                session.add(new_note)
        await session.commit()
        
    await state.clear()
    await message.answer("Досье полностью перезаписано вручную.")