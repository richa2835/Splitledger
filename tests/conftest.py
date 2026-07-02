import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.db import Base, engine
from app.services.rate_limit import redis_client


@pytest.fixture(scope="function")
def client():
    # Fresh schema for every test function -- avoids leftover balances from
    # previous tests giving false confidence.
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    redis_client.flushdb()  # reset rate-limit buckets too
    with TestClient(app) as c:
        yield c


def make_user(client, name, email):
    r = client.post("/users", json={"name": name, "email": email})
    assert r.status_code == 201, r.text
    return r.json()


def deposit(client, account_id, amount):
    r = client.post(f"/accounts/{account_id}/deposit", json={"amount": amount})
    assert r.status_code == 200, r.text
    return r.json()
