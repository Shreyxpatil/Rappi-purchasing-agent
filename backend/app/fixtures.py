"""Scenario fixture schema.

A fixture is one self-contained, reproducible purchasing situation: the data to seed,
the trigger handed to the agent, how the mock supplier will behave, how a human will
answer approval requests, and the hand-computed expected result.

Dates are written as day offsets from `as_of` (day 0 = today, -1 = yesterday) so the
arithmetic in `rationale` can be checked without a calendar.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import REPO_ROOT

SCENARIOS_DIR = REPO_ROOT / "evals" / "scenarios"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- seed sections


class InventorySeed(_Strict):
    node: str
    sku: str
    on_hand: int
    reserved: int = 0
    updated_hours_ago: float = 1


class DailySeries(_Strict):
    """Either an explicit list of daily values or `flat` repeated `days` times."""

    daily: list[float] | None = None
    flat: float | None = None
    days: int | None = None

    @model_validator(mode="after")
    def _one_form(self) -> "DailySeries":
        if (self.daily is None) == (self.flat is None):
            raise ValueError("give exactly one of `daily` or `flat`")
        if self.flat is not None and not self.days:
            raise ValueError("`flat` needs `days`")
        return self

    def values(self) -> list[float]:
        return list(self.daily) if self.daily is not None else [self.flat] * self.days  # type: ignore[list-item]


class ForecastSeed(DailySeries):
    """Forecast for day 0 onwards."""

    node: str
    sku: str
    generated_hours_ago: float = 6


class SalesSeed(DailySeries):
    """Sales history ending yesterday: the last value is day -1."""

    node: str
    sku: str
    orders: list[int] | None = None
    max_order_units: list[int] | None = None
    stockout_days: list[int] = Field(default_factory=list)  # negative offsets


class POLineSeed(_Strict):
    sku: str
    qty: int
    qty_confirmed: int | None = None
    unit_cost: float | None = None  # defaults to the supplier's current terms
    eta_day: int


class POEventSeed(_Strict):
    type: str
    source: str = "supplier"
    hours_ago: float = 1
    payload: dict[str, Any] = Field(default_factory=dict)


class PurchaseOrderSeed(_Strict):
    id: str
    node: str
    supplier: str
    status: str
    created_days_ago: int = 1
    lines: list[POLineSeed]
    events: list[POEventSeed] = Field(default_factory=list)


class BudgetSeed(_Strict):
    category: str
    currency: str
    period: str | None = None  # defaults to the as_of month
    limit: float
    committed: float
    spent: float


class StorageSeed(_Strict):
    node: str
    zone: str
    capacity: int
    used: int


class PromotionSeed(_Strict):
    id: str
    node: str
    sku: str
    start_day: int
    end_day: int
    uplift_pct: float
    description: str


class RecommendationSeed(_Strict):
    id: str
    node: str
    sku: str
    supplier: str
    qty: int


class SupplierOverride(_Strict):
    id: str
    active: bool | None = None
    reliability_score: float | None = None


class SupplierTermsSeed(_Strict):
    """Upsert of supplier terms: overrides catalog fields, or adds a new supplier/SKU pair."""

    supplier: str
    sku: str
    unit_cost: float | None = None
    moq: int | None = None
    case_pack: int | None = None
    lead_time_days: int | None = None
    hist_fill_rate: float | None = None
    is_primary: bool | None = None
    notes: str | None = None
    updated_hours_ago: float | None = None


class Seed(_Strict):
    inventory: list[InventorySeed] = Field(default_factory=list)
    forecasts: list[ForecastSeed] = Field(default_factory=list)
    sales: list[SalesSeed] = Field(default_factory=list)
    purchase_orders: list[PurchaseOrderSeed] = Field(default_factory=list)
    budgets: list[BudgetSeed] = Field(default_factory=list)
    storage: list[StorageSeed] = Field(default_factory=list)
    promotions: list[PromotionSeed] = Field(default_factory=list)
    recommendations: list[RecommendationSeed] = Field(default_factory=list)
    suppliers: list[SupplierOverride] = Field(default_factory=list)
    supplier_terms: list[SupplierTermsSeed] = Field(default_factory=list)


# --------------------------------------------------------------------------- trigger, behaviour, expectations


class Trigger(_Strict):
    """What starts the agent run."""

    type: Literal["recommendation_review", "supplier_response", "demand_alert"]
    node: str
    sku: str
    recommendation_id: str | None = None
    po_id: str | None = None
    message: str = ""


class SupplierResponse(_Strict):
    """One scripted answer from the mock supplier to a submitted PO."""

    type: Literal["CONFIRMED", "PARTIAL", "REJECTED", "PRICE_CHANGE", "DELAYED"]
    qty: int | None = None  # PARTIAL: quantity the supplier can ship
    pct: float | None = None  # PRICE_CHANGE: new price vs ordered, in percent
    days: int | None = None  # DELAYED: days added to the arrival date
    message: str = ""


class ExpectedPO(_Strict):
    id: str | None = None  # set when the PO already exists in the seed (e.g. acknowledged partial)
    supplier: str
    sku: str
    qty_min: int
    qty_max: int
    status: str


class Expected(_Strict):
    outcome: Literal["ACCEPT", "MODIFY", "REJECT", "INVESTIGATE"]
    qty_min: int
    qty_max: int
    final_status: Literal["COMPLETED", "ESCALATED", "AWAITING_APPROVAL"]
    # PURCHASE | TRANSFER | ACCEPT_PARTIAL | NO_ACTION | INVESTIGATE | ESCALATE
    option_kind: str
    # POs created or changed by the run, as they must look at the end (untouched seed POs are not listed).
    final_pos: list[ExpectedPO] = Field(default_factory=list)
    transfers: list[dict[str, Any]] = Field(default_factory=list)
    approval_reasons: list[str] = Field(default_factory=list)  # policy reason codes that must be requested
    escalation: bool = False
    min_replans: int = 0
    required_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    # No action tool call in the trace may carry a quantity above this (prompt-injection guard).
    max_action_qty: int | None = None
    # Constraint codes the *recommendation* must be found to violate (Scenario 1/4).
    recommendation_violations: list[str] = Field(default_factory=list)
    # Hand-computed intermediate values; engine tests assert against these.
    computed: dict[str, Any] = Field(default_factory=dict)


class ScenarioFixture(_Strict):
    id: str
    scenario: Literal["S1", "S2", "S3", "S4", "X"]
    title: str
    description: str
    as_of: datetime
    seed: Seed
    trigger: Trigger
    supplier_behaviour: dict[str, list[SupplierResponse]] = Field(default_factory=dict)
    approval_responses: list[Literal["APPROVE", "REJECT"]] = Field(default_factory=list)
    expected: Expected
    rationale: list[str]


def load_fixture(path_or_id: str | Path) -> ScenarioFixture:
    path = Path(path_or_id)
    if not path.suffix:
        path = SCENARIOS_DIR / f"{path_or_id}.json"
    return ScenarioFixture.model_validate(json.loads(path.read_text(encoding="utf-8")))


def list_fixtures() -> list[ScenarioFixture]:
    return [load_fixture(p) for p in sorted(SCENARIOS_DIR.glob("*.json"))]
