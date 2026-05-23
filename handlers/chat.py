import asyncio
import base64
import json
import time
import re
import random
import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message
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
last_operation_ids = {}

last_bot_response_times = {}
first_trigger_times = {}
sleep_timers = {}
ignore_counters = {}
soft_trigger_cooldowns = {}
SOFT_TRIGGER_COOLDOWN = 600.0

recent_chat_activity = {}
random_trigger_state = {}

http_session = None

MIN_WORDS_TO_RESPOND = 3

FAREWELL_PATTERNS = [
    "был рад", "рада помочь", "удачи всем", "пока всем",
    "спокойной ночи", "до встречи", "прощайте", "на связи",
    "всем удачи", "до свидания", "ухожу", "замолкаю"
]


def _is_topic_relevant(text: str, interests: str) -> tuple[bool, str]:
    if not interests or not interests.strip():
        return True, ""

    interest_list = [i.strip().lower() for i in interests.split(",") if i.strip()]
    text_words = [w for w in re.findall(r'\w+', text.lower()) if len(w) > 3]

    for interest in interest_list:
        interest_parts = [w for w in re.findall(r'\w+', interest) if len(w) > 3]
        for i_word in interest_parts:
            for t_word in text_words:
                if fuzz.ratio(i_word, t_word) >= 78:
                    return True, interest

    return False, ""


def _is_addressed_to_human(text: str, member_names: list[str]) -> str | None:
    if not text or not member_names:
        return None

    text_lower = text.lower()
    check_zone = text_lower[:35]

    for name in member_names:
        if len(name) < 3:
            continue
        if fuzz.partial_ratio(name, check_zone) >= 85:
            return name

    return None


def _strip_roleplay(text: str) -> str:
    text = re.sub(r'\*[^*\n]+\*', '', text)
    text = re.sub(r'_[^_\n]+_', '', text)
    text = re.sub(r'\([^)]*\s[а-яёa-z]+\s[а-яёa-z]+[^)]*\)', '', text, flags=re.IGNORECASE)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def _increment_ignore(chat_id: int, max_ignores: int, reason: str = "") -> bool:
    ignore_counters[chat_id] = ignore_counters.get(chat_id, 0) + 1
    count = ignore_counters[chat_id]
    label = f" ({reason})" if reason else ""
    logging.warning(f"[IGNORE {chat_id}] Счётчик: {count}/{max_ignores}{label}")

    if count >= max_ignores:
        active_group_sessions[chat_id] = False
        ignore_counters[chat_id] = 0
        random_trigger_state.pop(chat_id, None)
        logging.warning(f"[CHAT {chat_id}] Лимит IGNORE исчерпан — тихий выход из активного режима.")
        return True

    return False


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
    
    timeout = aiohttp.ClientTimeout(total=10.0)
    try:
        async with http_session.post(url, json=payload, timeout=timeout) as response:
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
                    logging.info(f"[LLM TOOLS] [{chat_id}] farewell_message из аргументов: '{tool_farewell[:80]}'")
                else:
                    logging.warning(f"[LLM TOOLS] [{chat_id}] farewell_message в аргументах пуст!")
            except Exception as e:
                logging.error(f"[LLM TOOLS] [{chat_id}] Не удалось распарсить аргументы инструмента: {e}")

            if not farewell_msg:
                logging.warning(f"[LLM TOOLS] [{chat_id}] Нет текста ни из аргументов, ни из контента — arg-фолбек пуст.")

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
                logging.error(f"[LLM TOOLS] [{chat_id}] Ошибка web_search: {e}")

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
            logging.warning(f"[ADMIN] [{chat_id}] Принудительное отключение командой /dismiss от {message.from_user.id}.")
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
    last_bot_response_times.pop(message.chat.id, None)
    logging.warning(f"[ADMIN] [{message.chat.id}] Очистка памяти чата от {message.from_user.id}.")
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
        logging.warning(f"[ADMIN] [{message.chat.id}] Режим тишины на {minutes} мин. от {message.from_user.id}.")
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
                logging.info(f"[SECURITY] Бот добавлен доверенным лицом {message.from_user.id} in чат {message.chat.id}.")


