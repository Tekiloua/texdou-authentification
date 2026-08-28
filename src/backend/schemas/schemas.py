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


# Création d'un utilisateur depuis le backoffice (par un admin) : contrairement
# à /register (auto-inscription publique où le rôle est déduit du numéro), ici
# l'admin choisit explicitement le rôle.
class UserAdminCreate(BaseModel):
    username: Optional[str] = None
    numero:   int = Field(..., ge=100_000, lt=1_000_000)
    password: str
    role:     UserRole = UserRole.normal


# Mise à jour partielle d'un utilisateur depuis le backoffice : tous les
# champs sont optionnels, seuls ceux fournis sont modifiés. `password` vide
# ou absent = mot de passe inchangé.
class UserUpdate(BaseModel):
    username: Optional[str] = None
    numero:   Optional[int] = Field(None, ge=100_000, lt=1_000_000)
    password: Optional[str] = None
    role:     Optional[UserRole] = None


class DeleteUsersPayload(BaseModel):
    ids: list[int]

class ArrangePayload(BaseModel):
    source: str
    destination: str

class FileInfo(BaseModel):
    nom: str
    type: str

class ArrangeResponse(BaseModel):
    folder_parent: str
    files_childs: list[FileInfo]

class ArrangeState(BaseModel):
    source: str
    destination: str