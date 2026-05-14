from aiogram import Router, F, Bot
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import selectinload

from database.models import User, Group, group_admins, GlobalSettings, UserNote, ChatMember, SoftTrigger
from database.engine import AsyncSessionLocal
from config.config import config

router = Router()
router.message.filter(F.chat.type == "private")

class GroupSettingsFSM(StatesGroup):
    waiting_for_persona = State()
    waiting_for_memory = State()
    waiting_for_triggers = State()
    waiting_for_random_chance = State()
    waiting_for_chat_notes = State()
    waiting_for_idle_timeout = State()
    waiting_for_max_ignores = State()
    waiting_for_debounce = State()
    waiting_for_max_wait = State()
    waiting_for_paragraph_limit = State()

class UserNotesFSM(StatesGroup):
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
    await callback.message.edit_text("Действие отменено.")
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
    if message.from_user.id not in config.admin_ids:
        await message.answer("У вас нет глобальных прав доступа к этому боту.")
        return
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
        await session.commit()

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Открыть настройки", callback_data=f"open_group_{group_id}")]
    ])
    await message.answer("Вы подтверждены как администратор. Нажмите кнопку ниже для перехода в настройки.", reply_markup=keyboard)

@router.message(Command("my_groups"))
@router.message(F.text == "Мои группы")
async def cmd_my_groups(message: Message):
    if message.from_user.id not in config.admin_ids:
        await message.answer("У вас нет глобальных прав доступа к этому боту.")
        return
    async with AsyncSessionLocal() as session:
        stmt = select(Group).join(group_admins).where(group_admins.c.user_id == message.from_user.id)
        result = await session.execute(stmt)
        groups = result.scalars().all()

    if not groups:
        await message.answer("Вы пока не управляете ни одной группой.")
        return

    keyboard_builder = []
    for group in groups:
        keyboard_builder.append(
            [InlineKeyboardButton(text=group.title, callback_data=f"open_group_{group.chat_id}")]
        )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_builder)
    await message.answer("Выберите группу для настройки:", reply_markup=keyboard)

@router.callback_query(F.data == "back_to_groups")
async def back_to_groups(callback: CallbackQuery):
    async with AsyncSessionLocal() as session:
        stmt = select(Group).join(group_admins).where(group_admins.c.user_id == callback.from_user.id)
        result = await session.execute(stmt)
        groups = result.scalars().all()

    keyboard_builder = []
    for group in groups:
        keyboard_builder.append(
            [InlineKeyboardButton(text=group.title, callback_data=f"open_group_{group.chat_id}")]
        )
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_builder)
    await callback.message.edit_text("Выберите группу для настройки:", reply_markup=keyboard)

@router.callback_query(F.data.startswith("open_group_"))
async def open_group_settings(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[2])
    
    async with AsyncSessionLocal() as session:
        group = await session.scalar(select(Group).where(Group.chat_id == group_id))
        if not group:
            await callback.answer("Группа не найдена.", show_alert=True)
            return

    text = f"Управление группой: {group.title}\nВыберите категорию настроек:"
    
    builder = InlineKeyboardBuilder()
    builder.button(text="Личность и Контекст", callback_data=f"menu_persona_{group_id}")
    builder.button(text="Триггеры и Появления", callback_data=f"menu_triggers_{group_id}")
    builder.button(text="Динамика и Лимиты", callback_data=f"menu_limits_{group_id}")
    builder.button(text="Досье участников", callback_data=f"users_list_{group_id}")
    builder.button(text="Назад к списку", callback_data="back_to_groups")
    builder.adjust(1)

    await callback.message.edit_text(text, reply_markup=builder.as_markup())

@router.callback_query(F.data.startswith("menu_persona_"))
async def menu_persona(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[2])
    
    async with AsyncSessionLocal() as session:
        group = await session.scalar(select(Group).where(Group.chat_id == group_id))

    persona_text = group.active_persona[:200] + "..." if group.active_persona and len(group.active_persona) > 200 else (group.active_persona or "Не задана")
    chat_notes = group.chat_notes[:200] + "..." if group.chat_notes and len(group.chat_notes) > 200 else (group.chat_notes or "Не заданы")

    text = (
        f"Настройки личности и контекста\n\n"
        f"Персона:\n{persona_text}\n\n"
        f"Заметки чата:\n{chat_notes}\n\n"
        f"Глубина контекста: {group.context_length} сообщений"
    )
    
    builder = InlineKeyboardBuilder()
    builder.button(text="Изменить персону", callback_data=f"set_persona_{group_id}")
    builder.button(text="Заметки чата", callback_data=f"set_chatnotes_{group_id}")
    builder.button(text="Глубина контекста", callback_data=f"set_memory_{group_id}")
    builder.button(text="Назад", callback_data=f"open_group_{group_id}")
    builder.adjust(1)
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup())

