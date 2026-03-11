from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.core.database import Base, engine
from app.routers import auth, dashboard, canchas, reservas, clientes, config, webhook

# Crear tablas (en producción usar Alembic)
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="MatchBot API",
    description="Backend de reservas deportivas vía WhatsApp",
    version="1.0.0",
    docs_url="/docs" if settings.APP_ENV != "production" else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_URL, "http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router,      prefix="/api")
app.include_router(dashboard.router, prefix="/api")
app.include_router(canchas.router,   prefix="/api")
app.include_router(reservas.router,  prefix="/api")
app.include_router(clientes.router,  prefix="/api")
app.include_router(config.router,    prefix="/api")
app.include_router(webhook.router)   # sin /api — Twilio llama directo

@app.get("/health")
def health():
    return {"status": "ok", "app": "MatchBot API"}
