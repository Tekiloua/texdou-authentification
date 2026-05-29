from sqlalchemy import Column, Integer, String
from src.backend.db.database import Base

class Categorie(Base):
    __tablename__="categories"
    id = Column(Integer, primary_key=True, index=True)
    nom = Column(String, index=True)