@router.callback_query(F.data.startswith("menu_triggers_"))
async def menu_triggers(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[2])
    
    async with AsyncSessionLocal() as session:
        group = await session.scalar(select(Group).options(selectinload(Group.triggers)).where(Group.chat_id == group_id))

    triggers_list = [t.word for t in group.triggers] if group and group.triggers else []
    saved_triggers = ", ".join(triggers_list) if triggers_list else "Не заданы"
    if len(saved_triggers) > 100:
        saved_triggers = saved_triggers[:100] + "..."

    text = (
        f"Настройки триггеров\n\n"
        f"Мягкие триггеры: {saved_triggers}\n"
        f"Шанс случайного ответа: {group.random_chance}%"
    )
    
    builder = InlineKeyboardBuilder()
    builder.button(text="Мягкие триггеры", callback_data=f"set_triggers_{group_id}")
    builder.button(text="Случайные появления", callback_data=f"set_random_{group_id}")
    builder.button(text="Назад", callback_data=f"open_group_{group_id}")
    builder.adjust(1)
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup())

@router.callback_query(F.data.startswith("menu_limits_"))
async def menu_limits(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[2])
    
    async with AsyncSessionLocal() as session:
        group = await session.scalar(select(Group).where(Group.chat_id == group_id))

    text = (
        f"Динамика и Лимиты\n\n"
        f"Таймаут тишины (сон): {group.idle_timeout_minutes} мин\n"
        f"Лимит игноров: {group.max_consecutive_ignores} раз\n"
        f"Задержка debounce: {group.debounce_seconds} сек\n"
        f"Жесткое ожидание: {group.max_wait_seconds} сек\n"
        f"Макс. предложений: {group.paragraph_max_sentences}"
    )
    
    builder = InlineKeyboardBuilder()
    builder.button(text="Таймаут тишины", callback_data=f"set_idle_{group_id}")
    builder.button(text="Лимит игноров", callback_data=f"set_ignores_{group_id}")
    builder.button(text="Задержка debounce", callback_data=f"set_debounce_{group_id}")
    builder.button(text="Жесткое ожидание", callback_data=f"set_maxwait_{group_id}")
    builder.button(text="Лимит предложений", callback_data=f"set_paragraph_{group_id}")
    builder.button(text="Назад", callback_data=f"open_group_{group_id}")
    builder.adjust(2)
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup())

@router.callback_query(F.data.startswith("users_list_"))
async def show_chat_members(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[2])
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(ChatMember).where(ChatMember.group_id == group_id).limit(50))
        members = result.scalars().all()
    
    if not members:
        await callback.answer("База участников пока пуста.", show_alert=True)
        return

    builder = InlineKeyboardBuilder()
    for member in members:
        builder.button(text=member.user_name, callback_data=f"man_note_{member.user_id}")
    builder.button(text="Назад", callback_data=f"open_group_{group_id}")
    builder.adjust(2)
    await callback.message.edit_text("Выберите участника для настройки досье:", reply_markup=builder.as_markup())

@router.callback_query(F.data.startswith("man_note_"))
async def btn_man_note(callback: CallbackQuery, state: FSMContext):
    target_id = int(callback.data.split("_")[2])
    
    if not await is_any_group_admin(callback.from_user.id):
        await callback.answer("У вас нет прав.", show_alert=True)
        return
        
    await state.update_data(target_id=target_id)
    await state.set_state(UserNotesFSM.waiting_for_manual_text)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(UserNote).where(UserNote.user_id == target_id))
        user_note = result.scalar_one_or_none()
    
    current_text = user_note.note_text if user_note else "Пусто"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="cancel_fsm")]])
    await callback.message.edit_text(
        f"Текущее досье:\n{current_text}\n\nОтправьте новый текст досье. Это перезапишет старые данные. Отправьте 0 для удаления.",
        reply_markup=kb
    )

