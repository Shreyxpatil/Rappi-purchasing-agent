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


class Recommendation(Frozen):
    id: str
    supplier_id: str
    qty: int


class PartialFill(Frozen):
    """Scenario 2: a supplier confirmed less than was ordered."""

    po_id: str
    supplier_id: str
    qty_ordered: int
    qty_confirmed: int
    eta_day: int
    unit_cost: float
    backorder_eta_day: int | None = None  # when the supplier says the remainder could follow


class OpenLine(Frozen):
    """An open PO line that could still be increased."""

    po_id: str
    supplier_id: str
    qty: int
    eta_day: int


class TransferSource(Frozen):
    """Another node in the same city; its own requirement decides how much it can spare."""

    node_id: str
    requirement: NetRequirementInput


class OptionsInput(Frozen):
    forecast: list[float]  # demand basis chosen for this decision
    on_hand: int
    reserved: int
    receipts: list[Receipt]  # existing inbound (supplier-confirmed quantities)
    review_period_days: int
    safety_days: float
    max_days_cover: int
    storage: StorageInfo | None
    budget_remaining: float | None
    suppliers: list[SupplierTerms]  # every supplier that sells the SKU; the primary is the reference
    recommendation: Recommendation | None = None
    partial_fill: PartialFill | None = None
    open_lines: list[OpenLine] = []
    transfer_sources: list[TransferSource] = []
    transfer_lead_days: int = 1
    accept_tolerance_pct: float = 10.0
    excluded_suppliers: list[str] = []  # e.g. rejected the PO earlier in this run
    excluded_option_ids: list[str] = []  # e.g. refused by a human
    refused_overrides: list[str] = []  # e.g. BUDGET_EXCEEDED: drop every option that needs it


class Option(Frozen):
    id: str  # stable and readable, e.g. BUY:SUP-ALQ:240, TRANSFER:CDMX-02:80
    kind: str  # PURCHASE | TRANSFER | ACCEPT_PARTIAL | BACKORDER | NO_ACTION | ESCALATE
    label: str
    rank: int
    supplier_id: str | None = None
    po_id: str | None = None  # existing PO this option changes
    from_node: str | None = None
    is_recommendation: bool = False
    deliveries: list[Delivery] = []
    qty: int
    unit_cost: float
    value: float  # new spend this option commits
    violations: list[Violation]
    blocked: bool
    needs_override: bool
    is_alternate_supplier: bool
    price_variance_pct: float | None
    stockout_day: int | None
    unmet_units: float
    end_level: float
    safety_shortfall: float
    cover_at_arrival: float | None
    projection: list[float]  # end-of-day levels over the horizon
    tradeoffs: list[str]


class OptionSet(Frozen):
    reference_supplier_id: str
    reference: NetRequirement  # own requirement with the primary supplier's terms
    options: list[Option]  # ranked, best first
    recommendation_acceptable: bool
    recommendation_deviation_pct: float | None


class DataIssue(Frozen):
    code: str  # STALE_DATA | MISSING_DATA | CONFLICTING_DATA
    source: str  # inventory | forecast | supplier_terms | ...
    detail: dict[str, float | int | str | None] = {}


class InventorySensitivity(Frozen):
    units_sold_since_count: int
    as_recorded: NetRequirement
    adjusted: NetRequirement
    decision_flips: bool


class Factor(Frozen):
    name: str
    value: str
    effect: str  # how it moved the decision, e.g. "raises need", "blocks recommendation"


class ConstraintCheck(Frozen):
    name: str  # MOQ | CASE_PACK | STORAGE | BUDGET | COVER | SUPPLIER_ACTIVE | LEAD_TIME | DATA
    status: str  # pass | fail | overridden | warning | n/a
    detail: dict[str, float | int | str | None] = {}


class Decision(Frozen):
    """The structured decision. The narrative is written from this object, never the other way round."""

    outcome: str  # ACCEPT | MODIFY | REJECT | INVESTIGATE
    quantity: int
    option_id: str | None
    option_kind: str | None
    supplier_id: str | None
    deliveries: list[Delivery]
    value: float
    factors: list[Factor]
    constraints_checked: list[ConstraintCheck]
    recommendation_check: list[ConstraintCheck] | None
    confidence: str  # high | medium | low
    residual_risk: dict[str, float | int] | None
    information_needed: list[str]
    data_issues: list[DataIssue]
    alternatives: list[str]  # next-best option ids, for the approval card and the report
