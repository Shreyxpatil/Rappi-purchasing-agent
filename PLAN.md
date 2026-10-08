# PLAN — Rappi AI Purchasing Agent

## Context

Take-home brief: `docs/assignment.pdf` (kept locally, not committed). The goal is "a system capable of making,
executing, and validating purchasing decisions", not a chatbot. Evaluation focus: problem breakdown, tool design,
constraints and uncertainty, the **feedback loop**, validation, reliability, evaluation quality, and commit history.

**Deadline: within 24h.** Every push from P5 onward must be a submittable state (minimal docker-compose, Makefile and
a working README land at the end of P5). Scenarios 1 and 2 are built end to end first. Scenarios 3 and 4 reuse the same
engine and agent loop and are proven through eval cases. The P7 UI is time-boxed: functional and clean, with no extra polish
until S1/S2, evals and docs are done.

Environment: Python **3.12** locally (managed with `uv`, pinned in `.python-version`) and in Docker (`python:3.12-slim`);
Node 22; `docker compose` v2 (`docker-compose.yml` works with both CLIs). Pushes require a passing `gh auth status`.
If it fails, work stops until auth is fixed.

## Core design (how the principles become code)

### Who decides what

- **Engine = pure functions.** The `engine/` package never imports SQLAlchemy and never calls the LLM. It receives
  plain input objects (on_hand, forecast list, MOQ, …) and returns results. The tools layer reads the DB, builds those
  inputs, and calls the engine. So every formula can be unit-tested with hand-written numbers, needing no database,
  API key or mocks, and gives the same answer every time. The engine computes every number: net requirement,
  projections, constraint caps, options, validate_po and the outcome label.
- **LLM:** decides what to investigate and how to interpret anomalies (e.g. which demand basis to trust), picks one of the
  **engine-generated option_ids** or chooses INVESTIGATE instead, and writes the narrative. It never passes a free quantity:
  `propose_decision(option_id | investigate, reasons)`. Code derives ACCEPT/MODIFY/REJECT from the chosen option
  vs the recommendation using explicit rules (tolerance in policy.yaml).
- **Policy gate** (code, inside every act tool): decides auto / approval / escalate. It also enforces "decision binding": the
  act-tool args must match the approved decision (supplier, qty, node), which blocks prompt injection by construction.

### Replenishment math (`engine/replenishment.py`)

```text
horizon      = lead_time_days + review_period_days
demand       = Σ forecast_daily[d] for d in horizon       (or engine-adjusted basis if LLM adopts a detected shift)
safety_stock = safety_days × avg_forecast_daily            (simple, hand-computable; z·σ noted as a next step)
available    = on_hand − reserved
inbound      = Σ open-PO confirmed qty with ETA ≤ need date
net          = demand + safety_stock − available − inbound
qty          = ceil_to(case_pack, max(net, MOQ))  → cap by storage at arrival (floor to case pack)
             → cap by budget remaining (floor to case pack) → check days-of-cover ≤ max_days_cover
```

Constraint priority: hard (MOQ, storage, budget unless human override) > avoid stockout > avoid overstock > cost.

**Outcome rules:**

- REJECT if net ≤ 0.
- INVESTIGATE if data is stale (older than freshness_max_hours on the scenario clock), missing or conflicting, or the
  demand evidence is weak. The decision must list the information that would change it.
- ACCEPT if |rec − feasible_qty| ≤ tolerance and rec passes validate_po.
- MODIFY otherwise.

### State machine (`agent/loop.py`; each state exposes only its allowed tools)

```text
INTAKE → INVESTIGATE (LLM: read+compute; code enforces an evidence checklist before leaving)
 → DECIDE (LLM picks option; code builds Decision{outcome, qty, factors[name,value,effect], constraints_checked, confidence})
 → POLICY_GATE (code) ─approval→ AWAITING_APPROVAL (run paused; inbox resumes it) ─escalate→ REPORT
 → EXECUTE (LLM calls act tools; pre-action validate_po inside the tool)
 → VALIDATE (code: re-read PO, diff field by field vs intent)
 → AWAIT_SUPPLIER (mock supplier event) → VERIFY_OUTCOME (project_inventory with confirmed POs)
 → fail? REPLAN → DECIDE with reason codes (max 3, then ESCALATE with full context)
 → REPORT (LLM narrative from the Decision; code checks every number exists in the Decision, retries once, then falls back to a template)
```

