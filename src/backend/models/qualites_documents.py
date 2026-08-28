from sqlalchemy import Column, Float, ForeignKey, Integer
from src.backend.db.database import Base


class QualiteDocument(Base):
    __tablename__ = "qualites_documents"

    id = Column(Integer, primary_key=True, index=True)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=False, index=True)
    page = Column(Integer, nullable=False)          # numéro de page (1, 2, … n)
    blur = Column(Float, nullable=True)             # variance du Laplacien (netteté)
    skew = Column(Float, nullable=True)             # inclinaison détectée (degrés)
    noise_score = Column(Float, nullable=True)      # écart-type du bruit résiduel
    black_pixel_ratio = Column(Float, nullable=True)  # ratio pixels sombres / total
    entropy = Column(Float, nullable=True)          # entropie de Shannon
    brightness = Column(Float, nullable=True)       # luminosité moyenne (0-100)
    score = Column(Float, nullable=True)            # score global de qualité