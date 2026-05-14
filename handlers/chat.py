import asyncio
import base64
import json
import time
import re
import random
import logging
from io import BytesIO

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, ChatMemberUpdated
from sqlalchemy import select, delete
from sqlalchemy.orm import selectinload
import aiohttp
from thefuzz import fuzz

from database.engine import AsyncSessionLocal
from database.models import GlobalSettings, Group, ChatMember, UserNote
from services.llm_client import llm 
from services.memory_manager import MemoryManager
from config.config import config

router = Router()

memory_manager = MemoryManager()

active_group_sessions = {}
chat_locks = {}
last_message_times = {}
first_trigger_times = {} 
sleep_timers = {}
ignore_counters = {}
soft_trigger_cooldowns = {}
SOFT_TRIGGER_COOLDOWN = 600.0 

recent_chat_activity = {}
random_trigger_state = {}

http_session = None

async def perform_web_search(query: str, api_key: str) -> list:
    logging.info(f"[SEARCH] ИИ ищет в Tavily: {query}")
    if not api_key:
        logging.error("[SEARCH] Ключ TAVILY_API_KEY не настроен!")
        return []
    url = "https://api.tavily.com/search"
    payload = {
        "api_key": api_key, "query": query, "search_depth": "basic", 
        "include_answer": False, "max_results": 5
    }
    try:
        async with http_session.post(url, json=payload) as response:
            if response.status == 200:
                data = await response.json()
                results = data.get("results", [])
                logging.info(f"[SEARCH] Найдено результатов: {len(results)}")
                return results
            else:
                logging.error(f"[SEARCH] Ошибка HTTP: {response.status}")
                return []
    except Exception as e:
        logging.error(f"[SEARCH] Внутренняя ошибка запроса: {str(e)}")
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
        logging.info(f"[LLM TOOLS] [{chat_id}] Модель вызывает инструмент: {tool_call.function.name}")
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
    if message.from_user.id not in config.admin_ids:
        return
    if message.chat.type in ["group", "supergroup"]:
        chat_id = message.chat.id
        if active_group_sessions.get(chat_id, False):
            active_group_sessions[chat_id] = False
            ignore_counters[chat_id] = 0
            logging.warning(f"[ADMIN] [{chat_id}] Принудительное отключение бота командой /dismiss!")
            await message.reply("Принято. Принудительно отключаю активный режим...")
        else:
            await message.reply("Я и так нахожусь в спящем режиме. Пингуйте, если понадоблюсь.")

@router.message(Command("clean"))
async def cmd_clean(message: Message):
    if message.from_user.id not in config.admin_ids:
        return
    async with AsyncSessionLocal() as session:
        await memory_manager.clear_history(session, message.chat.id)
    active_group_sessions[message.chat.id] = False
    ignore_counters[message.chat.id] = 0
    logging.warning(f"[ADMIN] [{message.chat.id}] Очистка памяти чата.")
    await message.reply("Память очищена.")

@router.message(Command("sleep"))
async def cmd_sleep(message: Message):
    if message.from_user.id not in config.admin_ids:
        return
    parts = message.text.split()
    if len(parts) > 1 and parts[1].isdigit():
        minutes = int(parts[1])
        sleep_timers[message.chat.id] = time.time() + (minutes * 60)
        active_group_sessions[message.chat.id] = False
        ignore_counters[message.chat.id] = 0
        logging.warning(f"[ADMIN] [{message.chat.id}] Режим тишины включен на {minutes} минут.")
        await message.reply(f"Режим тишины на {minutes} минут.")
    else:
        await message.reply("Пожалуйста, укажите количество минут. Пример: /sleep 10")

@router.message(F.new_chat_members)
async def security_check_new_members(message: Message):
    bot_info = await message.bot.get_me()
    for member in message.new_chat_members:
        if member.id == bot_info.id:
            if message.from_user.id not in config.admin_ids:
                logging.warning(f"[SECURITY] Бот добавлен в чат {message.chat.id} чужаком {message.from_user.id}!")
                try:
                    await message.answer("Я приватный бот. У вас нет прав для моего использования.")
                except Exception:
                    pass
                await message.bot.leave_chat(message.chat.id)
                return
            else:
                logging.info(f"[SECURITY] Бот успешно добавлен доверенным лицом {message.from_user.id}.")

