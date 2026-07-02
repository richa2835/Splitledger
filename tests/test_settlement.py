from decimal import Decimal

from app.services.settlement_service import simplify_debts
from tests.conftest import make_user


def test_simplify_debts_reduces_transaction_count():
    """
    A owes B 200, B owes A 150 (net: A owes B 50).
    C owes A 30.
    Net: A = -200+150+30 = -20 ... let's use clean numbers instead:
    """
    # net balances: positive = owed money, negative = owes money
    net = {
        "A": Decimal("-50"),   # A owes 50 overall
        "B": Decimal("30"),    # B is owed 30 overall
        "C": Decimal("20"),    # C is owed 20 overall
    }
    settlements = simplify_debts(net)

    # Should resolve in at most len(net)-1 = 2 payments, not more.
    assert len(settlements) <= 2

    # Every debtor should end up paying exactly their debt, every creditor
    # exactly what they're owed, once you sum settlements per person.
    paid = {}
    received = {}
    for frm, to, amt in settlements:
        paid[frm] = paid.get(frm, Decimal("0")) + amt
        received[to] = received.get(to, Decimal("0")) + amt

    assert paid.get("A", Decimal("0")) == Decimal("50")
    assert received.get("B", Decimal("0")) + received.get("C", Decimal("0")) == Decimal("50")


def test_group_balances_endpoint_nets_correctly(client):
    a = make_user(client, "A", "a-group@test.com")
    b = make_user(client, "B", "b-group@test.com")

    group = client.post("/groups", json={"name": "Trip", "member_ids": [a["id"], b["id"]]}).json()

    # A pays 200, split equally -> B owes A 100
    client.post(f"/groups/{group['id']}/expenses", json={
        "paid_by": a["id"], "amount": "200.00", "description": "Hotel",
        "split_type": "equal", "splits": [{"user_id": a["id"]}, {"user_id": b["id"]}],
    })

    # B pays 150, split equally -> A owes B 75
    client.post(f"/groups/{group['id']}/expenses", json={
        "paid_by": b["id"], "amount": "150.00", "description": "Dinner",
        "split_type": "equal", "splits": [{"user_id": a["id"]}, {"user_id": b["id"]}],
    })

    result = client.get(f"/groups/{group['id']}/balances").json()
    simplified = result["simplified_settlements"]

    # Net: B owed A 100 - 75 = 25 net difference. Should collapse to ONE payment.
    assert len(simplified) == 1
    assert Decimal(simplified[0]["amount"]) == Decimal("25.00")
