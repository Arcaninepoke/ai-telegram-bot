from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from database.models import MessageHistory

class MemoryManager:
    async def add_message(self, session: AsyncSession, chat_id: int, role: str, content: str):
        new_msg = MessageHistory(chat_id=chat_id, role=role, content=content)
        session.add(new_msg)
        await session.commit()

    async def enforce_limit(self, session: AsyncSession, chat_id: int, limit: int):
        stmt = select(MessageHistory.id).where(MessageHistory.chat_id == chat_id).order_by(MessageHistory.id.desc()).offset(limit)
        result = await session.execute(stmt)
        old_ids = result.scalars().all()
        if old_ids:
            await session.execute(delete(MessageHistory).where(MessageHistory.id.in_(old_ids)))
            await session.commit()

    async def get_context(self, session: AsyncSession, chat_id: int, limit: int) -> list[dict]:
        stmt = (
            select(MessageHistory)
            .where(MessageHistory.chat_id == chat_id)
            .order_by(MessageHistory.id.desc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        messages = result.scalars().all()
        return [{"role": msg.role, "content": msg.content} for msg in reversed(messages)]

    async def clear_history(self, session: AsyncSession, chat_id: int):
        stmt = delete(MessageHistory).where(MessageHistory.chat_id == chat_id)
        await session.execute(stmt)
        await session.commit()