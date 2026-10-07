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


class SupplierTerms(Frozen):
    supplier_id: str
    unit_cost: float
    moq: int
    case_pack: int
    lead_time_days: int
    active: bool = True
    is_primary: bool = False
    reliability: float = 1.0


class PODraft(Frozen):
    """A proposed PO (or change to one) for a single SKU, possibly split into several deliveries."""

    supplier_id: str
    sku: str
    deliveries: list[Delivery]
    unit_cost: float
    po_id: str | None = None  # set when changing an existing PO
    base_qty: int = 0  # quantity already on the existing line; MOQ applies to the total


class OpenPORef(Frozen):
    po_id: str
    supplier_id: str
    sku: str
    status: str


class ValidationResult(Frozen):
    ok: bool
    violations: list[Violation]
    overridden: list[str]  # hard codes a human explicitly approved past (e.g. BUDGET_EXCEEDED)
    report: ConstraintReport


class SalesDay(Frozen):
    day: int  # negative offset: -1 = yesterday
    units: int
    orders: int
    max_order_units: int
    stockout: bool = False


class PromoWindow(Frozen):
    id: str
    start_day: int
    end_day: int  # inclusive
    uplift_pct: float

    def covers(self, day: int) -> bool:
        return self.start_day <= day <= self.end_day


class DemandSignal(Frozen):
    # NO_SHIFT | SUSTAINED_SHIFT | ONE_OFF_OUTLIER | PROMO | STOCKOUT_CENSORED | INCONCLUSIVE
    classification: str
    suggested_basis: str  # forecast | recent_run_rate | promo_adjusted
    baseline_daily: float  # mean before the recent window, stockout days excluded
    recent_daily: float  # raw mean of the recent window
    threshold: float  # a recent day above this is "elevated"
    adjusted_daily: float  # run-rate with explained one-offs removed
    elevated_days: list[int]
    bulk_days: list[int]  # elevated days explained by one large order
    promo_days: list[int]  # elevated days inside a promotion
    stockout_days: list[int]  # recent days whose sales were capped by zero stock
    evidence: list[str]
