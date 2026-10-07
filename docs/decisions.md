# Design decisions

Each entry: the decision, the alternatives considered, and why. Assumptions made where the brief is ambiguous
are recorded here too.

## D1. SQLite + SQLAlchemy, natural string keys

- **Decision:** One SQLite database through SQLAlchemy 2.x. Business identifiers (SKU, `BOG-01`, `SUP-ALQ`, `PO-1001`)
  are the primary keys.
- **Alternatives:** Postgres (more realistic, but needs another container and adds nothing for a single-user demo);
  surrogate integer keys everywhere.
- **Why:** Zero setup, and the file can be inspected with any SQLite browser. Natural keys keep agent traces, fixtures
  and eval reports readable: a trace says `PO-1001`, not `purchase_order_id=7`. Swapping to Postgres only changes
  `DATABASE_URL`.

## D2. Arrival date lives on the PO line, not the PO header

- **Decision:** `po_lines.expected_arrival`.
- **Alternatives:** One arrival date per PO.
- **Why:** Split deliveries (Scenario 4, storage-bound) must stay on a single PO, so the supplier's MOQ applies to
  the PO total while each delivery fits the storage available on its own arrival day.

## D3. Supplier notes are stored, but marked untrusted

- **Decision:** `supplier_products.notes` holds free text from the supplier portal. It is never treated as instructions.
- **Alternatives:** Not storing free text at all.
- **Why:** Real supplier channels carry free text, and that text is a prompt-injection vector. The eval suite has a
  case for it. Tools return such text in an `untrusted_text` field.

## D4. Storage is checked per temperature zone, assuming other SKUs stay flat

- **Decision:** `storage_capacity` stores `capacity_units` and the zone's current `used_units` per node and zone.
  Free space for a SKU on its arrival day is computed as:

  ```
  free_for_sku_at_arrival = capacity − (used_units − sku_on_hand_now) − sku_projected_level_at_arrival
  ```

- **Alternatives:** Projecting every SKU in the zone (needs every SKU's forecast and open POs); per-SKU slot
  allocations.
- **Why:** Hand-computable, and it captures the constraint that matters: the zone is shared and chilled space is scarce.
  The assumption that other SKUs' occupancy stays constant until arrival is stated in every storage factor the agent reports.

## D5. Budgets are per category × currency × month

- **Decision:** `remaining = limit − committed − spent`. A PO counts against the month of its creation date.
- **Why:** This matches how category buyers usually hold budgets. Currency is part of the key because Bogotá (COP)
  and CDMX (MXN) nodes are never mixed.

## D6. Scenario clock and relative-day fixtures

- **Decision:** Each scenario fixes `as_of`, and every fixture date is a day offset from it (0 = today, −1 = yesterday,
  `eta_day: 2`). No business logic reads the wall clock.
- **Alternatives:** Absolute dates; the real current time.
- **Why:** Freshness checks ("inventory updated 72 h ago"), need dates and arrival days stay reproducible run after run,
  and the arithmetic in each fixture's `rationale` can be checked by hand without a calendar.

## D7. Re-seeding replaces domain data and keeps the run history

- **Decision:** Launching a scenario wipes and rebuilds the domain tables (catalog, stock, purchasing, constraints).
  `agent_runs`, `agent_steps`, `approvals` and `audit_log` survive.
- **Alternatives:** One database per run; a scenario id column on every domain row.
- **Why:** This is the simplest model that still keeps every past trace inspectable in the UI. The limitation:
  a run paused for approval can't be resumed after another scenario is loaded, so it is marked `SUPERSEDED`.
  Evals avoid this entirely by using a fresh in-memory database per run.
