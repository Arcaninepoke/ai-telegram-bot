from aiogram import Router, F
from aiogram.types import Message, ChatMemberUpdated, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command, ChatMemberUpdatedFilter, IS_MEMBER, IS_NOT_MEMBER
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.sqlite import insert

from database.models import Group
from database.models import User
from database.engine import AsyncSessionLocal

router = Router()
router.message.filter(F.chat.type.in_({"group", "supergroup"}))

@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> IS_MEMBER))
async def bot_added_to_group(event: ChatMemberUpdated):
    async with AsyncSessionLocal() as session:
        stmt = insert(Group).values(
            chat_id=event.chat.id,
            title=event.chat.title
        ).on_conflict_do_nothing(index_elements=['chat_id'])
        
        await session.execute(stmt)
        await session.commit()
        
    await event.bot.send_message(
        event.chat.id,
        "Всем привет! Я ИИ-ассистент. Администраторы могут настроить меня с помощью команды /manage"
    )

@router.message(Command("manage"))
async def cmd_manage(message: Message):
    chat_member = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
    
    if chat_member.status not in ["administrator", "creator"]:
        await message.reply("Эта команда доступна только администраторам группы.")
        return

    bot_info = await message.bot.get_me()
    bot_username = bot_info.username
    group_id = message.chat.id
    deep_link = f"https://t.me/{bot_username}?start=manage_{group_id}"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Настроить бота в ЛС", url=deep_link)]
    ])
    
    await message.reply("Настройки чата доступны в личных сообщениях:", reply_markup=keyboard)

@router.message(Command("note"), F.chat.type.in_(["group", "supergroup"]))
async def cmd_user_note(message: Message):
    if not message.reply_to_message:
        await message.reply("Эту команду нужно отправлять в ответ на сообщение пользователя.")
        return

    chat_member = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
    if chat_member.status not in ["administrator", "creator"]:
        await message.reply("Только администраторы могут управлять заметками.")
        return

    target_user = message.reply_to_message.from_user
    bot_info = await message.bot.get_me()
    deep_link = f"https://t.me/{bot_info.username}?start=note_{target_user.id}"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Заметки: {target_user.first_name}", url=deep_link)]
    ])
    
    await message.reply("Управление памятью об этом пользователе доступно в ЛС:", reply_markup=keyboard)