from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from src.backend.db.database import engine, Base
from src.backend.routes.routes import router

app = FastAPI()

# Création des tables au démarrage
Base.metadata.create_all(bind=engine)

origins = [
    "http://localhost",
    "http://localhost:3000",   # React CRA
    "http://127.0.0.1:3000",
    "http://localhost:5173",   # Vite (dev)
    "http://127.0.0.1:5173",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,   # indispensable pour que le cookie refresh_token passe
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)