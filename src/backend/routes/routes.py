from src.backend.auth.auth import (
    create_access_token,
    hash_password,
    verify_password,
    verify_token,
    create_refresh_token,
)
from src.backend.models.user import User, UserRole
from src.backend.schemas.schemas import UserLogin, UserCreate, UserResponse
from fastapi import APIRouter, HTTPException, Depends, Cookie, Response, File, UploadFile
from fastapi.security import HTTPBearer
from src.backend.models import (
    Categorie, Document, Historique, Liens_Utile,
    Statut, Texte, Texte_Document, Texte_Reference, Texte_Theme, Theme,
)
from src.backend.db.database import db_dependency, get_db, Session
from pathlib import Path

router = APIRouter()
security = HTTPBearer()

BASE_DIR   = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


# ─── AUTH ──────────────────────────────────────────────────────────────────────

@router.post("/login")
def login(user: UserLogin, response: Response, db: Session = Depends(get_db)):
    # numero est un int dans le schéma Pydantic mais un String en BDD
    # → toujours comparer avec str()
    db_user = db.query(User).filter(User.numero == str(user.numero)).first()

    if not db_user or not verify_password(user.password, db_user.hashed_password):
        raise HTTPException(status_code=401, detail="Identifiants invalides")

    access_token = create_access_token({
        "sub":  db_user.numero,          # str
        "role": db_user.role.value,
    })
    refresh_token = create_refresh_token({"sub": db_user.numero})

    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=False,     # True en production (HTTPS)
        samesite="lax",
    )

    return {
        "access_token": access_token,
        "role":         db_user.role.value,
    }


@router.post("/refresh")
def refresh(
    refresh_token: str = Cookie(None),
    db: Session = Depends(get_db),
):
    if not refresh_token:
        raise HTTPException(status_code=401, detail="Refresh token manquant")

    payload = verify_token(refresh_token)
    if not payload:
        raise HTTPException(status_code=401, detail="Refresh token invalide")

    db_user = db.query(User).filter(User.numero == payload["sub"]).first()
    if not db_user:
        raise HTTPException(status_code=401, detail="Utilisateur introuvable")

    new_access_token = create_access_token({
        "sub":  db_user.numero,
        "role": db_user.role.value,
    })

    return {
        "access_token": new_access_token,
        "role":         db_user.role.value,
    }


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(key="refresh_token")
    return {"message": "Déconnecté"}


@router.post("/register", response_model=UserResponse)
def register(user: UserCreate, db: Session = Depends(get_db)):
    # 1) Valider le matricule EN PREMIER (Pydantic le fait déjà via Field,
    #    mais on garde la vérification explicite pour le message d'erreur)
    if user.numero < 100_000 or user.numero >= 1_000_000:
        raise HTTPException(status_code=400, detail="Matricule invalide (100000–999999)")

    # 2) Vérifier l'unicité ensuite
    if db.query(User).filter(User.numero == str(user.numero)).first():
        raise HTTPException(status_code=400, detail="Numéro déjà utilisé")

    # Attribution du rôle selon la plage de matricule
    if user.numero in (100_000, 100_001):
        role = UserRole.admin
    elif 100_002 < user.numero < 100_100:
        role = UserRole.expert
    else:
        role = UserRole.normal

    new_user = User(
        username=user.username,
        numero=str(user.numero),          # stocké en String
        hashed_password=hash_password(user.password),
        role=role,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    return new_user   # UserResponse sérialisera correctement grâce à from_attributes=True


@router.get("/me")
def me(credentials=Depends(security), db: Session = Depends(get_db)):
    token = credentials.credentials
    payload = verify_token(token)

    if not payload:
        raise HTTPException(status_code=401, detail="Token invalide ou expiré")

    db_user = db.query(User).filter(User.numero == payload["sub"]).first()
    if not db_user:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")

    return {
        "numero":   db_user.numero,
        "username": db_user.username,
        "role":     db_user.role.value,
    }


# ─── UPLOAD ────────────────────────────────────────────────────────────────────

@router.post("/upload-file")
async def upload_file(file: UploadFile = File(...)):
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    file_path = UPLOAD_DIR / file.filename
    file_path.write_bytes(await file.read())
    return {"filename": file.filename, "path": str(file_path)}


# ─── DONNÉES ───────────────────────────────────────────────────────────────────

@router.get("/categories")
def get_all_categories(db: db_dependency):
    return db.query(Categorie).all()

@router.get("/documents")
def get_all_documents(db: db_dependency):
    return db.query(Document).all()

@router.get("/documents/{id}")
def get_document_by_id(id: int, db: db_dependency):
    result = db.query(Document).filter(Document.id == id).first()
    if not result:
        raise HTTPException(status_code=404, detail="Document introuvable")
    return result

@router.get("/historiques")
def get_all_historiques(db: db_dependency):
    return db.query(Historique).all()

@router.get("/liens-utiles")
def get_all_liens_utiles(db: db_dependency):
    return db.query(Liens_Utile).all()

@router.get("/statuts")
def get_all_statuts(db: db_dependency):
    return db.query(Statut).all()

@router.get("/textes")
def get_all_textes(db: db_dependency):
    return db.query(Texte).order_by(Texte.id.asc()).all()

@router.get("/textes/{id}")
def get_texte_by_id(id: int, db: db_dependency):
    result = db.query(Texte).filter(Texte.id == id).first()
    if not result:
        raise HTTPException(status_code=404, detail="Texte introuvable")
    return result

@router.get("/textes-documents")
def get_all_textes_documents(db: db_dependency):
    return db.query(Texte_Document).all()

@router.get("/textes-references")
def get_all_textes_references(db: db_dependency):
    return db.query(Texte_Reference).all()

@router.get("/textes-themes")
def get_all_textes_themes(db: db_dependency):
    return db.query(Texte_Theme).all()

@router.get("/themes")
def get_all_themes(db: db_dependency):
    return db.query(Theme).all()