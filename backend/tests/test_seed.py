from datetime import date, datetime

from sqlalchemy import select

from app.fixtures import ScenarioFixture
from app.models import (AgentRun, Forecast, Inventory, POLine, PurchaseOrder, SalesDaily, Supplier,
                        SupplierProduct, Workspace)
from app.seed import seed_workspace

AS_OF = datetime(2026, 10, 7, 7, 0)


def _fixture(**seed) -> ScenarioFixture:
    return ScenarioFixture.model_validate({
        "id": "t1", "scenario": "S1", "title": "t", "description": "t", "as_of": AS_OF,
        "seed": seed,
        "trigger": {"type": "recommendation_review", "node": "BOG-01", "sku": "LECHE-ALQ-1L"},
        "supplier_behaviour": {"SUP-ALQ": [{"type": "PARTIAL", "qty": 250}]},
        "expected": {"outcome": "ACCEPT", "qty_min": 0, "qty_max": 0, "final_status": "COMPLETED",
                     "option_kind": "NO_ACTION"},
        "rationale": ["test"],
    })


def _dump(session) -> list:
    rows = []
    for model in (Inventory, Forecast, SalesDaily, POLine, SupplierProduct):
        for r in session.scalars(select(model)):
            rows.append({c.name: getattr(r, c.name) for c in model.__table__.columns if c.name != "id"})
    return rows


def test_relative_days_resolve_against_as_of(session) -> None:
    seed_workspace(session, _fixture(
        inventory=[{"node": "BOG-01", "sku": "LECHE-ALQ-1L", "on_hand": 180, "reserved": 20, "updated_hours_ago": 72}],
        forecasts=[{"node": "BOG-01", "sku": "LECHE-ALQ-1L", "flat": 60, "days": 3}],
        sales=[{"node": "BOG-01", "sku": "LECHE-ALQ-1L", "daily": [50, 70], "stockout_days": [-1]}],
        purchase_orders=[{"id": "PO-1", "node": "BOG-01", "supplier": "SUP-ALQ", "status": "CONFIRMED",
                          "lines": [{"sku": "LECHE-ALQ-1L", "qty": 120, "qty_confirmed": 120, "eta_day": 2}]}],
    ))
    inv = session.scalars(select(Inventory)).one()
    assert inv.updated_at == datetime(2026, 10, 4, 7, 0)
    assert [f.day for f in session.scalars(select(Forecast).order_by(Forecast.day))] == [
        date(2026, 10, 7), date(2026, 10, 8), date(2026, 10, 9)]
    sales = list(session.scalars(select(SalesDaily).order_by(SalesDaily.day)))
    assert [(s.day, s.units, s.stockout) for s in sales] == [
        (date(2026, 10, 5), 50, False), (date(2026, 10, 6), 70, True)]
    po = session.get(PurchaseOrder, "PO-1")
    assert po.currency == "COP"
    assert po.lines[0].unit_cost == 4200  # defaulted from supplier terms
    assert po.lines[0].expected_arrival == date(2026, 10, 9)
    assert po.events[0].type == "CREATED"


def test_supplier_overrides_and_new_terms(session) -> None:
    seed_workspace(session, _fixture(
        suppliers=[{"id": "SUP-ALQ", "active": False}],
        supplier_terms=[
            {"supplier": "SUP-CEDA", "sku": "LECHE-LALA-1L", "moq": 480, "notes": "ships full pallets only"},
            {"supplier": "SUP-MAKRO", "sku": "COCA-1.5L", "unit_cost": 6100, "moq": 60, "case_pack": 6,
             "lead_time_days": 1, "hist_fill_rate": 0.9},
        ],
    ))
    assert session.get(Supplier, "SUP-ALQ").active is False
    ceda = session.scalars(select(SupplierProduct).filter_by(supplier_id="SUP-CEDA")).one()
    assert (ceda.moq, ceda.case_pack, ceda.notes) == (480, 12, "ships full pallets only")
    makro = session.scalars(select(SupplierProduct).filter_by(supplier_id="SUP-MAKRO", sku="COCA-1.5L")).one()
    assert (makro.unit_cost, makro.currency) == (6100, "COP")


def test_seeding_is_deterministic_and_reseed_keeps_run_history(session) -> None:
    fx = _fixture(
        inventory=[{"node": "BOG-01", "sku": "LECHE-ALQ-1L", "on_hand": 180}],
        sales=[{"node": "BOG-01", "sku": "LECHE-ALQ-1L", "flat": 60, "days": 28}],
    )
    seed_workspace(session, fx)
    first = _dump(session)
    session.add(AgentRun(scenario_id="t1", provider="scripted", trigger={}, status="COMPLETED",
                         state="REPORT", started_at=AS_OF))
    session.flush()

    seed_workspace(session, fx)
    assert _dump(session) == first
    assert session.scalars(select(AgentRun)).one().scenario_id == "t1"
    ws = session.get(Workspace, 1)
    assert ws.config["supplier_behaviour"] == {"SUP-ALQ": [{"type": "PARTIAL", "qty": 250, "message": ""}]}
