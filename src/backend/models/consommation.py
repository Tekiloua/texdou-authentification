from sqlalchemy import Column, Integer, String, DateTime
from datetime import datetime
from src.backend.db.database import Base


class Consommation(Base):
    __tablename__ = "consommations"

    id = Column(Integer, primary_key=True, index=True)
    input = Column(Integer, nullable=False, default=0)   # tokens entrants (prompt)
    output = Column(Integer, nullable=False, default=0)  # tokens sortants (completion)

    # Pas de ForeignKey volontairement : simple copie historique du numero
    # au moment de l'appel. La ligne survit même si le user est supprimé.
    numero = Column(String, nullable=False, index=True)

    created_at = Column(DateTime, default=datetime.utcnow)