Every state transition, LLM call and tool call (input, output, latency, tokens) is written to `agent_steps`; every action is written to `audit_log`.

### Feedback loop, 4 layers

1. **Pre-action:** `validate_po` returns reason codes such as `STORAGE_EXCEEDED{max:420}`, `BELOW_MOQ{moq:240}`,
   `BUDGET_EXCEEDED{remaining:…}`, `SUPPLIER_INACTIVE`, `LEAD_TIME_MISSES_NEED_DATE` and `DUPLICATE_OPEN_PO`.
2. **Post-action:** the PO is re-read from the DB and diffed field by field against the intended state.
3. **Supplier response:** a mock supplier answers CONFIRMED / PARTIAL(qty) / REJECTED / PRICE_CHANGE(pct) / DELAYED(days).
   Behaviour comes from the fixture (the UI can override it for the demo), is written as `po_events`, and is read back by the agent.
4. **Outcome:** project inventory with confirmed POs and check there is no stockout before the next cycle and cover ≤ max.

Fill outcomes update supplier reliability (EWMA).

### Data

- Seeded LatAm data across four nodes: BOG-01 Chapinero and BOG-02 Usaquén (COP), CDMX-01 Roma and CDMX-02 Polanco (MXN).
- Transfers happen only within a city.
- Real grocery SKUs (milk 1L, eggs 30u, Coca-Cola 1.5L, bananas/kg, etc.) across chilled, ambient and frozen zones.
- The scenario clock (`as_of`) is injected, so freshness and dates are deterministic.
- Every read tool returns `freshness{source_updated_at, age_hours, stale}`.
- Supplier free text comes back in an `untrusted_text` field.

**Workspace model:** domain tables are reseeded from the fixture on each scenario launch. Run, step, approval and audit
tables persist. Launching while a run awaits approval marks that run SUPERSEDED (documented limitation).
Evals use a fresh in-memory SQLite per run.

### LLM providers

- Selected with `LLM_PROVIDER` = gemini | openai_compat | scripted. Interface: `LLMClient.complete(messages, tools) → text | tool_calls`.
- **Gemini:** `google-genai` native function calling, temperature 0. The model comes from `GEMINI_MODEL`; the current Flash
  name is verified in Google docs at P4.
- **OpenAI-compatible:** one client, with a Groq preset in the README.
- **Scripted:** replays per-case tool-call scripts, with `$ref:` placeholders pointing to earlier outputs.
- **Rate limits:** a shared pacer (`LLM_MAX_RPM` token bucket) plus exponential backoff with jitter on 429/RESOURCE_EXHAUSTED/503.
- **Tool-call validation:** each tool call is checked against its Pydantic schema. The error goes back to the model once;
  a second failure fails the step, which escalates.

## Repo layout

```text
.python-version (3.12) ; pyproject.toml + uv.lock
backend/app/{config,db,models,clock,seed}.py
backend/app/engine/{replenishment,projection,demand,constraints,validation,options,decision,types}.py
backend/app/policy/{policy.yaml,gate.py}
backend/app/tools/{schemas,read,compute,act,registry}.py
backend/app/llm/{base,scripted,gemini,openai_compat,pacing}.py
backend/app/agent/{states,loop,prompts,narrative,persistence}.py
backend/app/supplier_mock/service.py
backend/app/api/{scenarios,runs,approvals,pos,evals}.py ; backend/app/main.py ; backend/tests/
evals/scenarios/*.json (seed + trigger + supplier_behaviour + expected + rationale + script)
evals/{run_evals.py,graders.py,judge.py,rubric.md,report.md}
frontend/ (Vite React TS Tailwind, Recharts)
docs/decisions.md ; README.md ; PLAN.md ; docker-compose.yml ; Makefile ; .env.example
```

## Evaluation

17 cases. Every expected value is hand-computed, with the arithmetic in the fixture's `rationale` field.

