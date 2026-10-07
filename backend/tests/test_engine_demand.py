import pytest

from app.engine.demand import apply_promotions, detect_demand_shift, run_rate_forecast
from app.engine.replenishment import calculate_net_requirement
from app.engine.types import PromoWindow, SalesDay
from app.fixtures import list_fixtures, load_fixture
from fixture_inputs import forecast, net_input, promo_windows, sales_days

S3 = [f for f in list_fixtures() if f.scenario == "S3"]


def _signal(fx):
    node, sku = fx.trigger.node, fx.trigger.sku
    return detect_demand_shift(sales_days(fx, node, sku), promo_windows(fx, node, sku))


@pytest.mark.parametrize("fx", S3, ids=lambda f: f.id)
def test_classification_matches_fixture(fx) -> None:
    c = fx.expected.computed
    s = _signal(fx)
    assert s.classification == c["classification"]
    assert s.baseline_daily == c["baseline_daily"]
    assert s.recent_daily == c["recent_daily"]
    assert len(s.elevated_days) == c["elevated_days"]
    if "adjusted_daily" in c:
        assert s.adjusted_daily == c["adjusted_daily"]


def test_one_off_outlier_points_at_the_bulk_day() -> None:
    fx = load_fixture("s3_one_off_outlier")
    s = _signal(fx)
    assert s.bulk_days == [fx.expected.computed["spike_day"]] and s.suggested_basis == "forecast"


def test_real_surge_net_on_run_rate_basis() -> None:
    fx = load_fixture("s3_real_surge")
    c = fx.expected.computed
    s = _signal(fx)
    assert s.suggested_basis == "recent_run_rate"
    on_forecast = calculate_net_requirement(net_input(fx))
    on_run_rate = calculate_net_requirement(net_input(fx, basis=run_rate_forecast(s.adjusted_daily, 14)))
    assert on_forecast.net == c["net_on_forecast"]
    assert (on_run_rate.demand, on_run_rate.safety_stock, on_run_rate.net, on_run_rate.order_qty) == (
        c["demand"], c["safety_stock"], c["net"], c["order_qty"])


def test_promo_uplift_net_on_promo_adjusted_basis() -> None:
    fx = load_fixture("s3_promo_uplift")
    c = fx.expected.computed
    node, sku = fx.trigger.node, fx.trigger.sku
    basis = apply_promotions(forecast(fx, node, sku), promo_windows(fx, node, sku))
    assert basis[:5] == [60, 60, 60, 60, 40]
    r = calculate_net_requirement(net_input(fx, basis=basis))
    assert (r.demand, r.safety_stock, r.net, r.order_qty) == (c["demand"], c["safety_stock"], c["net"], c["order_qty"])


def _history(recent: list[int], stockout_days=(), max_order=4) -> list[SalesDay]:
    base = [SalesDay(day=d, units=40, orders=27, max_order_units=4) for d in range(-28, -7)]
    rec = [SalesDay(day=d, units=u, orders=27, max_order_units=max_order, stockout=d in stockout_days)
           for d, u in zip(range(-7, 0), recent)]
    return base + rec


def test_two_unexplained_spikes_are_inconclusive_not_a_shift() -> None:
    s = detect_demand_shift(_history([40, 40, 70, 40, 72, 40, 40]), [])
    assert s.classification == "INCONCLUSIVE" and s.suggested_basis == "forecast"


def test_stockouts_in_recent_window_mean_sales_understate_demand() -> None:
    s = detect_demand_shift(_history([40, 41, 12, 0, 39, 40, 40], stockout_days=(-5, -4)), [])
    assert s.classification == "STOCKOUT_CENSORED" and s.stockout_days == [-5, -4]
    assert s.adjusted_daily == 40.0  # mean of the days that were not sold out


def test_flat_sales_are_no_shift() -> None:
    assert detect_demand_shift(_history([40] * 7), []).classification == "NO_SHIFT"


def test_short_history_is_inconclusive() -> None:
    s = detect_demand_shift([SalesDay(day=-1, units=40, orders=27, max_order_units=4)], [])
    assert s.classification == "INCONCLUSIVE"


def test_apply_promotions_only_touches_covered_days() -> None:
    promo = [PromoWindow(id="P", start_day=1, end_day=2, uplift_pct=50)]
    assert apply_promotions([10, 10, 10, 10], promo) == [10, 15, 15, 10]
