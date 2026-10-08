"""Build evals/report.md from the stored results of every provider."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EVALS = Path(__file__).resolve().parent
RESULTS_DIR = EVALS / "results"
REPORT = EVALS / "report.md"
# Hand-written root-cause notes for real-model failures, keyed "<case>|<provider>|<run>".
ROOT_CAUSES = EVALS / "root_causes.json"
DIMS = ("decision", "information", "constraints", "action", "validation", "recovery")
ORDER = {"scripted": 0, "gemini": 1, "openai_compat": 2}
LABEL = {"scripted": "scripted", "gemini": "Gemini", "openai_compat": "Groq (OpenAI-compatible)"}


def _graded(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Runs that measured the system or the model; infrastructure failures are reported separately."""
    return [r for r in runs if not r.get("infra_error")]


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
              "| Provider | Model | Cases × runs | Overall | " + " | ".join(DIMS)
              + " | model_resisted | system_safe | Did not complete |",
              "|---|---|---|---|" + "---|" * len(DIMS) + "---|---|---|"]
    for res in results:
        infra = sum(1 for r in res["runs"] if r.get("infra_error"))
        runs = _graded(res["runs"]) or res["runs"][:0]
        cases = len({r["case"] for r in runs})
        per = [_rate([r["dimensions"].get(d) for r in runs]) for d in DIMS]
        mr = _rate([r["extras"].get("model_resisted") for r in runs if "model_resisted" in r.get("extras", {})])
        ss = _rate([r["extras"].get("system_safe") for r in runs if "system_safe" in r.get("extras", {})])
        lines.append(f"| {LABEL.get(res['provider'], res['provider'])} | `{res['model']}` | {cases} × "
                     f"{max((r['run'] for r in res['runs']), default=0)} | **{_rate([r['passed'] for r in runs])}** | "
                     + " | ".join(per) + f" | {mr} | {ss} | {infra or '–'} |")

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
            f"| `{case}` | {LABEL.get(provider, provider)} | {len(runs)} | "
            + (f"{sum(r['passed'] for r in runs)}/{len(runs)}" if _graded(runs) else "did not complete") + " | "
            + " | ".join(_cell([r["dimensions"].get(d) for r in _graded(runs)]) for d in DIMS)
            + f" | {last.get('status', 'error')} · {last.get('outcome')} {last.get('quantity')} | "
            f"{sum(r['duration_s'] for r in runs) / len(runs):.1f}s | {sum(r.get('llm_calls', 0) for r in runs) / len(runs):.0f} |")

    judged = [(r, res) for r, res in rows if r.get("judge", {}).get("scores")]
    if judged:
        lines += ["", "## Explanation quality (LLM judge)", "", "| Case | Ran on | Avg score (1–5) | Comment |",
                  "|---|---|---|---|"]
        for r, res in judged:
            lines.append(f"| `{r['case']}` | {LABEL.get(res['provider'])} | {r['judge']['average']} | "
                         f"{r['judge'].get('comment', '').replace('|', '/')} |")

    notes = json.loads(ROOT_CAUSES.read_text()) if ROOT_CAUSES.exists() else {}
    failures = [(r, res) for r, res in rows if not r["passed"] and not r.get("infra_error")]
    incomplete = [(r, res) for r, res in rows if r.get("infra_error")]
    if incomplete:
        lines += ["", "## Did not complete (infrastructure, excluded from pass rates)", ""]
        for r, res in incomplete:
            lines.append(f"- `{r['case']}` on {LABEL.get(res['provider'])} (run {r['run']}): `{r['infra_error']}`")
            note = (json.loads(ROOT_CAUSES.read_text()) if ROOT_CAUSES.exists() else {}).get(
                f"{r['case']}|{res['provider']}|{r['run']}")
            if note:
                lines.append(f"  - *Root cause:* {note}")
    lines += ["", "## Failures", ""]
    if not failures:
        lines.append("None.")
    for r, res in failures:
        why = r.get("error") or "; ".join(f"**{k}**: {v}" for k, v in r["failures"].items())
        if r.get("run_error"):
            why = f"run failed with `{r['run_error']}`. " + why
        lines.append(f"- `{r['case']}` on {LABEL.get(res['provider'])} (run {r['run']}): {why}")
        note = notes.get(f"{r['case']}|{res['provider']}|{r['run']}")
        lines.append(f"  - *Root cause:* {note}" if note else "  - *Root cause:* not yet analysed.")

    lines += ["", "## Limitations", "",
              "- The narrative judge uses Gemini, which is also one of the evaluated providers, so its scores on "
              "Gemini runs may be biased.",
              "- Real-model runs are one run per case on a four-case subset (free-tier quotas): enough to show where "
              "model judgement differs, not to estimate pass rates tightly.",
              "- Scripted runs replay fixed trajectories: their 100% shows the system and graders work, not that a "
              "model would choose the same way."]
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report
