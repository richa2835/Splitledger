import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://splitledger:splitledger@localhost:5432/splitledger"
)

# pool_pre_ping avoids handing out dead connections after e.g. a DB restart.
# pool_size/max_overflow sized up because our concurrency tests deliberately
# hold many connections at once (each thread holding a row lock needs its
# own connection) -- the default pool_size=5 would deadlock threads waiting
# for a free connection while other threads hold locks.
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=30, max_overflow=30)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI dependency: one DB session per request, always closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
