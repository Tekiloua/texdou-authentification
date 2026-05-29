from sqlalchemy import Column, Integer, String, Date, BigInteger
from src.backend.db.database import Base

class Document(Base):
    __tablename__ = 'documents'
    id= Column(Integer, primary_key=True, index=True)
    nom = Column(String, index=True)
    chemin_fichier = Column(String, index=True)
    nouveau_chemin = Column(String, index=True)
    mime_type = Column(String, index=True)
    taille_octets = Column(BigInteger)
    date_upload = Column(Date, index=True)