"""Demand-shift detection: is a sales change real enough to act on?

Deterministic rules, so the evidence is the same every time and the LLM only interprets it:
  * elevated day   = sales > baseline x (1 + elevated_pct)
  * bulk day       = the day's largest single order explains >= bulk_share of its excess over baseline
  * promo day      = inside an active promotion window
  * SUSTAINED_SHIFT needs >= sustained_min_days organic (not bulk, not promo) elevated days
"""

from app.engine.types import DemandSignal, PromoWindow, SalesDay


def detect_demand_shift(sales: list[SalesDay], promotions: list[PromoWindow], *, recent_days: int = 7,
                        elevated_pct: float = 25.0, sustained_min_days: int = 5,
                        bulk_share: float = 0.5) -> DemandSignal:
    sales = sorted(sales, key=lambda s: s.day)
    recent = [s for s in sales if s.day >= -recent_days]
    base = [s for s in sales if s.day < -recent_days and not s.stockout]
    if not recent or not base:
        return _signal("INCONCLUSIVE", "forecast", 0.0, _mean(recent), 0.0, _mean(recent), [], [], [], [],
                       [f"not enough history: {len(base)} baseline days, {len(recent)} recent days"])

    baseline = _mean(base)
    recent_mean = _mean(recent)
    threshold = baseline * (1 + elevated_pct / 100)
    elevated = [s for s in recent if s.units > threshold]
    bulk = [s for s in elevated if s.max_order_units >= bulk_share * (s.units - baseline)]
    promo = [s for s in elevated if any(p.covers(s.day) for p in promotions)]
    organic = [s for s in elevated if s not in bulk and s not in promo]
    stockouts = [s for s in recent if s.stockout]
    adjusted = _mean([s for s in recent if s not in bulk]) or baseline

    evidence = [f"baseline {baseline:.2f}/day over {len(base)} days; recent {recent_mean:.2f}/day over {len(recent)} days",
                f"{len(elevated)} of {len(recent)} recent days above {threshold:.2f}"]
    evidence += [f"day {s.day}: {s.units} units, largest single order {s.max_order_units}" for s in bulk]
    evidence += [f"day {s.day}: inside promotion" for s in promo]

    if stockouts:
        cls, basis = "STOCKOUT_CENSORED", "forecast"
        adjusted = _mean([s for s in recent if not s.stockout]) or baseline
        evidence.append(f"{len(stockouts)} recent days sold out: recorded sales understate demand")
    elif not elevated:
        cls, basis = "NO_SHIFT", "forecast"
    elif len(organic) >= sustained_min_days:
        cls, basis = "SUSTAINED_SHIFT", "recent_run_rate"
    elif not organic and promo:
        cls, basis = "PROMO", "promo_adjusted"
    elif not organic:
        cls, basis = "ONE_OFF_OUTLIER", "forecast"
    else:
        cls, basis = "INCONCLUSIVE", "forecast"
        evidence.append(f"only {len(organic)} unexplained elevated days; {sustained_min_days} needed to call a shift")

    return _signal(cls, basis, baseline, recent_mean, threshold, adjusted, elevated, bulk, promo, stockouts, evidence)


def apply_promotions(forecast: list[float], promotions: list[PromoWindow]) -> list[float]:
    """Uplift a baseline forecast on promotion days (forecasts here are not promo-aware; decision D13)."""
    out = []
    for day, units in enumerate(forecast):
        uplift = max((p.uplift_pct for p in promotions if p.covers(day)), default=0.0)
        out.append(round(units * (1 + uplift / 100), 2))
    return out


def run_rate_forecast(daily: float, days: int) -> list[float]:
    return [round(daily, 2)] * days


def _mean(rows: list[SalesDay]) -> float:
    return round(sum(s.units for s in rows) / len(rows), 2) if rows else 0.0


def _signal(cls, basis, baseline, recent, threshold, adjusted, elevated, bulk, promo, stockouts, evidence):
    return DemandSignal(
        classification=cls, suggested_basis=basis, baseline_daily=round(baseline, 2), recent_daily=round(recent, 2),
        threshold=round(threshold, 2), adjusted_daily=round(adjusted, 2),
        elevated_days=[s.day for s in elevated], bulk_days=[s.day for s in bulk], promo_days=[s.day for s in promo],
        stockout_days=[s.day for s in stockouts], evidence=evidence,
    )
