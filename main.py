"""
main.py — Vstupní bod aplikace Immersive Stories API.

Registruje routery: campaign, action, narrative, visualize.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from dotenv import load_dotenv

from api.routes.campaign  import router as campaign_router
from api.routes.action    import router as action_router
from api.routes.narrative import router as narrative_router
from api.routes.visualize import router as visualize_router
from api.routes.credits   import router as credits_router
from db.pg import close_pool

load_dotenv()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    await close_pool()  # korektní uzavření asyncpg poolu při shutdownu


app = FastAPI(
    title="Immersive Stories API",
    description="Backend engine pro interaktivní narativní hry.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(campaign_router)
app.include_router(action_router)
app.include_router(narrative_router)
app.include_router(visualize_router)
app.include_router(credits_router)


@app.get("/health", tags=["system"])
async def health_check():
    return {"status": "ok"}
