import asyncio
import base64
import json
import time
import re
import random
from io import BytesIO

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.orm import selectinload
import aiohttp

from database.engine import AsyncSessionLocal
from database.models import GlobalSettings, Group
from services.llm_client import llm 
from services.memory_manager import MemoryManager
from services.notes_extractor import NotesExtractor
from config.config import config

router = Router()

memory_manager = MemoryManager(llm_client=llm)
extractor = NotesExtractor(llm_client=llm)

active_group_sessions = {}
user_message_buffers = {}
NOTES_BATCH_SIZE = 5

chat_locks = {}
last_message_times = {}
first_trigger_times = {} 
DEBOUNCE_DELAY = 4.0
MAX_WAIT_TIME = 15.0

soft_trigger_cooldowns = {}
SOFT_TRIGGER_COOLDOWN = 600.0 

recent_chat_activity = {}
random_trigger_state = {}

http_session = None

async def perform_web_search(query: str, api_key: str) -> list:
    print(f"[DEBUG SEARCH] ИИ ищет в Tavily: {query}")
    if not api_key:
        print("[ERROR SEARCH] Ключ TAVILY_API_KEY не настроен в .env!")
        return []
    url = "https://api.tavily.com/search"
    payload = {
        "api_key": api_key, "query": query, "search_depth": "basic", 
        "include_answer": False, "max_results": 4
    }
    try:
        async with http_session.post(url, json=payload) as response:
            if response.status == 200:
                data = await response.json()
                results = data.get("results", [])
                print(f"[DEBUG SEARCH] Найдено результатов: {len(results)}")
                return results
            else:
                print(f"[ERROR SEARCH] Ошибка Tavily HTTP: {response.status}")
                return []
    except Exception as e:
        print(f"[ERROR SEARCH] Внутренняя ошибка запроса: {str(e)}")
        return []

async def _extract_image_base64(message: Message) -> str | None:
    if message.photo and config.vision_enabled:
        photo = message.photo[-1]
        file_info = await message.bot.get_file(photo.file_id)
        downloaded_file = await message.bot.download_file(file_info.file_path)
        image_bytes = downloaded_file.read()
        return base64.b64encode(image_bytes).decode('utf-8')
    return None

async def _process_llm_tools(tool_calls, message_obj, messages_to_send, chat_id):
    if message_obj is None:
        message_obj = {"role": "assistant", "content": None, "tool_calls": tool_calls}
        
    messages_to_send.append(message_obj)
    force_exit = False
    farewell_msg = ""
    
    if hasattr(message_obj, 'content') and message_obj.content:
        farewell_msg = message_obj.content
    elif isinstance(message_obj, dict) and message_obj.get('content'):
        farewell_msg = message_obj.get('content')
    
    for tool_call in tool_calls:
        if tool_call.function.name == "end_active_dialogue":
            force_exit = True
            try:
                args = json.loads(tool_call.function.arguments)
                tool_farewell = args.get("farewell_message", "")
                if tool_farewell:
                    farewell_msg = tool_farewell
            except Exception:
                pass
                
            if not farewell_msg:
                farewell_msg = "Был рад пообщаться! Если что - пингуйте."

            messages_to_send.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "name": tool_call.function.name,
                "content": '{"status": "disconnected"}'
            })
            
        elif tool_call.function.name == "web_search":
            args = json.loads(tool_call.function.arguments)
            query = args.get("query", "")
            
            try:
                search_results = await perform_web_search(query, config.tavily_api_key)
                if search_results:
                    search_context = "\n".join([f"- {r['title']}: {r['content']}" for r in search_results])
                    function_result = f"Результаты поиска:\n{search_context}"
                else:
                    function_result = "Ничего не найдено."
            except Exception as e:
                function_result = f"Ошибка поиска: {str(e)}"
            
            messages_to_send.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "name": tool_call.function.name,
                "content": function_result
            })
            
    return force_exit, farewell_msg, messages_to_send

