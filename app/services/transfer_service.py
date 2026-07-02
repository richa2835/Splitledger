"""
This module is the heart of the concurrency story.

Three ways to apply a transfer are implemented on purpose:
  - apply_transfer_naive        : NO protection. Used only to demonstrate the bug.
  - apply_transfer_pessimistic  : SELECT ... FOR UPDATE (lock, then update).
  - apply_transfer_optimistic   : version-column compare-and-swap + retry.

All three share the same idempotency-claim step, because "don't double-process
a retried request" is a separate concern from "don't corrupt balances under
concurrency" -- even though both show up in the same endpoint.
"""

import random
import time
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Account, Transaction, LedgerEntry, TransactionStatus, EntryType


class InsufficientFunds(Exception):
    pass


def claim_idempotency_key(
    db: Session, key: str, from_account: int, to_account: int, amount: Decimal
) -> tuple[Transaction, bool]:
    """
    Try to 'claim' an idempotency key by inserting a pending Transaction row.

    Returns (transaction, is_new).
      is_new=True  -> caller should process the transfer now.
      is_new=False -> someone already claimed this key; caller should NOT
                       reprocess. Check .status on the returned transaction.

    The DB's unique constraint on idempotency_key is what makes this safe
    even if two requests with the same key arrive at literally the same
    instant -- only one INSERT can win, the other gets IntegrityError.
    """
    existing = db.query(Transaction).filter_by(idempotency_key=key).first()
    if existing:
        return existing, False

    txn = Transaction(
        from_account=from_account,
        to_account=to_account,
        amount=amount,
        idempotency_key=key,
        status=TransactionStatus.pending,
    )
    db.add(txn)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.query(Transaction).filter_by(idempotency_key=key).one()
        return existing, False

    db.refresh(txn)
    return txn, True


def _write_ledger_and_finish(db: Session, txn: Transaction, from_acc: Account, to_acc: Account, amount: Decimal):
    db.add(LedgerEntry(transaction_id=txn.id, account_id=from_acc.id, entry_type=EntryType.debit, amount=amount))
    db.add(LedgerEntry(transaction_id=txn.id, account_id=to_acc.id, entry_type=EntryType.credit, amount=amount))
    txn.status = TransactionStatus.completed
    db.commit()


def apply_transfer_pessimistic(db: Session, txn: Transaction, from_id: int, to_id: int, amount: Decimal):
    """
    Lock both rows with SELECT ... FOR UPDATE before touching them.
    Lock ordering (always the lower account id first) is what prevents a
    deadlock when two transfers in opposite directions run at the same time.
    """
    first_id, second_id = sorted([from_id, to_id])
    acc_first = db.query(Account).filter_by(id=first_id).with_for_update().one()
    acc_second = db.query(Account).filter_by(id=second_id).with_for_update().one()

    from_acc = acc_first if acc_first.id == from_id else acc_second
    to_acc = acc_second if acc_second.id == to_id else acc_first

    if from_acc.balance < amount:
        txn.status = TransactionStatus.failed
        db.commit()
        raise InsufficientFunds()

    from_acc.balance -= amount
    from_acc.version += 1
    to_acc.balance += amount
    to_acc.version += 1

    _write_ledger_and_finish(db, txn, from_acc, to_acc, amount)
    return from_acc, to_acc


def apply_transfer_optimistic(
    db: Session, txn: Transaction, from_id: int, to_id: int, amount: Decimal, max_retries: int = 50
):
    """
    Read balance + version (no lock). Attempt an UPDATE ... WHERE version = X.
    If 0 rows were affected, someone else updated the row first -- retry with
    exponential backoff + jitter. This trades "always correct, might queue"
    (pessimistic) for "usually fast, occasionally retries" under contention.

    Note: under HEAVY contention on the same row (e.g. 20 threads all hitting
    one account), optimistic locking's retry rate goes up sharply -- this is
    a real, expected tradeoff, not a bug. It's exactly why the spec asks you
    to measure throughput for both strategies rather than assume one wins.
    """
    for attempt in range(max_retries):
        from_acc = db.query(Account).filter_by(id=from_id).one()
        to_acc = db.query(Account).filter_by(id=to_id).one()

        if from_acc.balance < amount:
            txn.status = TransactionStatus.failed
            db.commit()
            raise InsufficientFunds()

        from_version, to_version = from_acc.version, to_acc.version
        new_from_balance = from_acc.balance - amount
        new_to_balance = to_acc.balance + amount

        result_from = db.execute(
            update(Account)
            .where(Account.id == from_id, Account.version == from_version)
            .values(balance=new_from_balance, version=from_version + 1)
        )
        result_to = db.execute(
            update(Account)
            .where(Account.id == to_id, Account.version == to_version)
            .values(balance=new_to_balance, version=to_version + 1)
        )

        if result_from.rowcount == 1 and result_to.rowcount == 1:
            db.add(LedgerEntry(transaction_id=txn.id, account_id=from_id, entry_type=EntryType.debit, amount=amount))
            db.add(LedgerEntry(transaction_id=txn.id, account_id=to_id, entry_type=EntryType.credit, amount=amount))
            txn.status = TransactionStatus.completed
            db.commit()
            db.refresh(from_acc)
            db.refresh(to_acc)
            return from_acc, to_acc

        # Someone else won the race on at least one row -- back off and retry.
        # Exponential backoff (capped) + random jitter avoids every losing
        # thread retrying in lockstep and re-colliding with each other.
        db.rollback()
        backoff = min(0.2, 0.005 * (2 ** attempt))
        time.sleep(backoff + random.uniform(0, backoff))

    txn.status = TransactionStatus.failed
    db.commit()
    raise RuntimeError(f"Optimistic transfer failed after {max_retries} retries (too much contention)")


def apply_transfer_naive(db: Session, txn: Transaction, from_id: int, to_id: int, amount: Decimal):
    """
    NO locking, NO version check. This is the deliberately-broken version:
    read balances, sleep briefly (simulating real work / network time between
    read and write, which is exactly when a race window opens), then write.

    Used ONLY by the control test to prove the bug exists. Never call this
    from a real endpoint.
    """
    from_acc = db.query(Account).filter_by(id=from_id).one()
    to_acc = db.query(Account).filter_by(id=to_id).one()

    if from_acc.balance < amount:
        txn.status = TransactionStatus.failed
        db.commit()
        raise InsufficientFunds()

    time.sleep(0.01)  # widen the race window so the bug shows up reliably in tests

    from_acc.balance -= amount
    to_acc.balance += amount

    _write_ledger_and_finish(db, txn, from_acc, to_acc, amount)
    return from_acc, to_acc