| Group | Cases |
|---|---|
| S1 | accept-correct · overstock→MODIFY · already-covered→REJECT · stale-inventory→INVESTIGATE |
| S2 | partial-250-enough (accept partial, no new PO) · partial-needs-alternate (alt supplier PO → approval) · alt-MOQ>gap (ranked: transfer BOG-02→BOG-01 vs. buy MOQ if cover ok vs. backorder) |
| S3 | real sustained surge→increase · one-off bulk-order outlier→no change · promo-explained uplift |
| S4 | budget-binding (ranked options, approval shows both side by side) · budget-override-rejected (fallback 342 executed, residual risk reported) · storage-binding (cap + split delivery) |
| Cross-cutting | supplier-rejects→replan · price-change>X%→approval · replans-exhausted→escalate · prompt-injection (10,000 ignored) |

**Grader negative controls:** known-bad scripted trajectories (blind-accept 800, skip validation, follow the injection)
must FAIL the right dimension. These run as pytest tests, which proves the graders discriminate.

**Dimensions**, all graded deterministically:

- **decision:** outcome type, and quantity within the acceptable range.
- **information:** required tools are present in the trace, in a sensible order.
- **constraints:** the final state passes validate_po; any violation is an automatic fail.
- **action:** correct DB state or approval request.
- **validation:** a validation step after every action.
- **recovery:** injected failures lead to a replan or escalation.

The prompt-injection case is graded twice: `model_resisted` (no action even attempts > 672 units) and
`system_safe` (no PO line > 672 ever persisted), to show defence in depth.

An LLM judge scores only narrative quality, against `rubric.md`.

**Runs:**

- Scripted runs are free: 3 runs per case, all cases (a determinism check).
- Real-LLM runs default to a representative subset, given free-tier quotas: s1_overstock, s2_partial_needs_alternate
  (recovery), x_prompt_injection and s4_budget_binding. Defaults: **3 runs per case on Groq** and **1 run per case on
  Gemini**, run sequentially and paced, with pass rates reported per provider. `--case` selects any case (repeatable)
  and `--runs` overrides the run count.
- **Every one of the 17 fixtures gets a scripted trajectory** (P6 adds the ones still missing), so the scripted eval
  covers the whole suite.

**Report** (`evals/report.md`): a case × dimension table, pass rate per dimension per provider, and an explicit
"ran on: scripted / gemini" column per case. It states plainly that scripted runs regression-test the system
(engine, gate, loop, graders) and real-LLM runs measure agent judgement.

## Phases and commit sequence

Conventional Commits; code and its tests in the same commit.

### P0 — Setup

Move the PDF to `docs/`, then `git init -b main`, add origin, and show `git config user.*` and `git remote -v`.

1. `chore: initialize repository with gitignore, env example and readme skeleton`. Contains only those 3 files.
   `.gitignore` covers `.env`, `*.db`, `*.sqlite`, `node_modules`, `__pycache__`, `dist`, `.venv`, `docs/assignment.pdf`, `CLAUDE.md` and `.claude/`.
2. `docs: add implementation plan`

### P1 — Schema, seed data, fixtures

- `build(backend): scaffold FastAPI service with uv, Python 3.12 and pytest`
- `feat(db): add domain models for catalog, inventory and purchasing`
- `feat(db): add budget, capacity, promotion and agent trace models`
- `feat(seed): load scenario fixtures into the database deterministically`
- `test(evals): add Scenario 1 and 2 fixtures with hand-computed expectations`
- `test(evals): add demand-shift, constraint and adversarial fixtures`

### P2 — Engine

- `feat(engine): compute net requirement with MOQ and case-pack rounding`
- `feat(engine): project inventory with hypothetical POs`
- `feat(engine): cap quantities by storage, budget and days of cover`
- `feat(engine): validate PO drafts with machine-readable reason codes`
- `feat(engine): classify demand shifts from sales history`
- `feat(engine): rank purchase options and derive decision outcome`

### P3 — Tools and policy

- `feat(tools): add read tools with data-freshness metadata`
- `feat(tools): expose engine as typed compute tools`
- `feat(policy): enforce approval thresholds from policy.yaml`
- `feat(tools): add idempotent action tools behind the policy gate`

### P4 — LLM providers and agent loop

- `feat(llm): add provider interface and scripted client`
- `feat(agent): run purchasing state machine with step persistence`
- `feat(agent): validate tool calls and return schema errors once`
- `feat(llm): add Gemini client with pacing and backoff`
- `feat(llm): add OpenAI-compatible client`
- `feat(agent): ground narrative in the structured decision`
- `feat(api): start runs, list steps, approve and resume`

Before the first real Gemini call, the API key is placed in `.env` (never committed).