@router.message(Command("dismiss"))
async def cmd_force_dismiss(message: Message):
    if message.chat.type in ["group", "supergroup"]:
        chat_id = message.chat.id
        if active_group_sessions.get(chat_id, False):
            active_group_sessions[chat_id] = False
            await message.reply("Принято. Принудительно отключаю активный режим...")
        else:
            await message.reply("Я и так нахожусь в спящем режиме. Пингуйте, если понадоблюсь.")

@router.message(F.chat.type == "private", (F.text | F.photo) & ~F.text.startswith("/") & ~F.caption.startswith("/"))
async def handle_private_messages(message: Message):
    is_admin = (message.from_user.id == config.admin_id)
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(GlobalSettings).where(GlobalSettings.id == 1))
        settings = result.scalar_one_or_none()
        is_allowed = settings.allow_all_pms if settings else False

    if not is_allowed and not is_admin:
        await message.answer("Владелец бота отключил режим общения в ЛС.")
        return

    messages_history = [
        {"role": "system", "content": "Ты дружелюбный ИИ-ассистент. Отвечай полезно и кратко."},
        {"role": "user", "content": message.text or message.caption or ""}
    ]
    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    ai_response_data = await llm.generate_response(messages_history)
    ai_response = ai_response_data.get("content", "Извините, произошла ошибка генерации.")
    await message.answer(ai_response)