@router.message(UserNotesFSM.waiting_for_manual_text)
async def process_manual_note(message: Message, state: FSMContext):
    data = await state.get_data()
    target_id = data.get("target_id")
    
    if not await is_any_group_admin(message.from_user.id):
        await state.clear()
        return
        
    async with AsyncSessionLocal() as session:
        if message.text.strip() == "0":
            await session.execute(delete(UserNote).where(UserNote.user_id == target_id))
        else:
            result = await session.execute(select(UserNote).where(UserNote.user_id == target_id))
            note = result.scalar_one_or_none()
            if note:
                note.note_text = message.text
            else:
                session.add(UserNote(user_id=target_id, note_text=message.text))
        await session.commit()
        
    await state.clear()
    await message.answer("Досье успешно обновлено.")

@router.callback_query(F.data.startswith("set_"))
async def route_set_callbacks(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    action = parts[1]
    group_id = int(parts[2])

    if not await is_user_group_admin(callback.from_user.id, group_id):
        await callback.answer("У вас нет доступа к этой группе.", show_alert=True)
        return

    await state.update_data(group_id=group_id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="cancel_fsm")]])

    if action == "persona":
        await state.set_state(GroupSettingsFSM.waiting_for_persona)
        await callback.message.edit_text("Отправьте системный промпт (описание персоны):", reply_markup=keyboard)
    elif action == "chatnotes":
        await state.set_state(GroupSettingsFSM.waiting_for_chat_notes)
        await callback.message.edit_text("Отправьте правила и заметки чата (или 0 для удаления):", reply_markup=keyboard)
    elif action == "memory":
        await state.set_state(GroupSettingsFSM.waiting_for_memory)
        await callback.message.edit_text("Отправьте глубину памяти (число сообщений):", reply_markup=keyboard)
    elif action == "triggers":
        await state.set_state(GroupSettingsFSM.waiting_for_triggers)
        await callback.message.edit_text("Отправьте имена-триггеры через запятую (или 0 для удаления):", reply_markup=keyboard)
    elif action == "random":
        await state.set_state(GroupSettingsFSM.waiting_for_random_chance)
        await callback.message.edit_text("Отправьте шанс случайного появления (0-100):", reply_markup=keyboard)
    elif action == "idle":
        await state.set_state(GroupSettingsFSM.waiting_for_idle_timeout)
        await callback.message.edit_text("Отправьте таймаут тишины в минутах:", reply_markup=keyboard)
    elif action == "ignores":
        await state.set_state(GroupSettingsFSM.waiting_for_max_ignores)
        await callback.message.edit_text("Отправьте лимит тегов IGNORE подряд:", reply_markup=keyboard)
    elif action == "debounce":
        await state.set_state(GroupSettingsFSM.waiting_for_debounce)
        await callback.message.edit_text("Отправьте задержку сбора (debounce) в секундах:", reply_markup=keyboard)
    elif action == "maxwait":
        await state.set_state(GroupSettingsFSM.waiting_for_max_wait)
        await callback.message.edit_text("Отправьте жесткий дедлайн ожидания в секундах:", reply_markup=keyboard)
    elif action == "paragraph":
        await state.set_state(GroupSettingsFSM.waiting_for_paragraph_limit)
        await callback.message.edit_text("Отправьте ограничение предложений в абзаце:", reply_markup=keyboard)
    
    await callback.answer()

@router.message(GroupSettingsFSM.waiting_for_persona)
async def save_persona(message: Message, state: FSMContext):
    data = await state.get_data()
    async with AsyncSessionLocal() as session:
        await session.execute(update(Group).where(Group.chat_id == data["group_id"]).values(active_persona=message.text))
        await session.commit()
    await state.clear()
    await message.answer("Персона обновлена.")

@router.message(GroupSettingsFSM.waiting_for_chat_notes)
async def save_chat_notes(message: Message, state: FSMContext):
    data = await state.get_data()
    val = None if message.text.strip() == "0" else message.text
    async with AsyncSessionLocal() as session:
        await session.execute(update(Group).where(Group.chat_id == data["group_id"]).values(chat_notes=val))
        await session.commit()
    await state.clear()
    await message.answer("Заметки чата обновлены.")

