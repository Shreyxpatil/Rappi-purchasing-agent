# Explanation rubric (LLM judge)

The judge scores **only the buyer-facing explanation**. Decisions, constraints, actions, validation and recovery
are graded deterministically by `evals/graders.py`; the judge never sees or changes those grades.

Score each criterion from 1 (poor) to 5 (excellent):

| Criterion | 5 means |
|---|---|
| `decision_clarity` | States plainly what was decided (accept / modify / reject / investigate), the quantity and the supplier or source |
| `reasons` | Names the factors that drove it: the own requirement versus the recommendation, and the binding constraint or evidence |
| `risk_and_handoff` | Mentions any approval, residual risk, escalation or missing information when the decision carries one; does not invent one when it does not |
| `faithfulness` | Every claim matches the structured decision provided; nothing is invented or contradicted |
| `concision` | A buyer can act on it in under 30 seconds; no filler, no internal jargon |

The judge returns JSON: `{"scores": {criterion: 1-5}, "comment": "one sentence"}`.
