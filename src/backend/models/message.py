from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text, Enum
from sqlalchemy.orm import relationship
from datetime import datetime
import enum
from src.backend.db.database import Base


class RoleEnum(str, enum.Enum):
    user = "user"
    assistant = "assistant"


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False)
    role = Column(Enum(RoleEnum), nullable=False)  # "user" ou "assistant"
    contenu = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relation
    conversation = relationship("Conversation", back_populates="messages")