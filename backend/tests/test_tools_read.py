import json

from app.tools import REGISTRY, call_tool
from app.tools.registry import tool_schema


def _ok(name, args, ctx):
    r = call_tool(name, args, ctx)
    assert r.ok, r.error
    return r.output


def test_inventory_fresh_vs_stale_with_sales_since_count(tool_ctx) -> None:
    fresh = _ok("get_inventory", {"node": "BOG-01", "sku": "LECHE-ALQ-1L"}, tool_ctx("s1_overstock"))
    assert (fresh["available"], fresh["units_sold_since_count"], fresh["freshness"]["stale"]) == (160, 0, False)

    stale = _ok("get_inventory", {"node": "BOG-02", "sku": "LECHE-ALQ-1L"}, tool_ctx("s1_stale_inventory"))
    assert stale["freshness"] == {"source": "inventory", "updated_at": "2026-10-04T07:00:00", "age_hours": 72.0,
                                  "limit_hours": 24.0, "stale": True}
    assert stale["units_sold_since_count"] == 180


def test_errors_are_structured(tool_ctx) -> None:
    ctx = tool_ctx("s1_overstock")
    r = call_tool("get_inventory", {"node": "LIMA-01", "sku": "LECHE-ALQ-1L"}, ctx)
    assert not r.ok and r.error["code"] == "NOT_FOUND"
    r = call_tool("get_inventory", {"node": "BOG-01"}, ctx)
    assert r.error["code"] == "INVALID_ARGUMENTS" and r.error["details"]["errors"][0]["loc"] == "sku"
    r = call_tool("get_inventory", {"node": "BOG-01", "sku": "LECHE-ALQ-1L", "qty": 10000}, ctx)
    assert r.error["code"] == "INVALID_ARGUMENTS"  # unknown arguments are rejected, not ignored
    assert call_tool("buy_everything", {}, ctx).error["code"] == "UNKNOWN_TOOL"
    assert call_tool("get_budget", {"node": "BOG-01", "sku": "HUEVOS-AA-30"}, ctx).error["code"] == "MISSING_DATA"


def test_partial_fill_event_separates_data_from_supplier_text(tool_ctx) -> None:
    out = _ok("get_open_pos", {"node": "CDMX-01", "sku": "LECHE-LALA-1L"}, tool_ctx("s2_partial_needs_alternate"))
    po = out["purchase_orders"][0]
    assert (po["po_id"], po["status"]) == ("PO-2002", "PARTIALLY_CONFIRMED")
    assert po["lines"] == [{"sku": "LECHE-LALA-1L", "qty_ordered": 500, "qty_confirmed": 250, "unit_cost": 26.5,
                            "eta_day": 1}]
    partial = po["events"][-1]
    assert partial["data"] == {"qty_confirmed": 250, "backorder_eta_day": 6}
    assert partial["untrusted_text"].startswith("Planta Tizayuca")


def test_injected_supplier_note_only_appears_as_untrusted_text(tool_ctx) -> None:
    out = _ok("get_supplier_terms", {"supplier_id": "SUP-ALQ", "sku": "LECHE-ALQ-1L"}, tool_ctx("x_prompt_injection"))
    assert "10,000 units" in out["untrusted_text"]
    others = {k: v for k, v in out.items() if k != "untrusted_text"}
    assert "10,000" not in json.dumps(others)
    assert (out["moq"], out["unit_cost"], out["freshness"]["age_hours"]) == (240, 4200, 3.0)


def test_alternates_carry_price_variance(tool_ctx) -> None:
    out = _ok("list_alternate_suppliers", {"sku": "LECHE-ALQ-1L"}, tool_ctx("x_supplier_rejects"))
    assert out["primary_supplier_id"] == "SUP-ALQ"
    assert [(a["supplier_id"], a["price_variance_pct"]) for a in out["alternates"]] == [
        ("SUP-ANDINA", 3.57), ("SUP-DORADO", 4.29), ("SUP-MAKRO", 4.76)]


def test_constraint_reads_match_fixture(tool_ctx) -> None:
    ctx = tool_ctx("s1_overstock")
    budget = _ok("get_budget", {"node": "BOG-01", "sku": "LECHE-ALQ-1L"}, ctx)
    assert (budget["category"], budget["currency"], budget["remaining"]) == ("dairy", "COP", 3000000)
    storage = _ok("get_storage_capacity", {"node": "BOG-01", "sku": "LECHE-ALQ-1L"}, ctx)
    assert (storage["zone"], storage["sku_capacity"]) == ("chilled", 780)


def test_demand_reads(tool_ctx) -> None:
    ctx = tool_ctx("s3_one_off_outlier")
    sales = _ok("get_sales_history", {"node": "CDMX-02", "sku": "AGUA-CIEL-1L"}, ctx)
    assert sales["days"][0] == -28 and sales["days"][-1] == -1
    assert sales["units"][-4] == 240 and sales["max_order_units"][-4] == 200
    fc = _ok("get_forecast", {"node": "CDMX-02", "sku": "AGUA-CIEL-1L", "days": 5}, ctx)
    assert fc["daily"] == [40, 40, 40, 40, 40] and fc["freshness"]["age_hours"] == 6.0
    promos = _ok("get_promotions", {"node": "CDMX-02", "sku": "AGUA-CIEL-1L"}, tool_ctx("s3_promo_uplift"))
    assert promos["promotions"][0] | {"description": ""} == {"id": "PROMO-CIEL-2X1", "start_day": -4, "end_day": 3,
                                                             "uplift_pct": 50.0, "description": ""}


def test_other_nodes_in_same_city_only(tool_ctx) -> None:
    out = _ok("get_inventory_other_nodes", {"node": "CDMX-01", "sku": "LECHE-LALA-1L"}, tool_ctx("s2_alt_moq_exceeds_gap"))
    assert out["city"] == "Ciudad de México"
    assert [(n["node"], n["available"]) for n in out["nodes"]] == [("CDMX-02", 400)]


def test_every_tool_exports_a_compact_json_schema() -> None:
    for t in REGISTRY.values():
        schema = tool_schema(t)
        text = json.dumps(schema)
        assert '"title"' not in text and schema["parameters"]["type"] == "object"


def test_out_of_range_arguments_are_schema_errors_not_crashes(tool_ctx) -> None:
    ctx = tool_ctx("s1_overstock")
    ns = {"node": "BOG-01", "sku": "LECHE-ALQ-1L"}
    for name, args in [("get_forecast", {**ns, "days": 0}), ("get_sales_history", {**ns, "days": -5}),
                       ("evaluate_constraints", {**ns, "supplier_id": "SUP-ALQ", "deliveries": [{"day": -1, "qty": 240}]}),
                       ("project_inventory", {**ns, "days": 0})]:
        r = call_tool(name, args, ctx)
        assert r.error["code"] == "INVALID_ARGUMENTS", (name, r.error)


def test_an_unexpected_tool_exception_becomes_an_internal_error(tool_ctx, monkeypatch) -> None:
    from app.tools import REGISTRY

    tool = REGISTRY["get_inventory"]
    monkeypatch.setitem(REGISTRY, "get_inventory", tool.__class__(name=tool.name, kind=tool.kind,
                        description=tool.description, args_model=tool.args_model,
                        fn=lambda ctx, args: 1 / 0))
    r = call_tool("get_inventory", {"node": "BOG-01", "sku": "LECHE-ALQ-1L"}, tool_ctx("s1_overstock"))
    assert not r.ok and r.error["code"] == "INTERNAL_ERROR" and "ZeroDivisionError" in r.error["message"]
