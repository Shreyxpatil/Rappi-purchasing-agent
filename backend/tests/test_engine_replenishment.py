import pytest

from app.engine.replenishment import calculate_net_requirement, ceil_to, floor_to
from app.engine.types import EngineInputError, NetRequirementInput, Receipt
from app.fixtures import list_fixtures
from fixture_inputs import net_input

# S3 cases use an adjusted demand basis; they are tested with the demand-shift engine.
FORECAST_BASIS = [f for f in list_fixtures() if "net" in f.expected.computed and f.scenario != "S3"]


@pytest.mark.parametrize("fx", FORECAST_BASIS, ids=lambda f: f.id)
def test_matches_hand_computed_fixture_values(fx) -> None:
    c = fx.expected.computed
    r = calculate_net_requirement(net_input(fx))
    for key in ("horizon", "demand", "safety_stock", "available", "inbound", "net", "order_qty"):
        if key in c:
            assert getattr(r, key) == c[key], key


def _inp(**kw) -> NetRequirementInput:
    base = dict(forecast=[60] * 10, on_hand=180, reserved=20, inbound=[], lead_time_days=3,
                review_period_days=2, safety_days=2, moq=240, case_pack=12)
    return NetRequirementInput(**{**base, **kw})


def test_net_below_moq_is_raised_to_moq_and_explained() -> None:
    r = calculate_net_requirement(_inp(inbound=[Receipt(day=2, qty=120)]))
    assert (r.net, r.order_qty) == (140, 240)
    assert r.rounding == ["raised 140 to MOQ 240"]


def test_net_above_moq_rounds_up_to_case_pack() -> None:
    r = calculate_net_requirement(_inp(moq=100, inbound=[Receipt(day=2, qty=120)]))
    assert r.order_qty == 144
    assert r.rounding == ["rounded 140 up to case pack 12: 144"]


def test_non_positive_net_orders_nothing() -> None:
    r = calculate_net_requirement(_inp(on_hand=600))
    assert r.net < 0 and r.order_qty == 0 and r.rounding == []


def test_inbound_after_horizon_does_not_count() -> None:
    late = Receipt(day=6, qty=500, source="PO-late")
    r = calculate_net_requirement(_inp(inbound=[late]))
    assert r.inbound == 0 and r.inbound_excluded == [late]


def test_uses_daily_forecast_not_a_flat_average() -> None:
    r = calculate_net_requirement(_inp(forecast=[100, 0, 0, 0, 50, 999], moq=0, case_pack=1))
    assert r.demand == 150  # only days 0..4 of a 5-day horizon


def test_short_forecast_is_a_typed_error() -> None:
    with pytest.raises(EngineInputError) as e:
        calculate_net_requirement(_inp(forecast=[60, 60]))
    assert e.value.code == "FORECAST_TOO_SHORT"


@pytest.mark.parametrize("x,m,up,down", [(140, 12, 144, 132), (144, 12, 144, 144), (0, 12, 0, 0),
                                         (-5, 6, 0, 0), (290, 6, 294, 288)])
def test_rounding_helpers(x, m, up, down) -> None:
    assert ceil_to(x, m) == up and floor_to(x, m) == down
