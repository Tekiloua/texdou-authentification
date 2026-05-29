from sqlalchemy import Column, ForeignKey, Integer
from src.backend.db.database import Base

class Texte_Theme(Base):
    __tablename__ = "textes_themes"
    texte_id=Column(Integer, ForeignKey("textes.id"), primary_key=True)
    theme_id=Column(Integer, ForeignKey("themes.id"), primary_key=True)
