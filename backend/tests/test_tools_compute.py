import pytest

from app.engine.options import generate_options as engine_generate_options
from app.fixtures import list_fixtures, load_fixture
from app.tools import call_tool
from app.tools.compute import build_decision, build_options_input
from fixture_inputs import options_input
from test_engine_options import _basis


def _ok(name, args, ctx):
    r = call_tool(name, args, ctx)
    assert r.ok, r.error
    return r.output


BASIS_NAME = {"s3_real_surge": "recent_run_rate", "s3_promo_uplift": "promo_adjusted"}


@pytest.mark.parametrize("fx", list_fixtures(), ids=lambda f: f.id)
def test_db_adapter_and_fixture_adapter_give_identical_options(fx, tool_ctx) -> None:
    """The tools read the DB; the engine tests read the fixture JSON. Both must agree exactly."""
    ctx = tool_ctx(fx.id)
    from_db = engine_generate_options(build_options_input(ctx, fx.trigger.node, fx.trigger.sku,
                                                          BASIS_NAME.get(fx.id, "forecast")))
    from_fixture = engine_generate_options(options_input(fx, basis=_basis(fx)))
    assert [o.id for o in from_db.options] == [o.id for o in from_fixture.options]
    assert from_db.reference == from_fixture.reference.model_copy(
        update={"inbound_counted": from_db.reference.inbound_counted})


def test_net_requirement_tool_flags_stale_inventory_that_matters(tool_ctx) -> None:
    out = _ok("calculate_net_requirement", {"node": "BOG-02", "sku": "LECHE-ALQ-1L"}, tool_ctx("s1_stale_inventory"))
    assert out["requirement"]["net"] == 0
    assert [(i["code"], i["source"]) for i in out["data_issues"]] == [("STALE_DATA", "inventory")]
    assert out["inventory_sensitivity"]["adjusted"]["order_qty"] == 240
    assert out["data_blocks_decision"] is True

    fresh = _ok("calculate_net_requirement", {"node": "BOG-01", "sku": "LECHE-ALQ-1L"}, tool_ctx("s1_overstock"))
    assert fresh["data_issues"] == [] and fresh["data_blocks_decision"] is False
    assert (fresh["requirement"]["net"], fresh["requirement"]["order_qty"]) == (140, 240)


def test_run_rate_basis_requires_a_sustained_shift(tool_ctx) -> None:
    r = call_tool("generate_options", {"node": "CDMX-02", "sku": "AGUA-CIEL-1L", "demand_basis": "recent_run_rate"},
                  tool_ctx("s3_one_off_outlier"))
    assert not r.ok and r.error["code"] == "INSUFFICIENT_EVIDENCE"
    assert r.error["details"]["classification"] == "ONE_OFF_OUTLIER" and r.error["details"]["bulk_days"] == [-4]

    out = _ok("generate_options", {"node": "CDMX-02", "sku": "AGUA-CIEL-1L", "demand_basis": "recent_run_rate"},
              tool_ctx("s3_real_surge"))
    assert out["options"][0]["id"] == "INCREASE:PO-3001:108"


def test_generate_options_stores_the_full_set_and_returns_a_summary(tool_ctx) -> None:
    ctx = tool_ctx("s1_overstock")
    out = _ok("generate_options", {"node": "BOG-01", "sku": "LECHE-ALQ-1L"}, ctx)
    assert out["recommendation"] == {"id": "REC-101", "qty": 800, "deviation_pct": 233.33, "acceptable_as_is": False}
    assert out["options"][0]["id"] == "BUY:SUP-ALQ:240" and "projection" not in out["options"][0]
    assert ctx.state.options["options"][0]["projection"] == [100, 40, 100, 280, 220]

    proj = _ok("project_inventory", {"node": "BOG-01", "sku": "LECHE-ALQ-1L", "option_id": "BUY:SUP-ALQ:240"}, ctx)
    assert proj["projection"]["end_levels"] == [100, 40, 100, 280, 220] and proj["horizon"] == 5
    bad = call_tool("project_inventory", {"node": "BOG-01", "sku": "LECHE-ALQ-1L", "option_id": "BUY:X:1"}, ctx)
    assert bad.error["code"] == "UNKNOWN_OPTION" and "BUY:SUP-ALQ:240" in bad.error["details"]["known"]


def test_project_before_options_and_what_if_receipts(tool_ctx) -> None:
    ctx = tool_ctx("s2_partial_needs_alternate")
    assert call_tool("project_inventory", {"node": "CDMX-01", "sku": "LECHE-LALA-1L", "option_id": "X"},
                     ctx).error["code"] == "NO_OPTIONS"
    out = _ok("project_inventory", {"node": "CDMX-01", "sku": "LECHE-LALA-1L",
                                    "hypothetical": [{"day": 2, "qty": 204}]}, ctx)
    assert out["projection"]["end_levels"] == [90, 250, 364, 274, 184]


def test_evaluate_constraints_tool_returns_reason_codes(tool_ctx) -> None:
    out = _ok("evaluate_constraints", {"node": "BOG-01", "sku": "LECHE-ALQ-1L", "supplier_id": "SUP-ALQ",
                                       "deliveries": [{"day": 3, "qty": 10000}]}, tool_ctx("x_prompt_injection"))
    codes = {v["code"]: v["detail"] for v in out["violations"]}
    assert not out["ok"]
    assert codes["STORAGE_EXCEEDED"]["max"] == 672 and codes["BUDGET_EXCEEDED"]["max_qty"] == 708


def test_build_decision_from_tool_state(tool_ctx) -> None:
    ctx = tool_ctx("s1_overstock")
    _ok("generate_options", {"node": "BOG-01", "sku": "LECHE-ALQ-1L"}, ctx)
    d = build_decision(ctx, "BUY:SUP-ALQ:240", investigate=False, information_needed=[])
    assert (d.outcome, d.quantity, d.confidence) == ("MODIFY", 240, "high")

    stale = tool_ctx("s1_stale_inventory")
    _ok("generate_options", {"node": "BOG-02", "sku": "LECHE-ALQ-1L"}, stale)
    d = build_decision(stale, None, investigate=True, information_needed=["fresh cycle count"])
    assert d.outcome == "INVESTIGATE" and d.information_needed[0] == "fresh cycle count"
    assert any(f.name == "stock count sensitivity" and f.effect.startswith("decision flips") for f in d.factors)
