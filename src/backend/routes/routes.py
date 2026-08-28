from fastapi import APIRouter, HTTPException, Depends, Cookie, Response, File, UploadFile
from fastapi.security import HTTPBearer
from src.backend.models import (
    Categorie, Historique, Liens_Utile,
    Statut, Texte, Texte_Document, Texte_Reference, Texte_Theme, Theme,
)
from src.backend.db.database import db_dependency, get_db, Session
from pathlib import Path
from sqlalchemy import func


router = APIRouter()
security = HTTPBearer()

BASE_DIR   = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


# ─── UPLOAD ────────────────────────────────────────────────────────────────────
@router.post("/upload-file")
async def upload_file(file: UploadFile = File(...)):
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    file_path = UPLOAD_DIR / file.filename
    file_path.write_bytes(await file.read())
    return {"filename": file.filename, "path": str(file_path)}


# ─── DONNÉES ───────────────────────────────────────────────────────────────────

@router.get("/liens-utiles")
def get_all_liens_utiles(db: db_dependency):
    return db.query(Liens_Utile).all()


# ─── ADMIN ─────────────────────────────────────────────────────────────────────

# @router.get("/users")
# def get_all_users(credentials=Depends(security), db: Session = Depends(get_db)):
#     """Retourne la liste de tous les utilisateurs (réservé aux admins)."""
#     token = credentials.credentials
#     payload = verify_token(token)
#     if not payload:
#         raise HTTPException(status_code=401, detail="Token invalide ou expiré")

#     caller = db.query(User).filter(User.numero == payload["sub"]).first()
#     if not caller or caller.role.value != UserRole.admin.value:
#         raise HTTPException(status_code=403, detail="Accès réservé aux administrateurs")

#     users = db.query(User).all()
#     return [
#         {
#             "id":       u.id,
#             "numero":   u.numero,
#             "username": u.username,
#             "role":     u.role.value,
#         }
#         for u in users
#     ]


@router.get("/latest-documents")
def get_latest_documents(db: db_dependency):
    """Retourne les 10 derniers textes (par id décroissant)."""
    textes = (
        db.query(Texte)
        .order_by(Texte.id.desc())
        .limit(10)
        .all()
    )
    return [
        {
            "id":                 t.id,
            "titre":              t.titre,
            "numero":             t.numero,
            "date_mise_en_vigueur": str(t.date_mise_en_vigueur) if t.date_mise_en_vigueur else None,
            "statut_id":          t.statut_id,
            "categorie_id":       t.categorie_id,
        }
        for t in textes
    ]


@router.get("/stats")
def get_stats(db: db_dependency):
    """Retourne le nombre total de textes et le nombre de textes en vigueur (statut_id == 1)."""
    total   = db.query(func.count(Texte.id)).scalar()
    # Statut « en vigueur » : adapte statut_id=1 si ta table Statut utilise un autre id
    en_vigueur = db.query(func.count(Texte.id)).filter(Texte.statut_id == 1).scalar()
    return {
        "total_textes":    total,
        "textes_en_vigueur": en_vigueur,
    }