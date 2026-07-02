from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Group, GroupMember, User, Expense, ExpenseSplit, Settlement
from app.schemas import GroupCreate, ExpenseCreate, GroupBalanceOut, SettleRequest
from app.services.settlement_service import compute_net_balances, simplify_debts

router = APIRouter()


@router.post("/groups", status_code=201)
def create_group(payload: GroupCreate, db: Session = Depends(get_db)):
    missing = [uid for uid in payload.member_ids if not db.query(User).filter_by(id=uid).first()]
    if missing:
        raise HTTPException(status_code=400, detail=f"Unknown user_ids: {missing}")

    group = Group(name=payload.name)
    db.add(group)
    db.commit()
    db.refresh(group)

    for uid in payload.member_ids:
        db.add(GroupMember(group_id=group.id, user_id=uid))
    db.commit()

    return {"id": group.id, "name": group.name, "member_ids": payload.member_ids}


def _split_equal(amount: Decimal, user_ids: list[int]) -> dict[int, Decimal]:
    """
    Divide `amount` equally among user_ids, distributing rounding remainder
    cent-by-cent so the shares always sum EXACTLY to amount (never off by a
    cent due to floating/rounding -- that mismatch would silently corrupt
    the ledger invariant).
    """
    n = len(user_ids)
    base = (amount / n).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    shares = {uid: base for uid in user_ids}
    remainder = amount - (base * n)
    cents = int((remainder * 100).to_integral_value())
    step = Decimal("0.01") if cents > 0 else Decimal("-0.01")
    for uid in user_ids[:abs(cents)]:
        shares[uid] += step
    return shares


@router.post("/groups/{group_id}/expenses", status_code=201)
def add_expense(group_id: int, payload: ExpenseCreate, db: Session = Depends(get_db)):
    group = db.query(Group).filter_by(id=group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    if payload.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")

    expense = Expense(group_id=group_id, paid_by=payload.paid_by, amount=payload.amount, description=payload.description)
    db.add(expense)
    db.commit()
    db.refresh(expense)

    if payload.split_type == "equal":
        user_ids = [s.user_id for s in payload.splits] or [
            gm.user_id for gm in db.query(GroupMember).filter_by(group_id=group_id).all()
        ]
        shares = _split_equal(payload.amount, user_ids)
    elif payload.split_type == "custom":
        if not payload.splits:
            raise HTTPException(status_code=400, detail="custom split requires splits[]")
        shares = {s.user_id: s.share_amount for s in payload.splits}
        if sum(shares.values()) != payload.amount:
            raise HTTPException(status_code=400, detail="Custom splits must sum to the expense amount")
    else:
        raise HTTPException(status_code=400, detail="split_type must be 'equal' or 'custom'")

    for uid, share in shares.items():
        db.add(ExpenseSplit(expense_id=expense.id, user_id=uid, share_amount=share))
    db.commit()

    return {"expense_id": expense.id, "splits": [{"user_id": uid, "share_amount": str(share)} for uid, share in shares.items()]}


@router.get("/groups/{group_id}/balances")
def get_group_balances(group_id: int, db: Session = Depends(get_db)):
    group = db.query(Group).filter_by(id=group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    net = compute_net_balances(db, group_id)
    simplified = simplify_debts(net)

    return {
        "net_balances": [GroupBalanceOut(user_id=uid, net_amount=amt) for uid, amt in net.items()],
        "simplified_settlements": [
            {"from_user": frm, "to_user": to, "amount": str(amt)} for frm, to, amt in simplified
        ],
    }


@router.post("/groups/{group_id}/settle", status_code=201)
def settle(group_id: int, payload: SettleRequest, db: Session = Depends(get_db)):
    group = db.query(Group).filter_by(id=group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    if payload.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")

    s = Settlement(group_id=group_id, from_user=payload.from_user, to_user=payload.to_user, amount=payload.amount)
    db.add(s)
    db.commit()
    db.refresh(s)

    return {"settlement_id": s.id, "status": "recorded"}
