from sqlalchemy import Column, ForeignKey, Integer, String
from src.backend.db.database import Base

class Theme(Base):
    __tablename__="themes"
    id = Column(Integer, primary_key=True, index=True)
    nom = Column(String, index=True)
    parent_id = Column(Integer, ForeignKey("themes.id"))
    description = Column(String, index=True)
    slug = Column(String, index=True)
    couleur = Column(String, index=True)