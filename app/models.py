import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Column, Integer, String, Numeric, DateTime, ForeignKey,
    Enum, Boolean, UniqueConstraint, Index, func
)
from sqlalchemy.orm import relationship

from app.db import Base


class TransactionStatus(str, enum.Enum):
    pending = "pending"       # in-flight; used to detect concurrent retries
    completed = "completed"
    failed = "failed"


class EntryType(str, enum.Enum):
    debit = "debit"
    credit = "credit"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    account = relationship("Account", back_populates="user", uselist=False)


class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    # CACHED value, derived from ledger_entries. Never treat this as the
    # source of truth -- see services/transfer_service.py for why.
    balance = Column(Numeric(14, 2), nullable=False, default=0)

    # Used for optimistic locking (see services/transfer_service.py).
    # Every update to `balance` must also increment this.
    version = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="account")


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True)
    from_account = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    to_account = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    amount = Column(Numeric(14, 2), nullable=False)
    status = Column(Enum(TransactionStatus), nullable=False, default=TransactionStatus.pending)

    # Client-generated key for idempotency. Unique so a duplicate request
    # collides at the DB level even under a race (belt-and-suspenders on
    # top of the application-level check).
    idempotency_key = Column(String, unique=True, nullable=False, index=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    ledger_entries = relationship("LedgerEntry", back_populates="transaction")


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"

    id = Column(Integer, primary_key=True)
    transaction_id = Column(Integer, ForeignKey("transactions.id"), nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    entry_type = Column(Enum(EntryType), nullable=False)
    amount = Column(Numeric(14, 2), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    transaction = relationship("Transaction", back_populates="ledger_entries")

    __table_args__ = (
        Index("ix_ledger_account_created", "account_id", "created_at"),
    )


class Group(Base):
    __tablename__ = "groups"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class GroupMember(Base):
    __tablename__ = "group_members"

    group_id = Column(Integer, ForeignKey("groups.id"), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)


class Expense(Base):
    __tablename__ = "expenses"

    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=False)
    paid_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    amount = Column(Numeric(14, 2), nullable=False)
    description = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    splits = relationship("ExpenseSplit", back_populates="expense")


class ExpenseSplit(Base):
    __tablename__ = "expense_splits"

    id = Column(Integer, primary_key=True)
    expense_id = Column(Integer, ForeignKey("expenses.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    share_amount = Column(Numeric(14, 2), nullable=False)
    settled = Column(Boolean, nullable=False, default=False)

    expense = relationship("Expense", back_populates="splits")


class Settlement(Base):
    """A record of one member paying another to settle group debt directly
    (outside the wallet/transfer system -- e.g. cash in real life, logged here)."""
    __tablename__ = "settlements"

    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=False)
    from_user = Column(Integer, ForeignKey("users.id"), nullable=False)
    to_user = Column(Integer, ForeignKey("users.id"), nullable=False)
    amount = Column(Numeric(14, 2), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
