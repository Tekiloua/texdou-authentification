from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from src.backend.db.database import engine, Base
from src.backend.routes.routes import router
from src.backend.routes.categorie_route import router as categorie_router
from src.backend.routes.statut_route import router as statut_router
from src.backend.routes.texte_route import router as texte_router
from src.backend.routes.theme_route import router as theme_router
from src.backend.routes.user_route import router as user_router
from src.backend.routes.texte_document_route import router as texte_document_router
from src.backend.routes.conversation_route import router as conversation_router
from src.backend.routes.message_route import router as message_router
from src.backend.routes.texte_reference_route import router as texte_reference_router
from src.backend.routes.consommation_route import router as consommation_router
from src.backend.routes.document_route import router as document_router
from src.backend.routes.historique_route import router as historique_router
from src.backend.routes.rag_route import router as rag_router
from src.backend.routes.qualites_documents_route import router as qualites_documents_router
from src.backend.routes.chroma_route import router as chroma_router
 
app = FastAPI()

# Création des tables au démarrage
Base.metadata.create_all(bind=engine)

origins = [
    "http://localhost",
    "http://localhost:3000",   # React CRA
    "http://127.0.0.1:3000",
    "http://localhost:5173",   # Vite (dev)
    "http://127.0.0.1:5173",
    "http://192.168.123.15:5173",
    "http://192.168.123.15:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,   # indispensable pour que le cookie refresh_token passe
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(user_router, tags=["auth"])
app.include_router(router)
app.include_router(categorie_router)  
app.include_router(statut_router)  
app.include_router(texte_router)  
app.include_router(theme_router)  
app.include_router(texte_document_router)  
app.include_router(conversation_router)  
app.include_router(message_router)  
app.include_router(texte_reference_router)  
app.include_router(consommation_router)  
app.include_router(document_router)  
app.include_router(historique_router)  
app.include_router(qualites_documents_router) 
app.include_router(chroma_router) 
app.include_router(rag_router) 