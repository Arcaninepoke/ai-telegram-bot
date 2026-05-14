from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, Table, UniqueConstraint
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

group_admins = Table(
    'group_admins',
    Base.metadata,
    Column('user_id', Integer, ForeignKey('users.telegram_id'), primary_key=True),
    Column('group_id', Integer, ForeignKey('groups.chat_id'), primary_key=True)
)

class User(Base):
    __tablename__ = 'users'
    telegram_id = Column(Integer, primary_key=True)
    username = Column(String, nullable=True)
    full_name = Column(String, nullable=True)
    admin_in = relationship("Group", secondary=group_admins, back_populates="admins")

class SoftTrigger(Base):
    __tablename__ = 'soft_triggers'
    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey('groups.chat_id', ondelete="CASCADE"))
    word = Column(String, nullable=False)
    group = relationship("Group", back_populates="triggers")

class Group(Base):
    __tablename__ = 'groups'
    chat_id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    active_persona = Column(String, default="Ты умный ИИ-ассистент.")
    context_length = Column(Integer, default=10)
    random_chance = Column(Integer, default=5)
    admins = relationship("User", secondary=group_admins, back_populates="admin_in")
    triggers = relationship("SoftTrigger", back_populates="group", cascade="all, delete-orphan")

class GlobalSettings(Base):
    __tablename__ = 'global_settings'
    id = Column(Integer, primary_key=True)
    allow_all_pms = Column(Boolean, default=False)

class UserNote(Base):
    __tablename__ = 'user_notes'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False)
    category = Column(String, nullable=False)
    value = Column(String, nullable=False)
    __table_args__ = (UniqueConstraint('user_id', 'category', name='_user_category_uc'),)

class MessageHistory(Base):
    __tablename__ = 'message_history'
    id = Column(Integer, primary_key=True, autoincrement=True)
    chat_id = Column(Integer, nullable=False)
    role = Column(String, nullable=False)
    content = Column(String, nullable=False)