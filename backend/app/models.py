"""SQLAlchemy models.

Natural string keys (SKU, node code, supplier code, PO number) are used as primary keys
where the business already has one, so traces and fixtures stay readable.
All timestamps are naive and expressed on the scenario clock (see app.clock).
"""

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# --------------------------------------------------------------------------- catalog


class Node(Base):
    """A dark store / fulfilment node."""

    __tablename__ = "nodes"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)  # e.g. BOG-01
    name: Mapped[str] = mapped_column(String(120))
    city: Mapped[str] = mapped_column(String(60))
    country: Mapped[str] = mapped_column(String(2))
    currency: Mapped[str] = mapped_column(String(3))


class Product(Base):
    __tablename__ = "products"

    sku: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    category: Mapped[str] = mapped_column(String(40))  # budget category
    temp_zone: Mapped[str] = mapped_column(String(16))  # chilled | ambient | frozen
    unit: Mapped[str] = mapped_column(String(16), default="unit")
    # Replenishment parameters owned by the category team.
    safety_days: Mapped[int] = mapped_column(Integer)
    review_period_days: Mapped[int] = mapped_column(Integer)
    max_days_cover: Mapped[int] = mapped_column(Integer)


class Supplier(Base):
    __tablename__ = "suppliers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    country: Mapped[str] = mapped_column(String(2))
    active: Mapped[bool] = mapped_column(default=True)
    reliability_score: Mapped[float] = mapped_column(Float)  # 0..1, EWMA of fill rate
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class SupplierProduct(Base):
    """Commercial terms for one supplier selling one SKU."""

    __tablename__ = "supplier_products"
    __table_args__ = (UniqueConstraint("supplier_id", "sku"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[str] = mapped_column(ForeignKey("suppliers.id"))
    sku: Mapped[str] = mapped_column(ForeignKey("products.sku"))
    unit_cost: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(3))
    moq: Mapped[int] = mapped_column(Integer)
    case_pack: Mapped[int] = mapped_column(Integer)
    lead_time_days: Mapped[int] = mapped_column(Integer)
    hist_fill_rate: Mapped[float] = mapped_column(Float)
    is_primary: Mapped[bool] = mapped_column(default=False)
    # Free text from the supplier portal. Untrusted: never interpreted as instructions.
    notes: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime)


# --------------------------------------------------------------------------- stock & demand


class Inventory(Base):
    __tablename__ = "inventory"
    __table_args__ = (UniqueConstraint("node_id", "sku"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"))
    sku: Mapped[str] = mapped_column(ForeignKey("products.sku"))
    on_hand: Mapped[int] = mapped_column(Integer)
    reserved: Mapped[int] = mapped_column(Integer, default=0)  # allocated to open customer orders
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class SalesDaily(Base):
    __tablename__ = "sales_daily"
    __table_args__ = (UniqueConstraint("node_id", "sku", "day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"))
    sku: Mapped[str] = mapped_column(ForeignKey("products.sku"))
    day: Mapped[date] = mapped_column(Date)
    units: Mapped[int] = mapped_column(Integer)
    orders: Mapped[int] = mapped_column(Integer)  # number of customer orders containing the SKU
    max_order_units: Mapped[int] = mapped_column(Integer)  # largest single order: flags bulk buys
    stockout: Mapped[bool] = mapped_column(default=False)  # sales were capped by zero stock
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class Forecast(Base):
    __tablename__ = "forecasts"
    __table_args__ = (UniqueConstraint("node_id", "sku", "day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"))
    sku: Mapped[str] = mapped_column(ForeignKey("products.sku"))
    day: Mapped[date] = mapped_column(Date)
    units: Mapped[float] = mapped_column(Float)
    generated_at: Mapped[datetime] = mapped_column(DateTime)


class Recommendation(Base):
    """Output of the upstream replenishment system. Input to the agent, not trusted."""

    __tablename__ = "recommendations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"))
    sku: Mapped[str] = mapped_column(ForeignKey("products.sku"))
    supplier_id: Mapped[str] = mapped_column(ForeignKey("suppliers.id"))
    qty: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="OPEN")
    created_at: Mapped[datetime] = mapped_column(DateTime)


