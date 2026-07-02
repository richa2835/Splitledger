from fastapi import FastAPI

from app.db import Base, engine
from app import models  # noqa: F401 -- ensures models are registered before create_all
from app.routes import wallet, groups

app = FastAPI(title="SplitLedger", version="0.1.0")

app.include_router(wallet.router, tags=["wallet"])
app.include_router(groups.router, tags=["groups"])


@app.on_event("startup")
def on_startup():
    # For local/dev convenience. In a real deployment you'd use Alembic
    # migrations instead of create_all.
    Base.metadata.create_all(bind=engine)


@app.get("/health")
def health():
    return {"status": "ok"}