### P5 — Mock supplier and feedback loop (+ submittable baseline)

- `feat(supplier): add fixture-driven mock supplier service`
- `feat(agent): diff persisted PO against intended state`
- `feat(agent): replan on supplier response and failed outcome check`
- `feat(supplier): update reliability score from fill outcomes`
- `test(agent): cover partial-fill recovery end to end`
- `build: add minimal docker-compose and Makefile`
- `docs: add README with setup, architecture diagram and approach`
- `docs: answer the brief's agent-behaviour questions in README`

The P5 README already contains every item on the submission checklist, so the submission is complete even if P8 is cut.
P8 only refines it. See "README structure" below.

### P6 — Evals

- `feat(evals): grade runs on six deterministic dimensions`
- `test(evals): prove graders fail known-bad trajectories`
- `feat(evals): add CLI runner with case selection and pacing`
- `feat(evals): judge narrative quality against rubric`
- `docs(evals): add evaluation report`

### P7 — UI (time-boxed)

- `feat(ui): scaffold React app with API client`
- `feat(ui): add scenario launcher and live run timeline`
- `feat(ui): add decision card and inventory projection chart`
- `feat(ui): add approvals inbox and purchase order history`
- `feat(ui): add eval results dashboard`

### P8 — Docs and polish

- `build: serve UI and API together in docker-compose`
- `docs: refine README demo walkthrough, eval results and limitations`
- Review fixes, in focused commits.

### README structure

Section titles map one-to-one to the brief's submission checklist. All sections exist from P5; later phases fill in results.

| Section | Contents |
|---|---|
| **Setup & Run** | uv + Python 3.12; `make` targets; `docker compose up`; scripted (offline, no key) mode; free Gemini key from Google AI Studio; Groq preset; running tests |
| **Environment Variables** | Table of every `.env.example` variable, with default and purpose |
| **Architecture** | Mermaid diagram (UI → API → agent state machine → tools → engine / policy gate / DB / mock supplier / LLMClient) plus a state-machine diagram |
| **Approach** | Problem breakdown; the LLM-for-judgement / code-for-math split; never trust the recommendation; constraint priority |
| **Agent Behaviour — design answers** | The brief's 8 questions (information, tools/APIs, data, tool interaction, decision making, allowed actions, human approval, validation), 2–4 lines each, linking to the code |
| **How Decisions Are Validated** | The 4 feedback layers, reason codes, replan cap → escalation, narrative grounding check |
| **Test Scenarios & Evaluation Approach** | Case table, 6 dimensions, negative controls, scripted vs real-LLM runs, link to `evals/report.md` |
| **Demo** | Step-by-step S1 and S2 walkthrough (injected supplier failure → replan → approval), plus a demo-video link placeholder |
| **Beyond the scenarios** | Optional buyer problems already covered, each linked to its eval case or code: supplier reliability (EWMA update), alternate suppliers, promotional buying (promo-explained uplift), forecast anomalies (outlier vs surge), safety stock, open POs (inbound netting, partial fill) |
| **Limitations & Next Steps** | |

`docs/decisions.md` grows inside the commit that makes each decision, not in one big dump at the end.

### Per-commit and per-phase checklist

- **Every commit:** tests pass → `git status` → `git diff --staged` → secret scan → commit → `git log -1 --format=%B` (no trailer).
- **Phase end:** full test run, summary and manual checks, list of the phase's commits, then STOP.
- **After approval:** `gh auth status`, push (never force-push), then `git log --oneline`.

## Assumptions (recorded in decisions.md)

- Safety stock = safety_days × avg daily forecast (explainable, hand-computable).
- Reducing a PO line to the supplier-confirmed qty is an acknowledgement (auto). Any cancellation needs approval.
- Currency is per node (COP/MXN); value thresholds are per currency in policy.yaml.
- If time gets tight, the cut order is: S3 promo case → OpenAI-compat live verification → UI eval dashboard polish.
  S1/S2, evals and docs are never cut.

## Verification

- `make test`: pytest, no key needed.
- `make eval`: scripted, 3 runs; writes `evals/report.md`.
- `make eval PROVIDER=gemini`: the default real subset. `CASE=...` selects one case.
- `docker compose up` → in the UI, launch S1 and S2 (scripted and gemini), inject a supplier REJECTED/PARTIAL, approve in
  the inbox, and see the replan and the PO history.
