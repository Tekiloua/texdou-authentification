from sqlalchemy import SmallInteger, Column, ForeignKey, Integer, String, Date, BigInteger
from src.backend.db.database import Base

class Texte(Base):
    __tablename__="textes"
    id = Column(Integer, primary_key=True, index=True)
    # wp_id=Column(Integer, index=True)
    titre=Column(String, index=True)
    numero=Column(String, index=True)
    date_mise_en_vigueur=Column(Date, index=True)
    signataire_nom=Column(String, index=True)
    signataire_titre=Column(String, index=True)
    resume=Column(String)
    mots_cles=Column(String, index=True)
    contenu_html=Column(String)
    categorie_id=Column(Integer, ForeignKey("categories.id"))
    statut_id=Column(Integer, ForeignKey("statuts.id"))
    # note_presentation_id=Column(Integer)
    publish=Column(SmallInteger)
