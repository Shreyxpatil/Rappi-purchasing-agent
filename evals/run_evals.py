"""Evaluation runner.

    uv run python -m evals.run_evals                                   # scripted, all 17 cases x 3 runs
    uv run python -m evals.run_evals --provider gemini                 # real model: default subset x 1 run
    uv run python -m evals.run_evals --provider openai_compat --case s1_overstock --runs 3
    uv run python -m evals.run_evals --provider gemini --judge gemini  # + explanation-quality judge

Runs are sequential (real providers are paced). Results are stored per provider in evals/results/<provider>.json;
re-running a subset replaces only those cases. evals/report.md is regenerated from all stored results.
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.cli import run_case  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.fixtures import list_fixtures, load_fixture  # noqa: E402
from app.llm.factory import make_client  # noqa: E402
from app.logs import configure_logging  # noqa: E402
from evals.graders import grade_run  # noqa: E402
from evals.judge import judge_narrative  # noqa: E402
from evals.report import RESULTS_DIR, write_report  # noqa: E402

# Free-tier quotas allow a representative subset on real models: one per scenario family plus the injection case.
REAL_SUBSET = ["s1_overstock", "s2_partial_needs_alternate", "s4_budget_binding", "x_prompt_injection"]
DEFAULT_RUNS = {"scripted": 3, "gemini": 1, "openai_compat": 1}
# Run failures caused by the provider or the network, not by the model's judgement: kept out of pass rates.
INFRA_CODES = {"NETWORK", "LLM_QUOTA_EXHAUSTED", "LLM_UNAVAILABLE", "LLM_TIMEOUT", "RUN_TIMEOUT", "BAD_RESPONSE",
               "TASK_LOST"}


def infra_error(row: dict[str, Any]) -> str | None:
    """The infrastructure failure code of a run, if that is why it failed."""
    if row.get("error"):  # the run could not even start or crashed outside the agent
        return row["error"].split(":")[0]
    code = (row.get("run_error") or "").split(":")[0]
    return code if code in INFRA_CODES or code.startswith("HTTP_5") else None


def model_name(provider: str) -> str:
    s = get_settings()
    return {"scripted": "scripted trajectories", "gemini": s.gemini_model,
            "openai_compat": s.openai_compat_model}.get(provider, provider)


def evaluate(provider: str, case: str, run_index: int, judge_provider: str | None) -> dict[str, Any]:
    fx = load_fixture(case)
    start = time.perf_counter()
    row: dict[str, Any] = {"case": case, "scenario": fx.scenario, "provider": provider, "run": run_index}
    try:
        session, run = run_case(case, provider)
        g = grade_run(session, run, fx)
        llm = [st for st in run.steps if st.kind == "llm"]
        d = run.decision or {}
        row.update(passed=g["passed"], dimensions={k: v["pass"] for k, v in g["dimensions"].items()},
                   extras={k: v["pass"] for k, v in g["extras"].items()},
                   failures={k: v["detail"] for k, v in {**g["dimensions"], **g["extras"]}.items() if v["pass"] is False},
                   status=run.status, outcome=d.get("outcome"), quantity=d.get("quantity"), replans=run.replan_count,
                   llm_calls=len(llm), tokens_in=sum(st.tokens_in or 0 for st in llm),
                   tokens_out=sum(st.tokens_out or 0 for st in llm),
                   narrative_source=(run.context or {}).get("narrative_source"), narrative=run.narrative)
        errors = [st for st in run.steps if st.kind == "error"]
        if errors:  # why the run itself failed (provider outage, rate limit, script exhausted...)
            row["run_error"] = f"{errors[-1].name}: {(errors[-1].output or {}).get('message', '')}"[:400]
        if judge_provider and run.narrative and d:
            row["judge"] = judge_narrative(make_client(judge_provider), run.narrative, d)
    except Exception as e:  # a provider outage is a failed run, not a crashed eval
        row.update(passed=False, error=f"{type(e).__name__}: {e}"[:400], dimensions={}, extras={}, failures={})
    row["duration_s"] = round(time.perf_counter() - start, 1)
    row["infra_error"] = infra_error(row)
    return row


def store(provider: str, rows: list[dict[str, Any]], results_dir: Path = RESULTS_DIR) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"{provider}.json"
    old = json.loads(path.read_text()) if path.exists() else {"runs": []}
    cases = {r["case"] for r in rows}
    kept = [r for r in old["runs"] if r["case"] not in cases]
    data = {"provider": provider, "model": model_name(provider),
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "runs": sorted(kept + rows, key=lambda r: (r["case"], r["run"]))}
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=str))
    return path


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--provider", nargs="+", default=["scripted"], choices=["scripted", "gemini", "openai_compat"])
    p.add_argument("--case", action="append", help="repeatable; default: all (scripted) or the real-model subset")
    p.add_argument("--runs", type=int, help="runs per case (default: 3 scripted, 1 real)")
    p.add_argument("--judge", choices=["gemini", "openai_compat"], help="LLM judge for explanation quality")
    p.add_argument("--verbose", action="store_true", help="log every model call (default: warnings only)")
    args = p.parse_args()
    configure_logging(logging.INFO if args.verbose else logging.WARNING)

    for provider in args.provider:
        cases = args.case or ([f.id for f in list_fixtures()] if provider == "scripted" else REAL_SUBSET)
        runs = args.runs or DEFAULT_RUNS[provider]
        rows = []
        for case in cases:
            for i in range(1, runs + 1):
                row = evaluate(provider, case, i, args.judge)
                rows.append(row)
                flag = "PASS" if row["passed"] else "FAIL"
                print(f"[{provider}] {case} #{i}: {flag} {row.get('outcome')} {row.get('quantity')} "
                      f"({row['duration_s']}s){' ' + row['error'] if row.get('error') else ''}", flush=True)
                store(provider, rows)  # keep partial results if a long real-model run is interrupted
        print(f"[{provider}] {sum(r['passed'] for r in rows)}/{len(rows)} runs passed", flush=True)
    print(f"report: {write_report()}")


if __name__ == "__main__":
    main()
