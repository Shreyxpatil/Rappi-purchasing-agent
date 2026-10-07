"""Engine input/output models. Frozen Pydantic models so tools can return them as JSON."""

from pydantic import BaseModel, ConfigDict


class EngineInputError(ValueError):
    """Inputs are missing or inconsistent. `code` is machine-readable (e.g. FORECAST_TOO_SHORT)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Receipt(Frozen):
    """Units landing at the node at the start of `day` (offset from today)."""

    day: int
    qty: int
    source: str = ""  # e.g. "PO-1001", "TRANSFER CDMX-02", "new"


class NetRequirementInput(Frozen):
    forecast: list[float]  # daily demand basis starting today (day 0)
    on_hand: int
    reserved: int
    inbound: list[Receipt]
    lead_time_days: int
    review_period_days: int
    safety_days: float
    moq: int
    case_pack: int


class NetRequirement(Frozen):
    horizon: int
    demand: float
    avg_daily: float
    safety_stock: float
    available: int
    inbound: int
    inbound_counted: list[Receipt]
    inbound_excluded: list[Receipt]  # arrives after the horizon: does not cover this cycle
    net: float
    order_qty: int
    rounding: list[str]  # human-readable steps, e.g. "raised 140 to MOQ 240"


class Projection(Frozen):
    """Day-by-day stock level. Receipts land at the start of their day; that day's demand is then subtracted."""

    start_levels: list[float]  # after the day's receipts, before its demand
    end_levels: list[float]
    stockout_day: int | None  # first day whose end level is below zero
    unmet_units: float  # demand that could not be served within the window
    min_level: float
