from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from src.backend.db.database import Base


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True, index=True)
    titre = Column(String, nullable=True)  # titre optionnel, ex: "Discussion sur l'article X"
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relations
    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan")
    user = relationship("User", back_populates="conversations")