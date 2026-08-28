from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Response, Cookie
from sqlalchemy.orm import Session

from src.backend.auth.auth import (
    create_access_token,
    hash_password,
    verify_password,
    verify_token,
)
from src.backend.models.user import User, UserRole
from src.backend.schemas.schemas import (
    UserLogin,
    UserCreate,
    UserResponse,
    UserAdminCreate,
    UserUpdate,
    DeleteUsersPayload,
)
from src.backend.db.database import get_db

router = APIRouter()

ACCESS_TOKEN_EXPIRE = timedelta(hours=12)
COOKIE_NAME = "access_token"


def _set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=False,      # True en production (HTTPS)
        samesite="lax",
        max_age=int(ACCESS_TOKEN_EXPIRE.total_seconds()),
    )


# ─── LOGIN ───────────────────────────────────────────────────────────────────

@router.post("/login")
def login(user: UserLogin, response: Response, db: Session = Depends(get_db)):
    # numero est un int côté Pydantic mais un String en BDD → comparer avec str()
    db_user = db.query(User).filter(User.numero == str(user.numero)).first()

    if not db_user or not verify_password(user.password, db_user.hashed_password):
        raise HTTPException(status_code=401, detail="Identifiants invalides")

    access_token = create_access_token(
        {"sub": db_user.numero, "role": db_user.role.value},
        expires_delta=ACCESS_TOKEN_EXPIRE,
    )

    _set_auth_cookie(response, access_token)

    return {"role": db_user.role.value}


# ─── REGISTER ────────────────────────────────────────────────────────────────

@router.post("/register", response_model=UserResponse)
def register(user: UserCreate, db: Session = Depends(get_db)):
    if user.numero < 100_000 or user.numero >= 1_000_000:
        raise HTTPException(status_code=400, detail="Matricule invalide (100000–999999)")

    if db.query(User).filter(User.numero == str(user.numero)).first():
        raise HTTPException(status_code=400, detail="Numéro déjà utilisé")

    if user.numero in (100_000, 100_001):
        role = UserRole.admin
    elif 100_002 < user.numero < 100_100:
        role = UserRole.expert
    else:
        role = UserRole.normal

    new_user = User(
        username=user.username,
        numero=str(user.numero),
        hashed_password=hash_password(user.password),
        role=role,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    return new_user


# ─── LOGOUT ──────────────────────────────────────────────────────────────────

@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(key=COOKIE_NAME)
    return {"message": "Déconnecté"}


# ─── DÉPENDANCE : UTILISATEUR COURANT ────────────────────────────────────────

def get_current_user(
    access_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
) -> User:
    """À utiliser avec Depends() sur toute route protégée."""
    if not access_token:
        raise HTTPException(status_code=401, detail="Non authentifié")

    payload = verify_token(access_token)
    if not payload:
        raise HTTPException(status_code=401, detail="Token invalide ou expiré")

    db_user = db.query(User).filter(User.numero == payload["sub"]).first()
    if not db_user:
        raise HTTPException(status_code=401, detail="Utilisateur introuvable")

    return db_user


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role.value != UserRole.admin.value:
        raise HTTPException(status_code=403, detail="Accès réservé aux administrateurs")
    return current_user


# Autorise à la fois les admins et les experts. Utilisée pour la gestion des
# utilisateurs (backoffice), afin que les experts puissent aussi consulter,
# créer, modifier et supprimer des comptes — pas seulement les admins.
def require_admin_or_expert(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role.value not in (UserRole.admin.value, UserRole.expert.value):
        raise HTTPException(
            status_code=403, detail="Accès réservé aux administrateurs et experts"
        )
    return current_user


@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    return current_user

@router.get("/users", response_model=list[UserResponse])
def get_all_users(
    caller: User = Depends(require_admin_or_expert), db: Session = Depends(get_db)
):
    return db.query(User).order_by(User.id).all()


# ─── GESTION DES UTILISATEURS (BACKOFFICE, ADMIN + EXPERT) ───────────────────

@router.post("/users", response_model=UserResponse, status_code=201)
def create_user(
    user: UserAdminCreate,
    caller: User = Depends(require_admin_or_expert),
    db: Session = Depends(get_db),
):
    if db.query(User).filter(User.numero == str(user.numero)).first():
        raise HTTPException(status_code=400, detail="Numéro déjà utilisé")

    new_user = User(
        username=user.username,
        numero=str(user.numero),
        hashed_password=hash_password(user.password),
        role=user.role,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    return new_user


@router.put("/users/{user_id}", response_model=UserResponse)
def update_user(
    user_id: int,
    payload: UserUpdate,
    caller: User = Depends(require_admin_or_expert),
    db: Session = Depends(get_db),
):
    db_user = db.query(User).filter(User.id == user_id).first()
    if not db_user:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")

    if payload.numero is not None:
        numero_str = str(payload.numero)
        doublon = (
            db.query(User)
            .filter(User.numero == numero_str, User.id != user_id)
            .first()
        )
        if doublon:
            raise HTTPException(status_code=400, detail="Numéro déjà utilisé")
        db_user.numero = numero_str

    if payload.username is not None:
        db_user.username = payload.username

    if payload.role is not None:
        db_user.role = payload.role

    # Mot de passe : uniquement si explicitement fourni et non vide, sinon
    # on laisse l'existant tel quel (évite d'écraser avec une chaîne vide).
    if payload.password:
        db_user.hashed_password = hash_password(payload.password)

    db.commit()
    db.refresh(db_user)

    return db_user


@router.delete("/users")
def delete_users(
    payload: DeleteUsersPayload,
    caller: User = Depends(require_admin_or_expert),
    db: Session = Depends(get_db),
):
    ids = set(payload.ids)
    if not ids:
        return {"deleted": 0}
    if caller.id in ids:
        raise HTTPException(
            status_code=400, detail="Impossible de supprimer votre propre compte"
        )

    deleted = (
        db.query(User)
        .filter(User.id.in_(ids))
        .delete(synchronize_session=False)
    )
    db.commit()

    return {"deleted": deleted}


@router.delete("/users/{user_id}")
def delete_user(
    user_id: int,
    caller: User = Depends(require_admin_or_expert),
    db: Session = Depends(get_db),
):
    if user_id == caller.id:
        raise HTTPException(
            status_code=400, detail="Impossible de supprimer votre propre compte"
        )

    db_user = db.query(User).filter(User.id == user_id).first()
    if not db_user:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")

    db.delete(db_user)
    db.commit()

    return {"message": "Utilisateur supprimé"}