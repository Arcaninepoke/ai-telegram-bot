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
    chat_notes = Column(String, nullable=True)
    idle_timeout_minutes = Column(Integer, default=5)
    max_consecutive_ignores = Column(Integer, default=3)
    debounce_seconds = Column(Integer, default=4)
    max_wait_seconds = Column(Integer, default=15)
    paragraph_max_sentences = Column(Integer, default=3)
    admins = relationship("User", secondary=group_admins, back_populates="admin_in")
    triggers = relationship("SoftTrigger", back_populates="group", cascade="all, delete-orphan")
    members = relationship("ChatMember", back_populates="group", cascade="all, delete-orphan")

class GlobalSettings(Base):
    __tablename__ = 'global_settings'
    id = Column(Integer, primary_key=True)
    allow_all_pms = Column(Boolean, default=False)

class UserNote(Base):
    __tablename__ = 'user_notes'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, unique=True, nullable=False)
    note_text = Column(String, nullable=False)

class MessageHistory(Base):
    __tablename__ = 'message_history'
    id = Column(Integer, primary_key=True, autoincrement=True)
    chat_id = Column(Integer, nullable=False)
    role = Column(String, nullable=False)
    content = Column(String, nullable=False)

class ChatMember(Base):
    __tablename__ = 'chat_members'
    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey('groups.chat_id', ondelete="CASCADE"))
    user_id = Column(Integer, nullable=False)
    user_name = Column(String, nullable=False)
    group = relationship("Group", back_populates="members")
    __table_args__ = (UniqueConstraint('group_id', 'user_id', name='_group_user_uc'),)