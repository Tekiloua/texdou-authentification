from sqlalchemy import Column, Integer, String
from src.backend.db.database import Base

class Statut(Base):
    __tablename__='statuts'
    id = Column(Integer, primary_key=True, index=True)
    nom = Column(String, index=True)