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