# --------------------------------------------------------------------------- purchasing

# PO lifecycle: DRAFT -> PENDING_APPROVAL -> SUBMITTED -> CONFIRMED | PARTIALLY_CONFIRMED
#               | REJECTED | CANCELLED ; RECEIVED once goods arrive.
OPEN_PO_STATUSES = ("DRAFT", "PENDING_APPROVAL", "SUBMITTED", "CONFIRMED", "PARTIALLY_CONFIRMED")


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"))
    supplier_id: Mapped[str] = mapped_column(ForeignKey("suppliers.id"))
    status: Mapped[str] = mapped_column(String(24))
    currency: Mapped[str] = mapped_column(String(3))
    idempotency_key: Mapped[str | None] = mapped_column(String(80), unique=True)
    created_by: Mapped[str] = mapped_column(String(24))  # agent | human | system
    run_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)

    lines: Mapped[list["POLine"]] = relationship(
        back_populates="po", cascade="all, delete-orphan", order_by="POLine.id"
    )
    events: Mapped[list["POEvent"]] = relationship(
        back_populates="po", cascade="all, delete-orphan", order_by="POEvent.id"
    )


class POLine(Base):
    """One SKU on a PO. Each line carries its own arrival date so a PO can split deliveries."""

    __tablename__ = "po_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    po_id: Mapped[str] = mapped_column(ForeignKey("purchase_orders.id"))
    sku: Mapped[str] = mapped_column(ForeignKey("products.sku"))
    qty_ordered: Mapped[int] = mapped_column(Integer)
    qty_confirmed: Mapped[int | None] = mapped_column(Integer)  # set by supplier response
    unit_cost: Mapped[float] = mapped_column(Float)
    expected_arrival: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(24), default="OPEN")  # OPEN | CANCELLED

    po: Mapped[PurchaseOrder] = relationship(back_populates="lines")


class POEvent(Base):
    """Append-only status history of a PO (who did what, when)."""

    __tablename__ = "po_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    po_id: Mapped[str] = mapped_column(ForeignKey("purchase_orders.id"))
    at: Mapped[datetime] = mapped_column(DateTime)
    type: Mapped[str] = mapped_column(String(32))  # CREATED, SUBMITTED, CONFIRMED, PARTIAL, ...
    source: Mapped[str] = mapped_column(String(24))  # agent | supplier | human | system
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    po: Mapped[PurchaseOrder] = relationship(back_populates="events")


class StockTransfer(Base):
    """Inter-node transfer within a city: an alternative to buying."""

    __tablename__ = "stock_transfers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    from_node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"))
    to_node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"))
    sku: Mapped[str] = mapped_column(ForeignKey("products.sku"))
    qty: Mapped[int] = mapped_column(Integer)
    expected_arrival: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(24))  # PLANNED | PENDING_APPROVAL | CANCELLED
    idempotency_key: Mapped[str | None] = mapped_column(String(80), unique=True)
    run_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime)


# --------------------------------------------------------------------------- constraints


