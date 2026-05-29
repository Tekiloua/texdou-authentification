from sqlalchemy import Column, ForeignKey, Integer, String
from src.backend.db.database import Base

class Liens_Utile(Base):
    __tablename__ = "liens_utiles"
    id = Column(Integer, primary_key=True, index=True)
    texte_id=Column(Integer, ForeignKey("textes.id"))
    titre=Column(String, index=True)
    url=Column(String, index=True)
    entite=Column(String, index=True)