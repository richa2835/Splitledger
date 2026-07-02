from fastapi import APIRouter, Depends, HTTPException, Query, Response, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User, Account, Transaction, TransactionStatus
from app.schemas import (
    UserCreate, UserOut, DepositRequest, TransferRequest, TransferResponse,
    BalanceOut, TransactionOut, LedgerEntryOut,
)
from app.services import transfer_service
from app.services.rate_limit import check_rate_limit, CAPACITY

router = APIRouter()


def enforce_rate_limit(request: Request, response: Response):
    # Identity: real deployment would use the authenticated user/API key.
    # Falling back to client IP keeps this demoable without auth wired up.
    identity = request.client.host if request.client else "anonymous"
    allowed, remaining = check_rate_limit(identity)
    response.headers["X-RateLimit-Limit"] = str(CAPACITY)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    if not allowed:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")


@router.post("/users", response_model=UserOut, status_code=201)
def create_user(payload: UserCreate, db: Session = Depends(get_db)):
    if db.query(User).filter_by(email=payload.email).first():
        raise HTTPException(status_code=400, detail="Email already registered")

    user = User(name=payload.name, email=payload.email)
    db.add(user)
    db.commit()
    db.refresh(user)

    account = Account(user_id=user.id, balance=0, version=0)
    db.add(account)
    db.commit()
    db.refresh(account)

    return UserOut(id=user.id, name=user.name, email=user.email, account_id=account.id, balance=account.balance)


@router.post("/accounts/{account_id}/deposit", response_model=BalanceOut)
def deposit(account_id: int, payload: DepositRequest, db: Session = Depends(get_db)):
    if payload.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")

    # FOR UPDATE here too -- a deposit racing with a transfer FROM this same
    # account is still a "two writers, one account" problem.
    account = db.query(Account).filter_by(id=account_id).with_for_update().first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    account.balance += payload.amount
    account.version += 1
    db.commit()
    db.refresh(account)

    return BalanceOut(account_id=account.id, balance=account.balance)


@router.post("/transfer", response_model=TransferResponse, dependencies=[Depends(enforce_rate_limit)])
def transfer(payload: TransferRequest, strategy: str = Query("pessimistic", enum=["pessimistic", "optimistic"]),
             db: Session = Depends(get_db)):
    if payload.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    if payload.from_account == payload.to_account:
        raise HTTPException(status_code=400, detail="Cannot transfer to the same account")

    if not db.query(Account).filter_by(id=payload.from_account).first():
        raise HTTPException(status_code=404, detail="from_account not found")
    if not db.query(Account).filter_by(id=payload.to_account).first():
        raise HTTPException(status_code=404, detail="to_account not found")

    txn, is_new = transfer_service.claim_idempotency_key(
        db, payload.idempotency_key, payload.from_account, payload.to_account, payload.amount
    )

    if not is_new:
        if txn.status == TransactionStatus.completed:
            return _build_response(db, txn)
        elif txn.status == TransactionStatus.pending:
            raise HTTPException(status_code=409, detail="Request with this idempotency key is already in progress")
        else:  # failed
            raise HTTPException(status_code=400, detail="This idempotency key previously failed (insufficient funds)")

    try:
        if strategy == "pessimistic":
            transfer_service.apply_transfer_pessimistic(db, txn, payload.from_account, payload.to_account, payload.amount)
        else:
            transfer_service.apply_transfer_optimistic(db, txn, payload.from_account, payload.to_account, payload.amount)
    except transfer_service.InsufficientFunds:
        raise HTTPException(status_code=400, detail="Insufficient funds")

    return _build_response(db, txn)


def _build_response(db: Session, txn: Transaction) -> TransferResponse:
    from_acc = db.query(Account).filter_by(id=txn.from_account).one()
    to_acc = db.query(Account).filter_by(id=txn.to_account).one()
    return TransferResponse(
        transaction_id=txn.id,
        status=txn.status.value,
        from_account_balance=from_acc.balance,
        to_account_balance=to_acc.balance,
        ledger_entries=[
            LedgerEntryOut(account_id=txn.from_account, type="debit", amount=txn.amount),
            LedgerEntryOut(account_id=txn.to_account, type="credit", amount=txn.amount),
        ],
    )


@router.get("/accounts/{account_id}/balance", response_model=BalanceOut)
def get_balance(account_id: int, db: Session = Depends(get_db)):
    account = db.query(Account).filter_by(id=account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    return BalanceOut(account_id=account.id, balance=account.balance)


@router.get("/accounts/{account_id}/transactions")
def get_transactions(account_id: int, page: int = 1, limit: int = 20, db: Session = Depends(get_db)):
    if page < 1 or limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="Invalid pagination params")

    query = db.query(Transaction).filter(
        (Transaction.from_account == account_id) | (Transaction.to_account == account_id)
    ).order_by(Transaction.created_at.desc())

    total = query.count()
    items = query.offset((page - 1) * limit).limit(limit).all()

    return {
        "page": page,
        "limit": limit,
        "total": total,
        "items": [TransactionOut.model_validate(t) for t in items],
    }
