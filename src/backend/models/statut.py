from sqlalchemy import Column, Integer, String, ForeignKey
from src.backend.db.database import Base

class Statut(Base):
    __tablename__='statuts'
    id = Column(Integer, primary_key=True, index=True)
    nom = Column(String, index=True)
    description = Column(String, index=True)
    slug = Column(String, index=True)
    parent_id = Column(Integer, ForeignKey("statuts.id"))
    couleur = Column(String, index=True)