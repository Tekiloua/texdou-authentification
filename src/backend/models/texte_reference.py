from sqlalchemy import Column, ForeignKey, Integer, String, Date
from src.backend.db.database import Base

class Texte_Reference(Base):
    __tablename__ = "textes_reference"
    id = Column(Integer, primary_key=True, index=True)
    texte_id=Column(Integer, ForeignKey("textes.id"))
    titre=Column(String , index=True)
    numero=Column(String, index=True)
    date_mise_en_vigueur=Column(Date, index=True)
    categorie=Column(String, index=True)
    statut=Column(String, index=True)
    lien_url=Column(String, index=True)
    texte_lie_id=Column(Integer)