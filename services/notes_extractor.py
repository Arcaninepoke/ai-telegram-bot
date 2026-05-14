import json
import logging
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from database.engine import AsyncSessionLocal
from database.models import UserNote
from services.llm_client import LLMClient

class NotesExtractor:
    def __init__(self, llm_client: LLMClient):
        self.llm = llm_client

    async def extract_and_save(self, user_id: int, user_name: str, text_batch: str):
        system_prompt = (
            "Проанализируй текст пользователя. Твоя задача - извлечь долгосрочные факты о нем. "
            "Игнорируй эмоции, временные состояния и пустую болтовню. "
            "Выдели только конкретику: имя, возраст, профессию, город, хобби, предпочтения. "
            "Верни ответ СТРОГО в формате JSON. Если фактов нет, верни пустой список.\n"
            "Формат:\n"
            "{\"facts\": [{\"category\": \"профессия\", \"value\": \"работает врачом\"}]}"
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Текст пользователя {user_name}:\n{text_batch}"}
        ]

        try:
            response_data = await self.llm.generate_response(messages, temperature=0.1)
            response_text = response_data.get("content", "")
            start_idx = response_text.find('{')
            end_idx = response_text.rfind('}') + 1
            if start_idx == -1 or end_idx == 0:
                return

            json_str = response_text[start_idx:end_idx]
            data = json.loads(json_str)

            if "facts" in data and isinstance(data["facts"], list):
                async with AsyncSessionLocal() as session:
                    for fact in data["facts"]:
                        category = fact.get("category")
                        value = fact.get("value")
                        
                        if category and value:
                            stmt = insert(UserNote).values(
                                user_id=user_id,
                                category=str(category).lower(),
                                value=str(value)
                            ).on_conflict_do_update(
                                index_elements=['user_id', 'category'],
                                set_=dict(value=str(value))
                            )
                            await session.execute(stmt)
                            
                    await session.commit()
                    logging.info(f"Сохранены новые факты для пользователя {user_name}")

        except json.JSONDecodeError:
            logging.error(f"Ошибка парсинга JSON от LLM для пользователя {user_name}")
        except Exception as e:
            logging.error(f"Внутренняя ошибка при извлечении фактов: {e}")

    @staticmethod
    async def get_user_notes_text(session: AsyncSession, user_id: int) -> str:
        stmt = select(UserNote).where(UserNote.user_id == user_id)
        result = await session.execute(stmt)
        notes = result.scalars().all()

        if not notes:
            return ""

        facts = [f"{note.category.capitalize()}: {note.value}" for note in notes]
        return "\n".join(facts)

    async def extract_from_admin(self, session: AsyncSession, target_user_id: int, admin_text: str):
        system_prompt = (
            "Тебе предоставлена сырая информация о пользователе. "
            "Твоя задача - извлечь из неё конкретные факты (имя, профессия, характер, хобби и т.д.). "
            "Верни ответ СТРОГО в формате JSON. Если фактов нет, верни пустой список.\n"
            "Формат:\n"
            "{\"facts\": [{\"category\": \"статус\", \"value\": \"нарушитель спокойствия\"}]}"
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": admin_text}
        ]

        try:
            response_data = await self.llm.generate_response(messages, temperature=0.1)
            response_text = response_data.get("content", "")

            start_idx = response_text.find('{')
            end_idx = response_text.rfind('}') + 1
            if start_idx == -1 or end_idx == 0:
                return False

            json_str = response_text[start_idx:end_idx]
            data = json.loads(json_str)

            if "facts" in data and isinstance(data["facts"], list):
                for fact in data["facts"]:
                    category = fact.get("category")
                    value = fact.get("value")
                    
                    if category and value:
                        stmt = insert(UserNote).values(
                            user_id=target_user_id,
                            category=str(category).lower(),
                            value=str(value)
                        ).on_conflict_do_update(
                            index_elements=['user_id', 'category'],
                            set_=dict(value=str(value))
                        )
                        await session.execute(stmt)
                await session.commit()
                return True
            return False
                
        except Exception as e:
            logging.error(f"Ошибка ручного извлечения: {e}")
            return False