from sqlalchemy import Column, ForeignKey, Integer, String, Date
from src.backend.db.database import Base

class Historique(Base):
    __tablename__ = "historiques"
    id = Column(Integer, primary_key=True, index=True)
    texte_id=Column(Integer, ForeignKey("textes.id"))
    date=Column(Date, index=True)
    statut=Column(String, index=True)