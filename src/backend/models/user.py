from sqlalchemy import Column, Integer, String, Enum
from sqlalchemy.orm import relationship
from src.backend.db.database import Base
import enum


class UserRole(str, enum.Enum):
    normal = "normal"
    expert = "expert"
    admin  = "admin"


class User(Base):
    __tablename__ = "users"

    id       = Column(Integer, primary_key=True, index=True)
    username = Column(String, index=True, nullable=True)

    # Stocké en String — la comparaison en BDD se fait toujours avec str(numero)
    numero   = Column(String, unique=True, index=True, nullable=False)

    hashed_password = Column(String, nullable=False)

    role = Column(
        Enum(UserRole),
        nullable=False,
        default=UserRole.normal,
        server_default=UserRole.normal.value,  # valeur par défaut côté BDD aussi
    )

    # Relation inverse attendue par Conversation.user (back_populates="conversations")
    conversations = relationship(
        "Conversation", back_populates="user", cascade="all, delete-orphan"
    )