@router.message(GroupSettingsFSM.waiting_for_memory)
async def save_memory(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Отправьте число.")
        return
    data = await state.get_data()
    async with AsyncSessionLocal() as session:
        await session.execute(update(Group).where(Group.chat_id == data["group_id"]).values(context_length=int(message.text)))
        await session.commit()
    await state.clear()
    await message.answer("Глубина памяти сохранена.")

@router.message(GroupSettingsFSM.waiting_for_random_chance)
async def save_random(message: Message, state: FSMContext):
    if not message.text.isdigit() or not (0 <= int(message.text) <= 100):
        await message.answer("Отправьте число от 0 до 100.")
        return
    data = await state.get_data()
    async with AsyncSessionLocal() as session:
        await session.execute(update(Group).where(Group.chat_id == data["group_id"]).values(random_chance=int(message.text)))
        await session.commit()
    await state.clear()
    await message.answer("Шанс сохранен.")

@router.message(GroupSettingsFSM.waiting_for_idle_timeout)
async def save_idle(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Отправьте число.")
        return
    data = await state.get_data()
    async with AsyncSessionLocal() as session:
        await session.execute(update(Group).where(Group.chat_id == data["group_id"]).values(idle_timeout_minutes=int(message.text)))
        await session.commit()
    await state.clear()
    await message.answer("Таймаут тишины сохранен.")

@router.message(GroupSettingsFSM.waiting_for_max_ignores)
async def save_ignores(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Отправьте число.")
        return
    data = await state.get_data()
    async with AsyncSessionLocal() as session:
        await session.execute(update(Group).where(Group.chat_id == data["group_id"]).values(max_consecutive_ignores=int(message.text)))
        await session.commit()
    await state.clear()
    await message.answer("Лимит игноров сохранен.")

@router.message(GroupSettingsFSM.waiting_for_debounce)
async def save_debounce(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Отправьте число.")
        return
    data = await state.get_data()
    async with AsyncSessionLocal() as session:
        await session.execute(update(Group).where(Group.chat_id == data["group_id"]).values(debounce_seconds=int(message.text)))
        await session.commit()
    await state.clear()
    await message.answer("Задержка сохранена.")

@router.message(GroupSettingsFSM.waiting_for_max_wait)
async def save_maxwait(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Отправьте число.")
        return
    data = await state.get_data()
    async with AsyncSessionLocal() as session:
        await session.execute(update(Group).where(Group.chat_id == data["group_id"]).values(max_wait_seconds=int(message.text)))
        await session.commit()
    await state.clear()
    await message.answer("Жесткое ожидание сохранено.")

@router.message(GroupSettingsFSM.waiting_for_paragraph_limit)
async def save_paragraph(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Отправьте число.")
        return
    data = await state.get_data()
    async with AsyncSessionLocal() as session:
        await session.execute(update(Group).where(Group.chat_id == data["group_id"]).values(paragraph_max_sentences=int(message.text)))
        await session.commit()
    await state.clear()
    await message.answer("Лимит предложений сохранен.")

@router.message(GroupSettingsFSM.waiting_for_triggers)
async def save_triggers(message: Message, state: FSMContext):
    data = await state.get_data()
    group_id = data["group_id"]
    raw_text = message.text.strip()
    
    async with AsyncSessionLocal() as session:
        await session.execute(delete(SoftTrigger).where(SoftTrigger.group_id == group_id))
        if raw_text != "0":
            triggers = [t.strip().lower() for t in raw_text.split(",") if t.strip()]
            for word in triggers:
                session.add(SoftTrigger(group_id=group_id, word=word))
        await session.commit()
        
    await state.clear()
    await message.answer("Мягкие триггеры сохранены.")

@router.message(Command("toggle_pm"))
async def cmd_toggle_pm(message: Message):
    if message.from_user.id != config.admin_ids:
        return
    async with AsyncSessionLocal() as session:
        settings = await session.scalar(select(GlobalSettings).where(GlobalSettings.id == 1))
        if not settings:
            settings = GlobalSettings(id=1, allow_all_pms=True)
            session.add(settings)
            status = "ВКЛЮЧЕНО"
        else:
            settings.allow_all_pms = not settings.allow_all_pms
            status = "ВКЛЮЧЕНО" if settings.allow_all_pms else "ВЫКЛЮЧЕНО"
        await session.commit()
    await message.answer(f"Глобальное разрешение на ЛС: {status}")