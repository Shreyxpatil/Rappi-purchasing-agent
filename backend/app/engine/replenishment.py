"""Net requirement: how much this node needs to order now to cover lead time + review period."""

import math

from app.engine.types import EngineInputError, NetRequirement, NetRequirementInput


def ceil_to(x: float, multiple: int) -> int:
    """Smallest multiple of `multiple` that is >= x (x <= 0 gives 0)."""
    if x <= 0:
        return 0
    return multiple * math.ceil(round(x / multiple, 9))


def floor_to(x: float, multiple: int) -> int:
    """Largest multiple of `multiple` that is <= x (never negative)."""
    if x <= 0:
        return 0
    return multiple * math.floor(round(x / multiple, 9))


def calculate_net_requirement(inp: NetRequirementInput) -> NetRequirement:
    horizon = inp.lead_time_days + inp.review_period_days
    if horizon <= 0:
        raise EngineInputError("INVALID_HORIZON", f"lead time + review period must be > 0, got {horizon}")
    if len(inp.forecast) < horizon:
        raise EngineInputError(
            "FORECAST_TOO_SHORT", f"need {horizon} days of forecast, got {len(inp.forecast)}"
        )
    if inp.moq < 0 or inp.case_pack <= 0:
        raise EngineInputError("INVALID_TERMS", f"moq={inp.moq} case_pack={inp.case_pack}")

    demand = sum(inp.forecast[:horizon])
    avg_daily = demand / horizon
    safety_stock = inp.safety_days * avg_daily
    available = inp.on_hand - inp.reserved
    counted = [r for r in inp.inbound if 0 <= r.day < horizon]
    excluded = [r for r in inp.inbound if not 0 <= r.day < horizon]
    inbound = sum(r.qty for r in counted)
    net = demand + safety_stock - available - inbound

    rounding: list[str] = []
    order_qty = 0
    if net > 0:
        qty = net
        if qty < inp.moq:
            rounding.append(f"raised {_fmt(qty)} to MOQ {inp.moq}")
            qty = inp.moq
        order_qty = ceil_to(qty, inp.case_pack)
        if order_qty != qty:
            rounding.append(f"rounded {_fmt(qty)} up to case pack {inp.case_pack}: {order_qty}")

    return NetRequirement(
        horizon=horizon,
        demand=round(demand, 2),
        avg_daily=round(avg_daily, 2),
        safety_stock=round(safety_stock, 2),
        available=available,
        inbound=inbound,
        inbound_counted=counted,
        inbound_excluded=excluded,
        net=round(net, 2),
        order_qty=order_qty,
        rounding=rounding,
    )


def _fmt(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:.2f}"