@router.message(F.chat.type == "private", (F.text | F.photo) & ~F.text.startswith("/") & ~F.caption.startswith("/"))
async def handle_private_messages(message: Message):
    is_admin = (message.from_user.id == config.admin_ids)
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
    chat_id = message.chat.id
    user_name = message.from_user.first_name

    raw_text = message.text or message.caption or ""
    clean_text = raw_text.replace(f"@{bot_username}", "").strip()
    history_text = f"{user_name}: {clean_text}" if clean_text else f"{user_name} отправил изображение."
    
    logging.info(f"[CHAT {chat_id}] Новое сообщение от {user_name}: {clean_text[:50]}...")

    is_reply_to_bot = False
    if message.reply_to_message and message.reply_to_message.from_user:
        is_reply_to_bot = (message.reply_to_message.from_user.id == bot_info.id)
        
    is_bot_mentioned = (f"@{bot_username}" in raw_text)

    base64_image = await _extract_image_base64(message)
    should_respond = False
    is_soft_triggered = False

    async with AsyncSessionLocal() as session:
        if clean_text:
            await memory_manager.add_message(session, chat_id, "user", history_text)
            stmt = select(ChatMember).where(ChatMember.group_id == chat_id, ChatMember.user_id == message.from_user.id)
            result = await session.execute(stmt)
            if not result.scalar_one_or_none():
                session.add(ChatMember(group_id=chat_id, user_id=message.from_user.id, user_name=user_name))
                await session.commit()

        if chat_id in sleep_timers:
            if time.time() < sleep_timers[chat_id]:
                logging.debug(f"[CHAT {chat_id}] Бот спит (режим /sleep). Игнорирую.")
                return 
            else:
                del sleep_timers[chat_id]
                logging.info(f"[CHAT {chat_id}] Режим /sleep завершен, бот проснулся.")

        is_active_mode = active_group_sessions.get(chat_id, False)
        
        is_reply_to_human = False
        if message.reply_to_message and message.reply_to_message.from_user:
            is_reply_to_human = (message.reply_to_message.from_user.id != bot_info.id)

        result = await session.execute(select(Group).options(selectinload(Group.triggers)).where(Group.chat_id == chat_id))
        group = result.scalar_one_or_none()

        debounce_val = group.debounce_seconds if group and group.debounce_seconds else 4.0
        max_wait_val = group.max_wait_seconds if group and group.max_wait_seconds else 15.0
        context_len = group.context_length if group and group.context_length else 10
        
        await memory_manager.enforce_limit(session, chat_id, context_len)

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
                            logging.info(f"[CHAT {chat_id}] Сработал мягкий триггер!")

        chance = (group.random_chance / 100.0) if group and group.random_chance is not None else 0.05

    logging.debug(f"[CHAT {chat_id}] Оценка триггеров: ReplyBot={is_reply_to_bot}, Mention={is_bot_mentioned}, Soft={is_soft_triggered}, ActiveMode={is_active_mode}, ReplyHuman={is_reply_to_human}")

    if is_reply_to_bot or is_bot_mentioned or is_soft_triggered:
        should_respond = True
        active_group_sessions[chat_id] = True
        logging.info(f"[CHAT {chat_id}] Прямое обращение! Включаю активный режим.")
    elif is_active_mode and not is_reply_to_human:
        should_respond = True
        logging.debug(f"[CHAT {chat_id}] Поддерживаю активный диалог.")

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
                    logging.info(f"[CHAT {chat_id}] Умный рандом! Врываюсь в чужой разговор.")
        else:
            if random.random() < chance:
                should_respond = True
                active_group_sessions[chat_id] = True
                logging.info(f"[CHAT {chat_id}] Обычный рандом. Бот решил ответить.")

    if clean_text.lower() in ["хватит", "стоп", "пока", "спи"]:
        if is_reply_to_bot or is_bot_mentioned or is_soft_triggered:
            active_group_sessions[chat_id] = False
            ignore_counters[chat_id] = 0
            logging.info(f"[CHAT {chat_id}] Жесткая команда ухода в сон от юзера.")
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

        logging.debug(f"[DEBOUNCE {chat_id}] Вхожу в ожидание {debounce_val} сек...")
        await message.bot.send_chat_action(chat_id=chat_id, action="typing")
        
        await asyncio.sleep(debounce_val)
        
        time_since_first = time.time() - first_trigger_times.get(chat_id, current_time)
        
        if last_message_times[chat_id] != current_time:
            if time_since_first < max_wait_val:
                logging.debug(f"[DEBOUNCE {chat_id}] Перебит другим сообщением. Отменяю задачу.")
                return 
            else:
                logging.warning(f"[DEBOUNCE {chat_id}] Достигнут жесткий лимит ожидания ({max_wait_val}с)! Форсирую ответ.")
        
        if chat_id not in chat_locks:
            chat_locks[chat_id] = asyncio.Lock()

        async with chat_locks[chat_id]:
            logging.info(f"[LOCK {chat_id}] Замок получен. Приступаю к формированию ответа.")
            if chat_id not in first_trigger_times and last_message_times[chat_id] != current_time:
                return
            
            if not is_reply_to_bot and not is_bot_mentioned and not is_soft_triggered:
                if not active_group_sessions.get(chat_id, False) and not random_trigger_state.get(chat_id):
                    logging.info(f"[LOCK {chat_id}] Отмена! Активный режим был выключен соседним процессом.")
                    return
            
            first_trigger_times.pop(chat_id, None)

            async with AsyncSessionLocal() as session:
                result = await session.execute(select(Group).where(Group.chat_id == chat_id))
                group = result.scalar_one_or_none()
                persona = group.active_persona if group and group.active_persona else "Ты умный участник чата."
                
                notes_stmt = select(ChatMember.user_id, ChatMember.user_name, UserNote.note_text).join(
                    UserNote, ChatMember.user_id == UserNote.user_id
                ).where(ChatMember.group_id == chat_id)
                
                notes_result = await session.execute(notes_stmt)
                all_chat_notes = notes_result.all()
                
                logging.info(f"[NOTES {chat_id}] Всего заметок в базе для этого чата: {len(all_chat_notes)}")
                
                chat_history = await memory_manager.get_context(session, chat_id, context_len)
                context_texts = [clean_text]
                for msg_dict in reversed(chat_history[-5:]):
                    if isinstance(msg_dict["content"], str):
                        context_texts.append(msg_dict["content"])
                
                expanded_msg_text = " ".join(context_texts)
                msg_lower = expanded_msg_text.lower()
                
                relevant_notes = []
                msg_words = [w for w in re.findall(r'\w+', msg_lower) if len(w) > 3]
                
                for member_id, member_name, note_text in all_chat_notes:
                    match_reason = None
                    
                    if member_id == message.from_user.id:
                        match_reason = "отправитель"
                        relevant_notes.append(f"Отправитель ({member_name}): {note_text}")
                    else:
                        member_name_lower = member_name.lower()
                        note_lower = note_text.lower()
                        note_words = [w for w in re.findall(r'\w+', note_lower) if len(w) > 3]
                        if len(member_name_lower) > 3 and fuzz.partial_ratio(member_name_lower, msg_lower) >= 70:
                            match_reason = f"нечеткое совпадение имени '{member_name}'"
                        else:
                            for m_word in msg_words:
                                for n_word in note_words:
                                    if fuzz.ratio(m_word, n_word) >= 70:
                                        match_reason = f"нечеткое совпадение слов: {m_word} ≈ {n_word}"
                                        break
                                if match_reason:
                                    break
                                    
                        if match_reason:
                            relevant_notes.append(f"Участник чата ({member_name}): {note_text}")
                    
                    if match_reason:
                        logging.info(f"[NOTES {chat_id}] МАТЧ! Добавляю заметку {member_name}. Причина: {match_reason}")
                
                user_notes_text = "\n".join(relevant_notes)
                if not user_notes_text:
                    logging.info(f"[NOTES {chat_id}] Поиск не дал результатов. В промпт ничего не добавлено.")

                paragraph_max = group.paragraph_max_sentences if group and group.paragraph_max_sentences else 3
                
                smart_exit_instruction = (
                    f"\n\nСИСТЕМНОЕ ПРАВИЛО: Форматирование абзацев - максимум {paragraph_max} предложений. "
                )
                
                if config.tools_enabled:
                    smart_exit_instruction += (
                        "Ты в режиме активного диалога. "
                        "Если с тобой попрощались или тема закрыта, ты ОБЯЗАН вызвать функцию 'end_active_dialogue' "
                        "и передать свой прощальный текст в аргумент 'farewell_message'. "
                    )
                else:
                    smart_exit_instruction += (
                        "Ты в режиме активного диалога. "
                        "Если с тобой попрощались или тема закрыта, ты ОБЯЗАН завершить ответ тегом <END_CHAT>. "
                    )

                smart_exit_instruction += (
                    "\n\n[КРИТИЧЕСКОЕ ПРАВИЛО ВМЕШАТЕЛЬСТВА]\n"
                    "ОБЯЗАТЕЛЬНО проанализируй последнее сообщение. Если участники явно общаются МЕЖДУ СОБОЙ, "
                    "если к тебе нет прямого обращения (по смыслу или имени) и твое мнение не требуется — "
                    "ТЫ ОБЯЗАН ОТВЕТИТЬ СТРОГО ОДНИМ ТЕГОМ <IGNORE> без какого-либо текста!\n"
                    "Не прощайся, не извиняйся и не комментируй, просто выведи <IGNORE>."
                )

                if not is_reply_to_bot and not is_bot_mentioned and not is_soft_triggered:
                    smart_exit_instruction += (
                        "\n\n(Системная подсказка от сервера: В последнем сообщении нет прямого обращения к тебе. "
                        "Скорее всего, это разговор людей между собой. Используй <IGNORE>, если не уверен на 100%, что нужно влезть)."
                    )

                if random_trigger_state.get(chat_id):
                    smart_exit_instruction += (
                        "\n\nВНЕЗАПНОЕ ВМЕШАТЕЛЬСТВО: Ты инициативно решил ворваться в разговор. "
                        "Выскажись, если есть смысл, иначе ОБЯЗАТЕЛЬНО ответь тегом <IGNORE>."
                    )
                    
                idle_timeout = (group.idle_timeout_minutes if group and group.idle_timeout_minutes else 5) * 60
                if active_group_sessions.get(chat_id) and (time.time() - last_message_times.get(chat_id, time.time()) > idle_timeout):
                    smart_exit_instruction += "\nВ диалоге была долгая пауза. Если тема закрыта, используй <IGNORE>."

                if group and group.chat_notes:
                    persona = f"{persona}\n\nПРАВИЛА И ЗАМЕТКИ ЧАТА:\n{group.chat_notes}"

                if user_notes_text:
                    persona = f"{persona}\n\nИНФОРМАЦИЯ ОБ УЧАСТНИКАХ (Досье):\n{user_notes_text}"
                    
                persona = f"{persona}\n{smart_exit_instruction}"
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
                                        "description": "Твой прощальный текст в стиле текущей персоны."
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
                                "description": "Искать информацию в интернете.",
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

                logging.info(f"[LLM REQ {chat_id}] Отправляю запрос в ИИ. Длина контекста: {len(messages_to_send)} сообщений.")
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
                        ignore_counters[chat_id] = 0
                        logging.info(f"[LLM RES {chat_id}] ИИ вызвал end_active_dialogue. Активный режим ВЫКЛЮЧЕН.")
                        await memory_manager.add_message(session, chat_id, "assistant", farewell_msg)
                        await message.reply(farewell_msg)
                        return
                    
                    logging.info(f"[LLM REQ {chat_id}] Отправляю результаты функций обратно в ИИ...")
                    ai_response_data = await llm.generate_response(messages_to_send)

                ai_response = ai_response_data.get("content", "Не удалось сгенерировать ответ.")
                
                if "<IGNORE>" in ai_response:
                    ignore_counters[chat_id] = ignore_counters.get(chat_id, 0) + 1
                    max_ignores = group.max_consecutive_ignores if group and group.max_consecutive_ignores else 3
                    logging.warning(f"[LLM RES {chat_id}] ИИ ответил <IGNORE>. Счетчик: {ignore_counters[chat_id]}/{max_ignores}")
                    
                    if ignore_counters[chat_id] >= max_ignores:
                        active_group_sessions[chat_id] = False
                        ignore_counters[chat_id] = 0
                        logging.warning(f"[CHAT {chat_id}] Превышен лимит IGNORE. Активный режим ВЫКЛЮЧЕН.")
                    random_trigger_state.pop(chat_id, None)
                    return
                
                ignore_counters[chat_id] = 0

                if "<END_CHAT>" in ai_response:
                    active_group_sessions[chat_id] = False
                    random_trigger_state.pop(chat_id, None)
                    farewell_msg = ai_response.replace("<END_CHAT>", "").replace("<IGNORE>", "").strip()
                    logging.info(f"[LLM RES {chat_id}] ИИ использовал тег <END_CHAT>. Активный режим ВЫКЛЮЧЕН.")
                    
                    if not farewell_msg:
                        farewell_msg = "Был рад пообщаться! Если что - пингуйте."
                        
                    await memory_manager.add_message(session, chat_id, "assistant", farewell_msg)
                    await message.reply(farewell_msg)
                    return
                
                random_trigger_state.pop(chat_id, None)
                logging.info(f"[LLM RES {chat_id}] Успешный ответ сгенерирован. Длина: {len(ai_response)} симв.")
                await memory_manager.add_message(session, chat_id, "assistant", ai_response)
                await message.reply(ai_response)