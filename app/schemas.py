from pydantic import BaseModel, EmailStr
from decimal import Decimal
from typing import List, Optional
from datetime import datetime


class UserCreate(BaseModel):
    name: str
    email: EmailStr


class UserOut(BaseModel):
    id: int
    name: str
    email: str
    account_id: int
    balance: Decimal

    class Config:
        from_attributes = True


class DepositRequest(BaseModel):
    amount: Decimal


class TransferRequest(BaseModel):
    from_account: int
    to_account: int
    amount: Decimal
    idempotency_key: str


class LedgerEntryOut(BaseModel):
    account_id: int
    type: str
    amount: Decimal


class TransferResponse(BaseModel):
    transaction_id: int
    status: str
    from_account_balance: Decimal
    to_account_balance: Decimal
    ledger_entries: List[LedgerEntryOut]


class BalanceOut(BaseModel):
    account_id: int
    balance: Decimal


class TransactionOut(BaseModel):
    id: int
    from_account: int
    to_account: int
    amount: Decimal
    status: str
    created_at: datetime

    class Config:
        from_attributes = True


class GroupCreate(BaseModel):
    name: str
    member_ids: List[int]


class ExpenseSplitIn(BaseModel):
    user_id: int
    share_amount: Optional[Decimal] = None  # required if split_type == custom


class ExpenseCreate(BaseModel):
    paid_by: int
    amount: Decimal
    description: Optional[str] = None
    split_type: str  # "equal" | "custom"
    splits: List[ExpenseSplitIn] = []


class GroupBalanceOut(BaseModel):
    user_id: int
    net_amount: Decimal  # positive = owed to them, negative = they owe


class SettleRequest(BaseModel):
    from_user: int
    to_user: int
    amount: Decimal
