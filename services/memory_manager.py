import asyncio
import logging
from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from database.engine import AsyncSessionLocal
from database.models import MessageHistory
from services.llm_client import LLMClient

class MemoryManager:
    def __init__(self, llm_client: LLMClient):
        self.llm = llm_client

    async def add_message(self, session: AsyncSession, chat_id: int, role: str, content: str):
        new_msg = MessageHistory(
            chat_id=chat_id,
            role=role,
            content=content
        )
        session.add(new_msg)
        await session.commit()
        asyncio.create_task(self._safe_optimize(chat_id))

    async def _safe_optimize(self, chat_id: int):
        try:
            async with AsyncSessionLocal() as session:
                await self.optimize_history(session, chat_id)
        except Exception as e:
            logging.error(f"[ERROR MEMORY] Ошибка при оптимизации БД для чата {chat_id}: {e}")

    async def optimize_history(self, session: AsyncSession, chat_id: int, limit: int = 100):
        stmt = select(func.count(MessageHistory.id)).where(MessageHistory.chat_id == chat_id)
        result = await session.execute(stmt)
        count = result.scalar()

        if count and count > limit:
            compress_count = limit // 2
            
            stmt = select(MessageHistory).where(
                MessageHistory.chat_id == chat_id,
                MessageHistory.content.not_like("[СИСТЕМНАЯ ПАМЯТЬ%")
            ).order_by(MessageHistory.id.asc()).limit(compress_count)
            result = await session.execute(stmt)
            old_messages = result.scalars().all()

            if not old_messages:
                return

            text_to_summarize = "\n".join([f"{msg.role}: {msg.content}" for msg in old_messages])
            
            prompt = (
                "Сделай краткую выжимку (summary) следующего диалога. "
                "Опиши суть разговора, ключевые факты и к чему пришли участники. "
                "Ответ должен быть в виде одного короткого, плотного абзаца."
            )
            
            messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": text_to_summarize}
            ]
            
            response_data = await self.llm.generate_response(messages, temperature=0.3)
            summary = response_data.get("content", "")
            
            if summary:
                summary_msg = MessageHistory(
                    chat_id=chat_id,
                    role="system",
                    content=f"[СИСТЕМНАЯ ПАМЯТЬ ПРОШЛЫХ ДИАЛОГОВ]\n{summary}"
                )
                session.add(summary_msg)
                
                ids_to_delete = [msg.id for msg in old_messages]
                del_stmt = delete(MessageHistory).where(MessageHistory.id.in_(ids_to_delete))
                await session.execute(del_stmt)
                
                await session.commit()
                print(f"[DEBUG MEMORY] Успешно сжато {compress_count} сообщений для чата {chat_id}")

    async def get_context(self, session: AsyncSession, chat_id: int, limit: int) -> list[dict]:
        stmt_mem = select(MessageHistory).where(
            MessageHistory.chat_id == chat_id, 
            MessageHistory.content.like("[СИСТЕМНАЯ ПАМЯТЬ%")
        ).order_by(MessageHistory.id.desc()).limit(1)
        mem_result = await session.execute(stmt_mem)
        memory_msg = mem_result.scalar_one_or_none()

        stmt = (
            select(MessageHistory)
            .where(MessageHistory.chat_id == chat_id)
            .where(MessageHistory.content.not_like("[СИСТЕМНАЯ ПАМЯТЬ%"))
            .order_by(MessageHistory.id.desc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        messages = result.scalars().all()
        
        history = [
            {"role": msg.role, "content": msg.content}
            for msg in reversed(messages)
        ]
        
        if memory_msg:
            history.insert(0, {"role": memory_msg.role, "content": memory_msg.content})
            
        return history