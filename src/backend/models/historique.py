from sqlalchemy import Column, ForeignKey, Integer, String, DateTime
from sqlalchemy.sql import func
from src.backend.db.database import Base


class Historique(Base):
    """Journal des changements de statut d'un texte.

    Chaque ligne représente UN changement de statut détecté sur /textes/{id}
    (PUT). On dénormalise `texte_titre` (snapshot du titre au moment du
    changement) pour que l'historique reste lisible même si le texte est
    renommé ou supprimé par la suite.
    """

    __tablename__ = "historiques"

    id = Column(Integer, primary_key=True, index=True)

    # ondelete="SET NULL" : si le texte est supprimé, on garde la ligne
    # d'historique (utile pour l'audit) mais on détache la FK.
    texte_id = Column(
        Integer, ForeignKey("textes.id", ondelete="SET NULL"), nullable=True, index=True
    )
    texte_titre = Column(String, nullable=True)

    ancien_statut = Column(String, nullable=True)
    nouveau_statut = Column(String, index=True, nullable=True)

    # FK vers users.numero (String, unique) : identifie l'utilisateur ayant
    # fait le changement. Nullable : peut être None si l'appel n'était pas
    # authentifié. ondelete="SET NULL" : si l'utilisateur est supprimé, la
    # ligne d'historique est conservée (audit) mais détachée.
    numero_user = Column(
        String, ForeignKey("users.numero", ondelete="SET NULL"), nullable=True, index=True
    )

    date = Column(DateTime(timezone=True), server_default=func.now(), index=True)