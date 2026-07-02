"""
Turns raw "who paid what" expense records into a simplified list of
"who should pay whom" -- minimizing the number of payments needed to
settle everyone up. This is the classic greedy debt-simplification problem:
repeatedly match the person owed the most with the person who owes the most.
"""

from collections import defaultdict
from decimal import Decimal
from typing import Dict, List, Tuple

from sqlalchemy.orm import Session

from app.models import Expense, ExpenseSplit, Settlement


def compute_net_balances(db: Session, group_id: int) -> Dict[int, Decimal]:
    """
    Positive net balance = this user is OWED money overall.
    Negative net balance = this user OWES money overall.
    """
    net: Dict[int, Decimal] = defaultdict(lambda: Decimal("0"))

    expenses = db.query(Expense).filter_by(group_id=group_id).all()
    for expense in expenses:
        splits = db.query(ExpenseSplit).filter_by(expense_id=expense.id).all()
        for split in splits:
            if split.user_id == expense.paid_by:
                continue  # paying your own share is a no-op on net balance
            net[split.user_id] -= split.share_amount
            net[expense.paid_by] += split.share_amount

    settlements = db.query(Settlement).filter_by(group_id=group_id).all()
    for s in settlements:
        # from_user paid to_user directly -> from_user owes less, to_user is owed less
        net[s.from_user] += s.amount
        net[s.to_user] -= s.amount

    return dict(net)


def simplify_debts(net_balances: Dict[int, Decimal]) -> List[Tuple[int, int, Decimal]]:
    """
    Greedy minimization: repeatedly pair the biggest creditor with the
    biggest debtor. Returns a list of (from_user, to_user, amount) meaning
    "from_user should pay to_user this amount."
    """
    creditors = sorted(
        [[uid, amt] for uid, amt in net_balances.items() if amt > 0],
        key=lambda x: x[1], reverse=True,
    )
    debtors = sorted(
        [[uid, -amt] for uid, amt in net_balances.items() if amt < 0],
        key=lambda x: x[1], reverse=True,
    )

    settlements: List[Tuple[int, int, Decimal]] = []
    i, j = 0, 0
    while i < len(debtors) and j < len(creditors):
        debtor_id, debtor_amt = debtors[i]
        creditor_id, creditor_amt = creditors[j]

        pay = min(debtor_amt, creditor_amt)
        if pay > 0:
            settlements.append((debtor_id, creditor_id, pay))

        debtors[i][1] -= pay
        creditors[j][1] -= pay

        if debtors[i][1] == 0:
            i += 1
        if creditors[j][1] == 0:
            j += 1

    return settlements
