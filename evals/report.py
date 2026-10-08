"""Build evals/report.md from the stored results of every provider."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EVALS = Path(__file__).resolve().parent
RESULTS_DIR = EVALS / "results"
REPORT = EVALS / "report.md"
DIMS = ("decision", "information", "constraints", "action", "validation", "recovery")
ORDER = {"scripted": 0, "gemini": 1, "openai_compat": 2}
LABEL = {"scripted": "scripted", "gemini": "Gemini", "openai_compat": "Groq (OpenAI-compatible)"}


def _rate(values: list[Any]) -> str:
    vals = [v for v in values if v is not None]
    return f"{sum(1 for v in vals if v) / len(vals):.0%} ({sum(1 for v in vals if v)}/{len(vals)})" if vals else "n/a"


def _cell(values: list[Any]) -> str:
    vals = [v for v in values if v is not None]
    if not vals:
        return "–"
    passed = sum(1 for v in vals if v)
    return "✅" if passed == len(vals) else ("❌" if passed == 0 else f"{passed}/{len(vals)}")


def load_results(results_dir: Path = RESULTS_DIR) -> list[dict[str, Any]]:
    out = [json.loads(p.read_text()) for p in sorted(results_dir.glob("*.json"))]
    return sorted(out, key=lambda r: ORDER.get(r["provider"], 9))


def write_report(results_dir: Path = RESULTS_DIR, report: Path = REPORT) -> Path:
    results = load_results(results_dir)
    lines = ["# Evaluation report", "",
             f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} by `evals/run_evals.py` "
             "from `evals/results/*.json`._", "",
             "**How to read this.** Scripted runs replay a recorded agent trajectory through the real tools, engine, "
             "gate, database and feedback loop: they regression-test the *system* and must pass 100%. Real-model runs "
             "let Gemini or Groq make every choice and measure the *model's judgement*; the system's guardrails apply "
             "the same way. Every dimension is graded deterministically by `evals/graders.py`; ✅ all runs passed, "
             "❌ none, `k/n` some, – not applicable. An LLM judge scores only explanation quality (`evals/rubric.md`).", ""]

    lines += ["## Pass rate by provider", "",
              "| Provider | Model | Cases × runs | Overall | " + " | ".join(DIMS) + " | model_resisted | system_safe |",
              "|---|---|---|---|" + "---|" * len(DIMS) + "---|---|"]
    for res in results:
        runs = res["runs"]
        cases = len({r["case"] for r in runs})
        per = [_rate([r["dimensions"].get(d) for r in runs]) for d in DIMS]
        mr = _rate([r["extras"].get("model_resisted") for r in runs if "model_resisted" in r.get("extras", {})])
        ss = _rate([r["extras"].get("system_safe") for r in runs if "system_safe" in r.get("extras", {})])
        lines.append(f"| {LABEL.get(res['provider'], res['provider'])} | `{res['model']}` | {cases} × "
                     f"{max(r['run'] for r in runs)} | **{_rate([r['passed'] for r in runs])}** | "
                     + " | ".join(per) + f" | {mr} | {ss} |")

    lines += ["", "## Results by case", "",
              "| Case | Ran on | Runs | Passed | " + " | ".join(DIMS) + " | Outcome (last run) | Avg time | LLM calls |",
              "|---|---|---|---|" + "---|" * len(DIMS) + "---|---|---|"]
    rows = [(r, res) for res in results for r in res["runs"]]
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r, res in rows:
        by_key.setdefault((r["case"], res["provider"]), []).append(r)
    for (case, provider), runs in sorted(by_key.items(), key=lambda kv: (kv[0][0], ORDER.get(kv[0][1], 9))):
        last = runs[-1]
        lines.append(
            f"| `{case}` | {LABEL.get(provider, provider)} | {len(runs)} | {sum(r['passed'] for r in runs)}/{len(runs)} | "
            + " | ".join(_cell([r["dimensions"].get(d) for r in runs]) for d in DIMS)
            + f" | {last.get('status', 'error')} · {last.get('outcome')} {last.get('quantity')} | "
            f"{sum(r['duration_s'] for r in runs) / len(runs):.1f}s | {sum(r.get('llm_calls', 0) for r in runs) / len(runs):.0f} |")

    judged = [(r, res) for r, res in rows if r.get("judge", {}).get("scores")]
    if judged:
        lines += ["", "## Explanation quality (LLM judge)", "", "| Case | Ran on | Avg score (1–5) | Comment |",
                  "|---|---|---|---|"]
        for r, res in judged:
            lines.append(f"| `{r['case']}` | {LABEL.get(res['provider'])} | {r['judge']['average']} | "
                         f"{r['judge'].get('comment', '').replace('|', '/')} |")

    failures = [(r, res) for r, res in rows if not r["passed"]]
    lines += ["", "## Failures", ""]
    if not failures:
        lines.append("None.")
    for r, res in failures:
        why = r.get("error") or "; ".join(f"**{k}**: {v}" for k, v in r["failures"].items())
        lines.append(f"- `{r['case']}` on {LABEL.get(res['provider'])} (run {r['run']}): {why}")
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report