@router.message(F.chat.type == "private", (F.text | F.photo) & ~F.text.startswith("/") & ~F.caption.startswith("/"))
async def handle_private_messages(message: Message):
    is_admin = (message.from_user.id in config.admin_ids)
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
            stmt = select(ChatMember).where(
                ChatMember.group_id == chat_id,
                ChatMember.user_id == message.from_user.id
            )
            result = await session.execute(stmt)
            if not result.scalar_one_or_none():
                session.add(ChatMember(
                    group_id=chat_id,
                    user_id=message.from_user.id,
                    user_name=user_name
                ))
                await session.commit()
                logging.info(f"[CHAT {chat_id}] Новый участник добавлен: {user_name} ({message.from_user.id})")

        if chat_id in sleep_timers:
            if time.time() < sleep_timers[chat_id]:
                logging.debug(f"[CHAT {chat_id}] Бот спит (/sleep). Игнорирую.")
                return
            else:
                del sleep_timers[chat_id]
                logging.info(f"[CHAT {chat_id}] Режим /sleep завершён, бот проснулся.")

        is_active_mode = active_group_sessions.get(chat_id, False)

        is_reply_to_human = False
        if message.reply_to_message and message.reply_to_message.from_user:
            is_reply_to_human = (message.reply_to_message.from_user.id != bot_info.id)

        result = await session.execute(
            select(Group).options(selectinload(Group.triggers)).where(Group.chat_id == chat_id)
        )
        group = result.scalar_one_or_none()

        members_result = await session.execute(
            select(ChatMember.user_name).where(ChatMember.group_id == chat_id)
        )
        member_names = [
            row[0].lower() for row in members_result.all()
            if row[0] and row[0].lower() != user_name.lower()
        ]

        debounce_val = group.debounce_seconds if group and group.debounce_seconds else 4.0
        max_wait_val = group.max_wait_seconds if group and group.max_wait_seconds else 15.0
        context_len = group.context_length if group and group.context_length else 10
        max_ignores = group.max_consecutive_ignores if group and group.max_consecutive_ignores else 3

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
                        last_soft = soft_trigger_cooldowns.get(chat_id, 0)
                        if time.time() - last_soft > SOFT_TRIGGER_COOLDOWN:
                            is_soft_triggered = True
                            soft_trigger_cooldowns[chat_id] = time.time()
                            logging.info(f"[CHAT {chat_id}] Сработал мягкий триггер!")

        chance = (group.random_chance / 100.0) if group and group.random_chance is not None else 0.05
        interests = getattr(group, 'persona_interests', '') or ''

    is_direct_address = is_reply_to_bot or is_bot_mentioned or is_soft_triggered

    logging.debug(
        f"[CHAT {chat_id}] Триггеры: ReplyBot={is_reply_to_bot}, Mention={is_bot_mentioned}, "
        f"Soft={is_soft_triggered}, ActiveMode={is_active_mode}, "
        f"ReplyHuman={is_reply_to_human}, DirectAddress={is_direct_address}"
    )

    if is_direct_address:
        should_respond = True
        active_group_sessions[chat_id] = True
        logging.info(f"[CHAT {chat_id}] Прямое обращение! Включаю активный режим.")

    elif is_active_mode and not is_reply_to_human:
        addressed_to = _is_addressed_to_human(clean_text, member_names)
        if addressed_to:
            logging.info(
                f"[CHAT {chat_id}] Active mode, но сообщение адресовано участнику "
                f"'{addressed_to}' — тихий IGNORE."
            )
            _increment_ignore(chat_id, max_ignores, reason=f"обращение к '{addressed_to}'")
            random_trigger_state.pop(chat_id, None)
            return
        should_respond = True
        logging.debug(f"[CHAT {chat_id}] Поддерживаю активный диалог.")

    if not should_respond and not is_active_mode and not is_reply_to_human and chance > 0:
        now = time.time()
        if chat_id not in recent_chat_activity:
            recent_chat_activity[chat_id] = []
        recent_chat_activity[chat_id].append((now, message.from_user.id))
        recent_chat_activity[chat_id] = [
            (t, uid) for t, uid in recent_chat_activity[chat_id] if now - t < 60
        ]

        unique_users = len(set(uid for _, uid in recent_chat_activity[chat_id]))
        msg_count = len(recent_chat_activity[chat_id])
        is_long_msg = len(clean_text.split()) >= 12
        is_active_discussion = msg_count >= 5 and unique_users >= 2

        if config.tools_enabled:
            if is_long_msg or is_active_discussion:
                is_relevant, matched_interest = _is_topic_relevant(clean_text, interests)
                if not is_relevant:
                    logging.debug(
                        f"[RANDOM {chat_id}] Тема не близка персоне. "
                        f"Интересы: '{interests}'. Сообщение: '{clean_text[:60]}'"
                    )
                else:
                    if random.random() < chance:
                        should_respond = True
                        active_group_sessions[chat_id] = True
                        random_trigger_state[chat_id] = matched_interest
                        recent_chat_activity[chat_id].clear()
                        logging.info(
                            f"[RANDOM {chat_id}] Релевантное вмешательство! "
                            f"Совпадение: '{matched_interest or 'общее'}'. "
                            f"Длинное: {is_long_msg}, Активное: {is_active_discussion}."
                        )
                    else:
                        logging.debug(
                            f"[RANDOM {chat_id}] Тема подходит, "
                            f"но кубик не выпал (chance={chance:.0%})."
                        )
        else:
            is_relevant, matched_interest = _is_topic_relevant(clean_text, interests)
            if not is_relevant:
                logging.debug(
                    f"[RANDOM {chat_id}] (без tools) Тема не близка персоне. "
                    f"Интересы: '{interests}'. Сообщение: '{clean_text[:60]}'"
                )
            else:
                if random.random() < chance:
                    should_respond = True
                    active_group_sessions[chat_id] = True
                    random_trigger_state[chat_id] = matched_interest
                    logging.info(
                        f"[RANDOM {chat_id}] (без tools) Релевантное вмешательство! "
                        f"Совпадение: '{matched_interest or 'общее'}'."
                    )
                else:
                    logging.debug(
                        f"[RANDOM {chat_id}] (без tools) Тема подходит, "
                        f"но кубик не выпал (chance={chance:.0%})."
                    )

    if clean_text.lower() in ["хватит", "стоп", "пока", "спи"]:
        if is_direct_address:
            active_group_sessions[chat_id] = False
            ignore_counters[chat_id] = 0
            logging.info(f"[CHAT {chat_id}] Команда сна. Ухожу в спящий режим.")
            await message.reply("Понял, ухожу в спящий режим. Зовите, если понадоблюсь!")
            return

    if not should_respond:
        return

    if not is_direct_address and not message.photo:
        word_count = len(clean_text.split())
        if word_count < MIN_WORDS_TO_RESPOND:
            logging.info(
                f"[CHAT {chat_id}] Короткое сообщение ({word_count} сл.) "
                f"без прямого обращения — тихий IGNORE."
            )
            _increment_ignore(chat_id, max_ignores, reason=f"короткое: '{clean_text}'")
            random_trigger_state.pop(chat_id, None)
            return

    current_op_id = last_operation_ids.get(chat_id, 0) + 1
    last_operation_ids[chat_id] = current_op_id

    logging.debug(f"[DEBOUNCE {chat_id}] Ожидание {debounce_val} сек...")
    await message.bot.send_chat_action(chat_id=chat_id, action="typing")
    
    if chat_id not in first_trigger_times:
        first_trigger_times[chat_id] = time.time()

    await asyncio.sleep(debounce_val)

    time_since_first = time.time() - first_trigger_times.get(chat_id, time.time())

    if last_operation_ids.get(chat_id) != current_op_id:
        if time_since_first < max_wait_val:
            logging.debug(f"[DEBOUNCE {chat_id}] Операция #{current_op_id} заменена более новой. Отмена.")
            return
        else:
            logging.warning(f"[DEBOUNCE {chat_id}] Жёсткий лимит {max_wait_val}с превышен. Пробиваем ответ.")

    if chat_id not in chat_locks:
        chat_locks[chat_id] = asyncio.Lock()

    async with chat_locks[chat_id]:
        logging.info(f"[LOCK {chat_id}] Замок получен для операции #{current_op_id}. Формирую ответ.")
        if chat_id not in first_trigger_times and last_operation_ids.get(chat_id) != current_op_id:
            logging.debug(f"[LOCK {chat_id}] Задача устарела — параллельный поток уже обработал стек.")
            return

        if not is_direct_address:
            if not active_group_sessions.get(chat_id, False) and chat_id not in random_trigger_state:
                logging.info(f"[LOCK {chat_id}] Активный режим выключен соседним процессом. Отмена.")
                return

        first_trigger_times.pop(chat_id, None)

        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Group).where(Group.chat_id == chat_id))
            group = result.scalar_one_or_none()
            persona = group.active_persona if group and group.active_persona else "Ты умный участник чата."
            mode = getattr(group, 'mode', 'chat') or 'chat'

            notes_stmt = select(ChatMember.user_id, ChatMember.user_name, UserNote.note_text).join(
                UserNote, ChatMember.user_id == UserNote.user_id
            ).where(ChatMember.group_id == chat_id)
            notes_result = await session.execute(notes_stmt)
            all_chat_notes = notes_result.all()

            logging.info(f"[NOTES {chat_id}] Загружено заметок: {len(all_chat_notes)}")

            chat_history = await memory_manager.get_context(session, chat_id, context_len)
            context_texts = [clean_text]
            for msg_dict in reversed(chat_history[-5:]):
                if isinstance(msg_dict["content"], str):
                    context_texts.append(msg_dict["content"])

            expanded_msg_text = " ".join(context_texts)
            msg_lower = expanded_msg_text.lower()
            msg_words = [w for w in re.findall(r'\w+', msg_lower) if len(w) > 3]

            relevant_notes = []
            for member_id, member_name, note_text in all_chat_notes:
                match_reason = None

                if member_id == message.from_user.id:
                    match_reason = "отправитель"
                    relevant_notes.append(f"{member_name} (пишет сейчас): {note_text}")
                else:
                    member_name_lower = member_name.lower()
                    note_lower = note_text.lower()
                    note_words = [w for w in re.findall(r'\w+', note_lower) if len(w) > 3]
                    if len(member_name_lower) > 3 and fuzz.partial_ratio(member_name_lower, msg_lower) >= 70:
                        match_reason = f"нечёткое совпадение имени '{member_name}'"
                    else:
                        for m_word in msg_words:
                            for n_word in note_words:
                                if fuzz.ratio(m_word, n_word) >= 70:
                                    match_reason = f"совпадение слов: {m_word} ≈ {n_word}"
                                    break
                            if match_reason:
                                break
                    if match_reason:
                        relevant_notes.append(f"{member_name}: {note_text}")

                if match_reason:
                    logging.info(f"[NOTES {chat_id}] Матч заметки {member_name}. Причина: {match_reason}")

            user_notes_text = "\n".join(relevant_notes)
            if not user_notes_text:
                logging.info(f"[NOTES {chat_id}] Релевантных заметок не найдено.")

            paragraph_max = group.paragraph_max_sentences if group and group.paragraph_max_sentences else 3
            prompt_parts = [f"Не более {paragraph_max} предложений подряд."]

            if config.tools_enabled:
                prompt_parts.append(
                    "Завершение диалога — ТОЛЬКО через вызов end_active_dialogue. "
                    "Написать прощание текстом без вызова инструмента — запрещено."
                )
            else:
                prompt_parts.append("Диалог завершён → закончи ответ тегом <END_CHAT>.")

            if not is_direct_address:
                prompt_parts.append(
                    "Тебе запрещено отвечать если в сообщении есть обращение к другому человеку по имени, "
                    "или люди явно разговаривают между собой. "
                    "Примеры когда НУЖЕН <IGNORE>: 'Саш, ты видел?', 'да ладно', 'лол', 'окей'. "
                    "При малейшем сомнении — <IGNORE>."
                )

                current_ignore_count = ignore_counters.get(chat_id, 0)
                if current_ignore_count > 0:
                    prompt_parts.append(
                        f"ВАЖНО: в последних {current_ignore_count} сообщениях ты уже определил "
                        "что разговор идёт не с тобой. Скорее всего следующее сообщение тоже не к тебе — "
                        "применяй <IGNORE> при малейшем сомнении."
                    )
                    logging.debug(f"[PROMPT {chat_id}] Усиленный IGNORE (счётчик {current_ignore_count}).")

                if chat_id in random_trigger_state:
                    matched = random_trigger_state[chat_id]
                    if matched:
                        prompt_parts.append(
                            f"Ты вошёл в разговор потому что тема затронула «{matched}» — "
                            "область твоей прямой специальности или личного интереса. "
                            "Вступай ТОЛЬКО если тебе действительно есть что добавить по существу. "
                            "Если разговор ушёл в сторону или люди общаются между собой — строго <IGNORE>."
                        )
                        logging.debug(f"[PROMPT {chat_id}] Подсказка вмешательства: интерес «{matched}».")
                    else:
                        prompt_parts.append(
                            "(Ты вошёл инициативно — применяй <IGNORE> при малейшем сомнении.)"
                        )

                logging.debug(f"[PROMPT {chat_id}] Добавлено правило IGNORE (нет прямого обращения).")
            else:
                logging.debug(f"[PROMPT {chat_id}] Правило IGNORE пропущено (прямое обращение).")

            idle_timeout = (group.idle_timeout_minutes if group and group.idle_timeout_minutes else 5) * 60
            last_response = last_bot_response_times.get(chat_id, 0)
            if active_group_sessions.get(chat_id) and last_response > 0:
                idle_elapsed = time.time() - last_response
                if idle_elapsed > idle_timeout:
                    prompt_parts.append(
                        "В диалоге была долгая пауза. Если тема закрыта — используй <IGNORE>."
                    )
                    logging.info(
                        f"[PROMPT {chat_id}] Idle timeout: {idle_elapsed:.0f}с без ответа. Добавлена подсказка."
                    )

            smart_exit_instruction = "\n\n<rules>\n" + "\n".join(prompt_parts) + "\n</rules>"

            if group and group.chat_notes:
                persona = f"{persona}\n\n[Правила чата]\n{group.chat_notes}"
            if user_notes_text:
                persona = f"{persona}\n\n[Участники]\n{user_notes_text}"

            if mode == 'chat':
                mode_block = (
                    "<mode>chat</mode>\n"
                    "Ты участник группового чата в мессенджере. Пишешь обычные текстовые сообщения.\n"
                    "ЗАПРЕЩЕНО: *ролевые действия*, описания от третьего лица, нарратив, театральные паузы.\n"
                    "Любое событие или эмоцию — выражай словами как в обычном чате."
                )
                system_content = f"{mode_block}\n\n{smart_exit_instruction}\n\n{persona}"
                logging.debug(f"[MODE {chat_id}] Режим: чат.")
            else:
                system_content = f"{smart_exit_instruction}\n\n{persona}"
                logging.debug(f"[MODE {chat_id}] Режим: ролевой.")

            messages_to_send = [{"role": "system", "content": system_content}]
            messages_to_send.extend(chat_history)

            if message.photo:
                if base64_image:
                    last_msg = messages_to_send.pop()
                    user_content = [{"type": "text", "text": last_msg["content"]}]
                    if not clean_text:
                        user_content[0]["text"] = f"{user_name} показывает это изображение. Что на нём?"
                    user_content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}
                    })
                    messages_to_send.append({"role": "user", "content": user_content})
                else:
                    last_msg = messages_to_send.pop()
                    text_context = last_msg["content"] if clean_text else f"{user_name} отправил картинку."
                    user_content = (
                        f"{text_context}\n\n"
                        f"[Системное уведомление: Пользователь прислал изображение, но функция просмотра картинок (Vision) "
                        f"сейчас отключена или недоступна. Ответь строго в стиле своего персонажа и роли, "
                        f"что ты не можешь открыть, обработать или увидеть это изображение.]"
                    )
                    messages_to_send.append({"role": "user", "content": user_content})
            else:
                if not clean_text:
                    last_msg = messages_to_send.pop()
                    user_content = (
                        f"[Системное уведомление: Получено пустое сообщение или пинг. "
                        f"Ответь в стиле своего персонажа, проявив инициативу или поинтересовавшись, о чем идет речь.]"
                    )
                    messages_to_send.append({"role": "user", "content": user_content})

            if mode == 'chat':
                messages_to_send.insert(-1, {
                    "role": "user",
                    "content": "[режим: чат. Только текст, без *действий*]"
                })
                messages_to_send.insert(-1, {
                    "role": "assistant",
                    "content": "[принято]"
                })

            available_tools = []
            if config.tools_enabled:
                available_tools.append({
                    "type": "function",
                    "function": {
                        "name": "end_active_dialogue",
                        "description": (
                            "Завершить активный диалог. "
                            "Вызывай ТОЛЬКО если прощание или просьба уйти адресована именно ТЕБЕ: "
                            "'бот, хватит', 'спасибо, всё понял', 'иди спать', 'не мешай нам'. "
                            "НЕ вызывай если: участник сам уходит ('я пошёл спать', 'мне пора'), "
                            "люди прощаются между собой ('пока Саша'), тема просто сменилась."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "farewell_message": {
                                    "type": "string",
                                    "description": "Прощальная фраза в стиле персоны."
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

            logging.info(
                f"[LLM REQ {chat_id}] Запрос в LLM. "
                f"Контекст: {len(messages_to_send)} сообщений, "
                f"инструментов: {len(available_tools)}, режим: {mode}."
            )
            await message.bot.send_chat_action(chat_id=chat_id, action="typing")
            
            ai_response_data = await llm.generate_response(
                messages_to_send,
                tools=available_tools if available_tools else None
            )

            loops_count = 0
            MAX_TOOL_LOOPS = 3
            
            while ai_response_data.get("type") == "tool_calls" and loops_count < MAX_TOOL_LOOPS:
                loops_count += 1
                tool_calls = ai_response_data.get("tool_calls")
                message_obj = ai_response_data.get("message_obj")

                force_exit, farewell_arg, messages_to_send = await _process_llm_tools(
                    tool_calls, message_obj, messages_to_send, chat_id
                )

                if force_exit:
                    if not is_direct_address:
                        logging.warning(
                            f"[FAREWELL {chat_id}] end_active_dialogue без прямого обращения — "
                            f"тихий IGNORE. farewell_arg был: '{farewell_arg[:60]}'"
                        )
                        _increment_ignore(chat_id, max_ignores, reason="ложный end_active_dialogue")
                        random_trigger_state.pop(chat_id, None)
                        return

                    active_group_sessions[chat_id] = False
                    random_trigger_state.pop(chat_id, None)
                    ignore_counters[chat_id] = 0

                    farewell_msg = farewell_arg

                    if not farewell_msg:
                        logging.warning(
                            f"[FAREWELL {chat_id}] farewell_arg пуст — запрашиваю второй вызов LLM."
                        )
                        await message.bot.send_chat_action(chat_id=chat_id, action="typing")
                        farewell_messages = messages_to_send[:-1]
                        farewell_messages.append({
                            "role": "user",
                            "content": "(попрощайся с чатом в своём стиле, одна фраза)"
                        })
                        farewell_data = await llm.generate_response(farewell_messages)
                        farewell_msg = (farewell_data.get("content") or "").strip()

                        if not farewell_msg or farewell_data.get("type") == "tool_calls":
                            logging.error(f"[FAREWELL {chat_id}] Все источники пусты — отправляю дефолт.")
                            farewell_msg = "Удачи!"

                    if mode == 'chat':
                        farewell_msg = _strip_roleplay(farewell_msg) or farewell_msg

                    logging.info(f"[FAREWELL {chat_id}] Прощание отправлено: {farewell_msg[:80]}")
                    last_bot_response_times[chat_id] = time.time()
                    await memory_manager.add_message(session, chat_id, "assistant", farewell_msg)
                    await message.reply(farewell_msg)
                    return

                logging.info(f"[LLM REQ {chat_id}] Итерация #{loops_count}. Возвращаем результаты инструментов в LLM...")
                await message.bot.send_chat_action(chat_id=chat_id, action="typing")
                ai_response_data = await llm.generate_response(messages_to_send, tools=available_tools if available_tools else None)

            ai_response = ai_response_data.get("content", "Не удалось сгенерировать ответ.")

            if mode == 'chat':
                stripped = _strip_roleplay(ai_response)
                if stripped != ai_response:
                    logging.warning(
                        f"[MODE {chat_id}] Стрип: удалено {len(ai_response) - len(stripped)} симв."
                    )
                if len(stripped) < 5 and len(ai_response) > 10:
                    logging.warning(f"[MODE {chat_id}] После стрипа ответ пуст — запрашиваю повторно.")
                    messages_to_send.append({"role": "assistant", "content": ai_response})
                    messages_to_send.append({"role": "user", "content": "[только текст, без действий]"})
                    await message.bot.send_chat_action(chat_id=chat_id, action="typing")
                    retry_data = await llm.generate_response(messages_to_send)
                    stripped = _strip_roleplay(retry_data.get("content", ""))
                    if not stripped:
                        logging.error(f"[MODE {chat_id}] Повтор тоже дал пустоту. Пропускаю ответ.")
                        return
                if stripped:
                    ai_response = stripped

            if "<IGNORE>" in ai_response:
                _increment_ignore(chat_id, max_ignores, reason="явный тег от модели")
                random_trigger_state.pop(chat_id, None)
                return

            ignore_counters[chat_id] = 0
            random_trigger_state.pop(chat_id, None)

            if any(p in ai_response.lower() for p in FAREWELL_PATTERNS):
                if not is_direct_address:
                    logging.warning(
                        f"[FAREWELL {chat_id}] Прощальный паттерн без прямого обращения — "
                        "тихий IGNORE, не отправляю."
                    )
                    _increment_ignore(chat_id, max_ignores, reason="ложный farewell в тексте")
                    return
                else:
                    logging.info(
                        f"[FAREWELL {chat_id}] Текстовое прощание при прямом обращении — завершаю сессию."
                    )
                    active_group_sessions[chat_id] = False

            if "<END_CHAT>" in ai_response:
                if not is_direct_address:
                    logging.warning(
                        f"[FAREWELL {chat_id}] END_CHAT без прямого обращения — тихий IGNORE."
                    )
                    _increment_ignore(chat_id, max_ignores, reason="ложный END_CHAT")
                    return

                active_group_sessions[chat_id] = False
                farewell_msg = ai_response.replace("<END_CHAT>", "").replace("<IGNORE>", "").strip()
                logging.info(f"[LLM RES {chat_id}] Тег <END_CHAT>. Активный режим ВЫКЛЮЧЕН.")

                if not farewell_msg:
                    farewell_msg = "Был рад пообщаться! Если что — пингуйте."

                last_bot_response_times[chat_id] = time.time()
                await memory_manager.add_message(session, chat_id, "assistant", farewell_msg)
                await message.reply(farewell_msg)
                return

            logging.info(f"[LLM RES {chat_id}] Ответ сгенерирован ({len(ai_response)} симв.).")
            last_bot_response_times[chat_id] = time.time()
            await memory_manager.add_message(session, chat_id, "assistant", ai_response)
            await message.reply(ai_response)