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


class Delivery(Frozen):
    day: int
    qty: int


class StorageInfo(Frozen):
    """One temperature zone at one node. used_units includes this SKU's own on-hand stock."""

    capacity_units: int
    used_units: int
    sku_on_hand: int

    @property
    def sku_capacity(self) -> int:
        """Most units of this SKU the zone can hold, assuming other SKUs stay flat (decision D4)."""
        return self.capacity_units - (self.used_units - self.sku_on_hand)


class PlanContext(Frozen):
    """Everything needed to judge a purchase plan for one SKU at one node."""

    available: int
    forecast: list[float]  # demand basis from day 0
    horizon: int
    avg_daily: float
    safety_stock: float
    existing_receipts: list[Receipt]
    storage: StorageInfo | None
    budget_remaining: float | None
    max_days_cover: int


class Violation(Frozen):
    code: str  # e.g. STORAGE_EXCEEDED
    hard: bool  # hard = may not be executed as is
    overridable: bool = False  # hard, but a human may approve past it (budget)
    detail: dict[str, float | int | str | None] = {}


class Caps(Frozen):
    """Largest quantity (case-pack multiple) a single delivery on `day` can carry under each constraint."""

    day: int
    level_before_arrival: float
    storage_free: float | None
    max_qty_storage: int | None
    max_qty_budget: int | None
    max_qty_cover: int


class ConstraintReport(Frozen):
    deliveries: list[Delivery]
    total_qty: int
    value: float
    violations: list[Violation]
    projection: Projection  # over the horizon, with this plan's deliveries added
    cover_at_arrival: list[float]  # one per delivery

    @property
    def blocked(self) -> bool:
        """Violates a hard constraint nobody can override."""
        return any(v.hard and not v.overridable for v in self.violations)

    @property
    def needs_override(self) -> bool:
        return any(v.hard and v.overridable for v in self.violations)
