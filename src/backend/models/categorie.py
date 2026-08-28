from sqlalchemy import Column, Integer, String, ForeignKey
from src.backend.db.database import Base

class Categorie(Base):
    __tablename__="categories"
    id = Column(Integer, primary_key=True, index=True)
    nom = Column(String, index=True)
    description = Column(String, index=True)
    slug = Column(String, index=True)
    parent_id = Column(Integer, ForeignKey("categories.id"))
    couleur = Column(String, index=True)