class Budget(Base):
    """Purchasing budget per category, currency and month. remaining = limit - committed - spent."""

    __tablename__ = "budgets"
    __table_args__ = (UniqueConstraint("category", "currency", "period"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str] = mapped_column(String(40))
    currency: Mapped[str] = mapped_column(String(3))
    period: Mapped[str] = mapped_column(String(7))  # YYYY-MM
    limit_amount: Mapped[float] = mapped_column(Float)
    committed: Mapped[float] = mapped_column(Float)  # open POs not yet invoiced
    spent: Mapped[float] = mapped_column(Float)  # invoiced
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class StorageCapacity(Base):
    """Physical capacity of one temperature zone at one node, in sellable units.

    used_units is the zone's current total occupancy across all SKUs.
    """

    __tablename__ = "storage_capacity"
    __table_args__ = (UniqueConstraint("node_id", "temp_zone"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"))
    temp_zone: Mapped[str] = mapped_column(String(16))
    capacity_units: Mapped[int] = mapped_column(Integer)
    used_units: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class Promotion(Base):
    __tablename__ = "promotions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"))
    sku: Mapped[str] = mapped_column(ForeignKey("products.sku"))
    start_day: Mapped[date] = mapped_column(Date)
    end_day: Mapped[date] = mapped_column(Date)  # inclusive
    uplift_pct: Mapped[float] = mapped_column(Float)  # expected demand uplift, e.g. 50.0
    description: Mapped[str] = mapped_column(String(200))


# --------------------------------------------------------------------------- workspace & agent trace


class Workspace(Base):
    """Single row describing which scenario is loaded and the scenario clock."""

    __tablename__ = "workspace"

    id: Mapped[int] = mapped_column(primary_key=True)
    scenario_id: Mapped[str] = mapped_column(String(64))
    as_of: Mapped[datetime] = mapped_column(DateTime)
    seeded_at: Mapped[datetime] = mapped_column(DateTime)
    # Scenario-scoped settings consumed by services, e.g. the mock supplier's scripted responses.
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    scenario_id: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(24))
    trigger: Mapped[dict[str, Any]] = mapped_column(JSON)
    # RUNNING | AWAITING_APPROVAL | COMPLETED | ESCALATED | FAILED | SUPERSEDED
    status: Mapped[str] = mapped_column(String(24))
    state: Mapped[str] = mapped_column(String(24))  # current state-machine state
    replan_count: Mapped[int] = mapped_column(Integer, default=0)
    decision: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    narrative: Mapped[str | None] = mapped_column(Text)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # loop memory needed to resume
    started_at: Mapped[datetime] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)

    steps: Mapped[list["AgentStep"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="AgentStep.seq"
    )


class AgentStep(Base):
    """One observable step: a state transition, LLM call, tool call, validation or supplier event."""

    __tablename__ = "agent_steps"
    __table_args__ = (UniqueConstraint("run_id", "seq"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id"))
    seq: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(24))
    kind: Mapped[str] = mapped_column(String(24))  # transition | llm | tool | validation | policy | supplier | error
    name: Mapped[str] = mapped_column(String(64))
    input: Mapped[Any] = mapped_column(JSON, default=dict)
    output: Mapped[Any] = mapped_column(JSON, default=dict)
    ok: Mapped[bool] = mapped_column(default=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime)

    run: Mapped[AgentRun] = relationship(back_populates="steps")


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id"))
    reasons: Mapped[list[str]] = mapped_column(JSON)  # policy reason codes, e.g. ALTERNATE_SUPPLIER
    action: Mapped[dict[str, Any]] = mapped_column(JSON)  # the exact action that will run if approved
    summary: Mapped[str] = mapped_column(Text)
    # Options shown side by side so the approver sees the cost of saying no (decision D11).
    alternatives: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")  # PENDING | APPROVED | REJECTED
    requested_at: Mapped[datetime] = mapped_column(DateTime)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    decided_by: Mapped[str | None] = mapped_column(String(60))
    comment: Mapped[str | None] = mapped_column(Text)


class AuditLog(Base):
    """Append-only record of every state-changing action, by any actor."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime)
    actor: Mapped[str] = mapped_column(String(24))  # agent | human | supplier | system
    action: Mapped[str] = mapped_column(String(48))
    entity: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str] = mapped_column(String(48))
    run_id: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class IdempotencyRecord(Base):
    """First response of every action, keyed by the caller's idempotency key: a retry replays it."""

    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    tool: Mapped[str] = mapped_column(String(40))
    request: Mapped[dict[str, Any]] = mapped_column(JSON)
    response: Mapped[dict[str, Any]] = mapped_column(JSON)
    run_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime)