@router.message(F.chat.type.in_(["group", "supergroup"]), (F.text | F.photo) & ~F.text.startswith("/") & ~F.caption.startswith("/"))
async def handle_group_messages(message: Message):
    bot_info = await message.bot.get_me()
    bot_username = bot_info.username

    raw_text = message.text or message.caption or ""
    
    is_reply_to_bot = False
    if message.reply_to_message and message.reply_to_message.from_user:
        is_reply_to_bot = (message.reply_to_message.from_user.id == bot_info.id)
        
    is_bot_mentioned = (f"@{bot_username}" in raw_text)
    
    clean_text = raw_text.replace(f"@{bot_username}", "").strip()
    user_name = message.from_user.first_name
    history_text = f"{user_name}: {clean_text}" if clean_text else f"{user_name} отправил изображение."

    base64_image = await _extract_image_base64(message)

    chat_id = message.chat.id
    should_respond = False
    is_soft_triggered = False

    async with AsyncSessionLocal() as session:
        if clean_text:
            await memory_manager.add_message(session, chat_id, "user", history_text)
            
            user_id = message.from_user.id
            if user_id not in user_message_buffers:
                user_message_buffers[user_id] = []
            user_message_buffers[user_id].append(clean_text)

            if len(user_message_buffers[user_id]) >= NOTES_BATCH_SIZE:
                text_to_analyze = "\n".join(user_message_buffers[user_id])
                user_message_buffers[user_id] = []
                asyncio.create_task(extractor.extract_and_save(user_id, user_name, text_to_analyze))

        is_active_mode = active_group_sessions.get(chat_id, False)
        
        is_reply_to_human = False
        if message.reply_to_message and message.reply_to_message.from_user:
            is_reply_to_human = (message.reply_to_message.from_user.id != bot_info.id)

        result = await session.execute(select(Group).options(selectinload(Group.triggers)).where(Group.chat_id == chat_id))
        group = result.scalar_one_or_none()

        if group and group.triggers:
            trigger_words = [t.word for t in group.triggers]
            if trigger_words:
                pattern = r'\b(?:' + '|'.join(map(re.escape, trigger_words)) + r')\b'
                if re.search(pattern, clean_text, re.IGNORECASE):
                    has_question = "?" in clean_text
                    is_comma_separated = re.search(r'[,.!?]\s*' + pattern + r'\s*[,.!?]', clean_text, re.IGNORECASE)
                    starts_with_trigger = re.match(pattern + r'\s*[,.!?]', clean_text, re.IGNORECASE)
                    is_standalone = re.fullmatch(pattern, clean_text, re.IGNORECASE)
                    if has_question or is_comma_separated or starts_with_trigger or is_standalone:
                        current_time = time.time()
                        last_soft = soft_trigger_cooldowns.get(chat_id, 0)
                        if current_time - last_soft > SOFT_TRIGGER_COOLDOWN:
                            is_soft_triggered = True
                            soft_trigger_cooldowns[chat_id] = current_time

        chance = (group.random_chance / 100.0) if group and group.random_chance is not None else 0.05

    if is_reply_to_bot or is_bot_mentioned or is_soft_triggered:
        should_respond = True
        active_group_sessions[chat_id] = True
    elif is_active_mode and not is_reply_to_human:
        should_respond = True

    if not should_respond and not is_active_mode and not is_reply_to_human and chance > 0:
        if config.tools_enabled:
            now = time.time()
            if chat_id not in recent_chat_activity:
                recent_chat_activity[chat_id] = []
            recent_chat_activity[chat_id].append((now, message.from_user.id))
            recent_chat_activity[chat_id] = [(t, uid) for t, uid in recent_chat_activity[chat_id] if now - t < 60]
            
            unique_users = len(set(uid for t, uid in recent_chat_activity[chat_id]))
            msg_count = len(recent_chat_activity[chat_id])
            
            is_long_msg = len(clean_text.split()) >= 12
            is_active_discussion = msg_count >= 5 and unique_users >= 2
            
            if is_long_msg or is_active_discussion:
                if random.random() < chance:
                    should_respond = True
                    active_group_sessions[chat_id] = True
                    random_trigger_state[chat_id] = True
                    recent_chat_activity[chat_id].clear()
        else:
            if random.random() < chance:
                should_respond = True
                active_group_sessions[chat_id] = True

    if should_respond and clean_text.lower() in ["хватит", "стоп", "пока", "спи"]:
        active_group_sessions[chat_id] = False
        await message.reply("Понял, ухожу в спящий режим. Зовите, если понадоблюсь!")
        return

    if should_respond:
        if not clean_text and not base64_image:
            await message.reply("Слушаю вас!")
            return

        current_time = time.time()
        last_message_times[chat_id] = current_time
        if chat_id not in first_trigger_times:
            first_trigger_times[chat_id] = current_time

        await message.bot.send_chat_action(chat_id=chat_id, action="typing")
        
        await asyncio.sleep(DEBOUNCE_DELAY)
        
        time_since_first = time.time() - first_trigger_times.get(chat_id, current_time)
        
        if last_message_times[chat_id] != current_time:
            if time_since_first < MAX_WAIT_TIME:
                return 
        
        if chat_id not in chat_locks:
            chat_locks[chat_id] = asyncio.Lock()

        async with chat_locks[chat_id]:
            if chat_id not in first_trigger_times and last_message_times[chat_id] != current_time:
                return
            
            first_trigger_times.pop(chat_id, None)

            async with AsyncSessionLocal() as session:
                result = await session.execute(select(Group).where(Group.chat_id == chat_id))
                group = result.scalar_one_or_none()
                persona = group.active_persona if group else "Ты умный участник чата."
                memory_limit = group.context_length if group else 10
                user_notes_text = await NotesExtractor.get_user_notes_text(session, message.from_user.id)
                
                if config.tools_enabled:
                    smart_exit_instruction = (
                        "\n\nСИСТЕМНОЕ ПРАВИЛО: Ты в режиме активного диалога. "
                        "Если с тобой попрощались или тема закрыта, ты ОБЯЗАН вызвать функцию 'end_active_dialogue' "
                        "и передать свой прощальный текст в аргумент 'farewell_message'."
                    )
                else:
                    smart_exit_instruction = (
                        "\n\n<CRITICAL_RULES>\n"
                        "1. Ты в режиме активного диалога.\n"
                        "2. Если с тобой попрощались или тема закрыта, ты ОБЯЗАН завершить ответ тегом <END_CHAT>.\n"
                        "</CRITICAL_RULES>"
                    )
                
                if random_trigger_state.get(chat_id) and config.tools_enabled:
                    smart_exit_instruction += (
                        "\n\nСИСТЕМНОЕ ПРАВИЛО (ВНЕЗАПНОЕ ВМЕШАТЕЛЬСТВО): Ты инициативно решил ворваться в чужой разговор. "
                        "Если тема интересная и тебе есть что сказать - выскажи свое мнение, и ты станешь участником беседы. "
                        "Если обсуждается скучный или непонятный мусор, ОБЯЗАТЕЛЬНО ответь строго одним тегом <IGNORE>."
                    )

                if user_notes_text:
                    persona = f"{persona}\n\nИНФОРМАЦИЯ О СОБЕСЕДНИКЕ:\n{user_notes_text}{smart_exit_instruction}"
                else:
                    persona = f"{persona}{smart_exit_instruction}"

                chat_history = await memory_manager.get_context(session, chat_id, memory_limit)
                messages_to_send = [{"role": "system", "content": persona}]
                messages_to_send.extend(chat_history)

                if base64_image:
                    last_msg = messages_to_send.pop() 
                    user_content = [{"type": "text", "text": last_msg["content"]}]
                    if not clean_text:
                        user_content[0]["text"] = f"{user_name} показывает это изображение. Что на нем?"
                        
                    user_content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}
                    })
                    messages_to_send.append({"role": "user", "content": user_content})

                available_tools = []
                if config.tools_enabled:
                    available_tools.append({
                        "type": "function",
                        "function": {
                            "name": "end_active_dialogue",
                            "description": "Вызови эту функцию, если с тобой попрощались или тема разговора завершена, чтобы отключить себя от чата.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "farewell_message": {
                                        "type": "string",
                                        "description": "Твой прощальный текст в стиле текущей персоны, учитывающий контекст разговора."
                                    }
                                },
                                "required": ["farewell_message"]
                            }
                        }
                    })
                    
                    if config.web_search_enabled and config.tavily_api_key:
                        available_tools.append({
                            "type": "function",
                            "function": {
                                "name": "web_search",
                                "description": "Искать информацию в интернете. Используй для поиска новостей, мемов и фактов.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "query": {
                                            "type": "string",
                                            "description": "Поисковый запрос"
                                        }
                                    },
                                    "required": ["query"]
                                }
                            }
                        })

                await message.bot.send_chat_action(chat_id=chat_id, action="typing")
                
                ai_response_data = await llm.generate_response(messages_to_send, tools=available_tools if available_tools else None)
                
                if ai_response_data.get("type") == "tool_calls":
                    tool_calls = ai_response_data.get("tool_calls")
                    message_obj = ai_response_data.get("message_obj")
                    
                    force_exit, farewell_msg, messages_to_send = await _process_llm_tools(
                        tool_calls, message_obj, messages_to_send, chat_id
                    )
                    
                    if force_exit:
                        active_group_sessions[chat_id] = False
                        random_trigger_state.pop(chat_id, None)
                        await memory_manager.add_message(session, chat_id, "assistant", farewell_msg)
                        await message.reply(farewell_msg)
                        return
                    
                    ai_response_data = await llm.generate_response(messages_to_send)

                ai_response = ai_response_data.get("content", "Не удалось сгенерировать ответ.")
                
                if "<IGNORE>" in ai_response:
                    active_group_sessions[chat_id] = False
                    random_trigger_state.pop(chat_id, None)
                    return
                
                if "<END_CHAT>" in ai_response:
                    active_group_sessions[chat_id] = False
                    random_trigger_state.pop(chat_id, None)
                    farewell_msg = ai_response.replace("<END_CHAT>", "").replace("<IGNORE>", "").strip()
                    
                    if not farewell_msg:
                        farewell_msg = "Был рад пообщаться! Если что - пингуйте."
                        
                    await memory_manager.add_message(session, chat_id, "assistant", farewell_msg)
                    await message.reply(farewell_msg)
                    return
                
                random_trigger_state.pop(chat_id, None)
                await memory_manager.add_message(session, chat_id, "assistant", ai_response)
                await message.reply(ai_response)