"""Every scenario fixture loads, seeds cleanly, and its hand-computed numbers are self-consistent.

These checks guard the *expectations*, not the engine: a typo in a fixture would otherwise
make the engine tests (and the evals) assert the wrong thing.
"""

import pytest
from sqlalchemy import select

from app.db import create_schema, make_engine, make_session_factory
from app.fixtures import list_fixtures
from app.models import Inventory, Product, SupplierProduct, Workspace
from app.seed import seed_workspace

FIXTURES = list_fixtures()


def test_there_are_fixtures() -> None:
    assert len(FIXTURES) >= 17


@pytest.mark.parametrize("fx", FIXTURES, ids=lambda f: f.id)
def test_fixture_seeds_into_fresh_db(fx) -> None:
    engine = make_engine("sqlite://")
    create_schema(engine)
    with make_session_factory(engine)() as session:
        seed_workspace(session, fx)
        session.commit()
        assert session.get(Workspace, 1).scenario_id == fx.id


@pytest.mark.parametrize("fx", FIXTURES, ids=lambda f: f.id)
def test_expected_block_is_coherent(fx) -> None:
    exp = fx.expected
    assert fx.rationale, "every fixture explains its arithmetic"
    assert 0 <= exp.qty_min <= exp.qty_max
    if exp.outcome in ("REJECT", "INVESTIGATE"):
        assert exp.qty_max == 0 and not exp.final_pos
    if exp.outcome == "INVESTIGATE":
        assert exp.escalation and exp.final_status == "ESCALATED"
    for po in exp.final_pos:
        assert po.qty_min <= po.qty_max


@pytest.mark.parametrize("fx", FIXTURES, ids=lambda f: f.id)
def test_hand_computed_numbers_add_up(fx, session) -> None:
    c = fx.expected.computed
    seed_workspace(session, fx)
    target = session.scalars(select(Inventory).filter_by(node_id=fx.trigger.node, sku=fx.trigger.sku)).one()
    product = session.get(Product, fx.trigger.sku)

    if "available" in c:
        assert c["available"] == target.on_hand - target.reserved
    if "lead_time_days" in c:
        assert c["review_period_days"] == product.review_period_days
        assert c["horizon"] == c["lead_time_days"] + c["review_period_days"]
        primary = session.scalars(select(SupplierProduct).filter_by(sku=fx.trigger.sku, is_primary=True)).one()
        assert c["lead_time_days"] == primary.lead_time_days
    if {"demand", "safety_stock", "available", "inbound", "net"} <= c.keys():
        assert c["net"] == c["demand"] + c["safety_stock"] - c["available"] - c["inbound"]
    if "safety_stock" in c and "horizon" in c and "demand" in c:
        assert c["safety_stock"] == product.safety_days * c["demand"] / c["horizon"]
