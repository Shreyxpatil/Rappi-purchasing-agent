"""Inventory projection: what stock looks like day by day under a given set of receipts."""

from app.engine.types import EngineInputError, Projection, Receipt


def project_inventory(available: float, forecast: list[float], receipts: list[Receipt], days: int) -> Projection:
    """Project `days` days ahead from `available` units, adding `receipts` (existing or hypothetical POs,
    transfers) and subtracting the daily forecast. Levels may go negative: the negative part is unmet demand."""
    if len(forecast) < days:
        raise EngineInputError("FORECAST_TOO_SHORT", f"need {days} days of forecast, got {len(forecast)}")

    by_day: dict[int, int] = {}
    for r in receipts:
        if r.day < 0:
            raise EngineInputError("RECEIPT_IN_PAST", f"receipt {r.source or ''} on day {r.day}")
        by_day[r.day] = by_day.get(r.day, 0) + r.qty

    level = float(available)
    start_levels, end_levels = [], []
    for d in range(days):
        level += by_day.get(d, 0)
        start_levels.append(round(level, 2))
        level -= forecast[d]
        end_levels.append(round(level, 2))

    stockout_day = next((d for d, lvl in enumerate(end_levels) if lvl < 0), None)
    min_level = min(end_levels) if end_levels else float(available)
    return Projection(
        start_levels=start_levels,
        end_levels=end_levels,
        stockout_day=stockout_day,
        unmet_units=round(max(0.0, -min_level), 2),
        min_level=min_level,
    )


def cover_days(units: float, avg_daily: float) -> float:
    """Days of demand `units` would last at `avg_daily`."""
    return round(units / avg_daily, 2) if avg_daily > 0 else float("inf")
