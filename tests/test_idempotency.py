"""
Proves the idempotency-key mechanism actually prevents double-processing --
the exact bug that happens when a client retries after a timeout without
knowing whether the first attempt actually went through.
"""

import uuid
from decimal import Decimal

from tests.conftest import make_user, deposit


def test_duplicate_request_only_applies_once(client):
    sender = make_user(client, "Alice", "alice-idem@test.com")
    receiver = make_user(client, "Bob", "bob-idem@test.com")
    deposit(client, sender["account_id"], "1000.00")

    key = str(uuid.uuid4())
    body = {
        "from_account": sender["account_id"],
        "to_account": receiver["account_id"],
        "amount": "250.00",
        "idempotency_key": key,
    }

    # Simulate a client that sent the request, didn't get a response in time
    # (e.g. network blip), and retries with the SAME idempotency key.
    r1 = client.post("/transfer", json=body)
    r2 = client.post("/transfer", json=body)

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text

    # Both responses should describe the SAME transaction, not two different ones.
    assert r1.json()["transaction_id"] == r2.json()["transaction_id"]

    sender_balance = client.get(f"/accounts/{sender['account_id']}/balance").json()["balance"]
    receiver_balance = client.get(f"/accounts/{receiver['account_id']}/balance").json()["balance"]

    # If double-processing happened, sender would be at 500.00 instead of 750.00.
    assert Decimal(str(sender_balance)) == Decimal("750.00")
    assert Decimal(str(receiver_balance)) == Decimal("250.00")


def test_different_idempotency_keys_both_apply(client):
    """Sanity check the fixture above isn't just accidentally deduping everything."""
    sender = make_user(client, "Carol", "carol-idem@test.com")
    receiver = make_user(client, "Dave", "dave-idem@test.com")
    deposit(client, sender["account_id"], "1000.00")

    for _ in range(2):
        client.post("/transfer", json={
            "from_account": sender["account_id"],
            "to_account": receiver["account_id"],
            "amount": "100.00",
            "idempotency_key": str(uuid.uuid4()),  # different key each time
        })

    sender_balance = client.get(f"/accounts/{sender['account_id']}/balance").json()["balance"]
    assert Decimal(str(sender_balance)) == Decimal("800.00")
