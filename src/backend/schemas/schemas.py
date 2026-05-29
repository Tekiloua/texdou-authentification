from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, ConfigDict


class UserRole(str, Enum):
    normal = "normal"
    expert = "expert"
    admin  = "admin"


class UserCreate(BaseModel):
    username: str
    numero:   int = Field(..., ge=100_000, lt=1_000_000)
    password: str


# Une seule définition — les deux doublons supprimés
class UserLogin(BaseModel):
    numero:   int = Field(..., ge=100_000, lt=1_000_000)
    password: str


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id:       int
    username: Optional[str]   # nullable=True dans le modèle SQLAlchemy
    numero:   str             # stocké en String dans la BDD
    role:     UserRole