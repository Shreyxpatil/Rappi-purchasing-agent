"""The narrative is written FROM the structured decision, and checked against it.

Every number in the model's explanation must appear in the decision or the execution facts.
Identifiers (PO-1002, BOG-02, COCA-1.5L) are removed first, and small counts (0..10) are allowed,
so "two options" or "day 4" do not trip the check. One retry with the offending numbers listed,
then a deterministic template: a wrong number never reaches the buyer.
"""

import json
import re
from typing import Any

_ID = re.compile(r"\b[A-Za-z]+(?:-[A-Za-z0-9.]+)+\b")
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
SMALL_COUNT = 10


def numbers_in(text: str) -> set[float]:
    text = _ID.sub(" ", text)
    out = set()
    for raw in _NUM.findall(text):
        try:
            out.add(float(raw.replace(",", "")))
        except ValueError:
            continue
    return out


def ungrounded_numbers(narrative: str, *sources: Any) -> list[float]:
    """Numbers in the narrative that appear in none of the sources (exact or to 2 decimals)."""
    allowed: set[float] = set()
    for src in sources:
        text = src if isinstance(src, str) else json.dumps(src, default=str)
        allowed |= numbers_in(text)
        allowed |= numbers_in(_ID.sub(lambda m: m.group(0).replace("-", " "), text))
    allowed_rounded = {round(a, 2) for a in allowed} | {round(a) for a in allowed}
    return sorted(n for n in numbers_in(narrative)
                  if n > SMALL_COUNT and round(n, 2) not in allowed_rounded and n not in allowed_rounded)


def template_narrative(decision: dict[str, Any], facts: dict[str, Any]) -> str:
    """Deterministic fallback, assembled only from the decision object."""
    lines = [f"{decision['outcome']}: {decision['quantity']} units"
             + (f" via {decision['option_id']}" if decision.get("option_id") else "") + "."]
    lines += [f"- {f['name']}: {f['value']} ({f['effect']})" for f in decision.get("factors", [])]
    if decision.get("residual_risk"):
        lines.append(f"- residual risk: {json.dumps(decision['residual_risk'])}")
    if decision.get("information_needed"):
        lines.append("- information needed: " + "; ".join(decision["information_needed"]))
    for po in facts.get("purchase_orders", []):
        lines.append(f"- {po['po_id']}: {po['qty']} units from {po['supplier']}, {po['status']}")
    for t in facts.get("transfers", []):
        lines.append(f"- {t['id']}: transfer of {t['qty']} from {t['from']}")
    return "\n".join(lines)
