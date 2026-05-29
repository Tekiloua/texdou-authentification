from sqlalchemy import Column, ForeignKey, Integer
from src.backend.db.database import Base

class Texte_Document(Base):
    __tablename__ = "textes_documents"
    texte_id=Column(Integer, ForeignKey("textes.id"),primary_key=True)
    document_id=Column(Integer, ForeignKey("documents.id"),primary_key=True)