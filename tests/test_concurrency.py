"""
The centerpiece test of this project.

Fires 100 concurrent transfer requests from the SAME source account and
checks that the final balance is exactly right -- no lost updates, no
double-applies -- under both locking strategies. Then runs the same load
against the deliberately unprotected (naive) code path to *prove* the bug
these strategies fix actually exists, rather than just asserting it away.
"""

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from app.db import SessionLocal
from app.models import Account, Transaction, TransactionStatus
from app.services import transfer_service
from tests.conftest import make_user, deposit

N_REQUESTS = 100
AMOUNT = Decimal("1.00")
INITIAL_BALANCE = Decimal("10000.00")


def _fire_concurrent_transfers(client, from_account, to_account, strategy):
    def do_one(i):
        key = str(uuid.uuid4())
        return client.post(
            "/transfer",
            params={"strategy": strategy},
            json={
                "from_account": from_account,
                "to_account": to_account,
                "amount": str(AMOUNT),
                "idempotency_key": key,
            },
        )

    start = time.time()
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(do_one, range(N_REQUESTS)))
    elapsed = time.time() - start

    statuses = [r.status_code for r in results]
    throughput = N_REQUESTS / elapsed if elapsed > 0 else float("inf")
    return statuses, elapsed, throughput


@pytest.mark.parametrize("strategy", ["pessimistic", "optimistic"])
def test_concurrent_transfers_preserve_balance(client, strategy):
    sender = make_user(client, "Sender", f"sender-{strategy}@test.com")
    receiver = make_user(client, "Receiver", f"receiver-{strategy}@test.com")
    deposit(client, sender["account_id"], str(INITIAL_BALANCE))

    statuses, elapsed, throughput = _fire_concurrent_transfers(
        client, sender["account_id"], receiver["account_id"], strategy
    )

    assert all(s == 200 for s in statuses), f"Some requests failed: {statuses}"

    final = client.get(f"/accounts/{sender['account_id']}/balance").json()["balance"]
    expected = INITIAL_BALANCE - (N_REQUESTS * AMOUNT)

    print(
        f"\n[{strategy}] {N_REQUESTS} requests in {elapsed:.2f}s "
        f"({throughput:.1f} req/s) -- final balance: {final}, expected: {expected}"
    )

    assert Decimal(str(final)) == expected, (
        f"Balance corrupted under {strategy} locking! "
        f"Expected {expected}, got {final}"
    )


def test_naive_transfer_loses_updates_under_concurrency():
    """
    Control case: same load, but using apply_transfer_naive (no locking).
    This is EXPECTED to corrupt the balance -- that's the point. It proves
    the race condition is real, not just a theoretical concern the other
    two strategies solve.
    """
    db = SessionLocal()
    from app.models import User

    sender_user = User(name="NaiveSender", email="naive-sender@test.com")
    receiver_user = User(name="NaiveReceiver", email="naive-receiver@test.com")
    db.add_all([sender_user, receiver_user])
    db.commit()

    sender_acc = Account(user_id=sender_user.id, balance=INITIAL_BALANCE, version=0)
    receiver_acc = Account(user_id=receiver_user.id, balance=0, version=0)
    db.add_all([sender_acc, receiver_acc])
    db.commit()
    db.refresh(sender_acc)
    db.refresh(receiver_acc)
    sender_id, receiver_id = sender_acc.id, receiver_acc.id
    db.close()

    def do_one(i):
        thread_db = SessionLocal()
        txn = Transaction(
            from_account=sender_id, to_account=receiver_id, amount=AMOUNT,
            idempotency_key=str(uuid.uuid4()), status=TransactionStatus.pending,
        )
        thread_db.add(txn)
        thread_db.commit()
        thread_db.refresh(txn)
        try:
            transfer_service.apply_transfer_naive(thread_db, txn, sender_id, receiver_id, AMOUNT)
        finally:
            thread_db.close()

    start = time.time()
    with ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(do_one, range(N_REQUESTS)))
    elapsed = time.time() - start

    check_db = SessionLocal()
    final = check_db.query(Account).filter_by(id=sender_id).one().balance
    check_db.close()

    expected = INITIAL_BALANCE - (N_REQUESTS * AMOUNT)
    throughput = N_REQUESTS / elapsed if elapsed > 0 else float("inf")

    print(
        f"\n[naive/no-locking] {N_REQUESTS} requests in {elapsed:.2f}s "
        f"({throughput:.1f} req/s) -- final balance: {final}, expected: {expected}"
    )

    # This assertion documents the bug: under real concurrency, naive
    # read-then-write reliably loses updates, so final != expected.
    # (If this ever passes, it means no race was hit this run -- rerun,
    # or increase N_REQUESTS / reduce max_workers gap to widen the window.)
    assert final != expected, (
        "Expected the naive implementation to lose updates under concurrency, "
        "but the balance was correct this run -- the race window may need widening."
    )
