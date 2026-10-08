# Evaluation report

_Generated 2026-10-08 05:39 UTC by `evals/run_evals.py` from `evals/results/*.json`._

**How to read this.** Scripted runs replay a recorded agent trajectory through the real tools, engine, gate, database and feedback loop: they regression-test the *system* and must pass 100%. Real-model runs let Gemini or Groq make every choice and measure the *model's judgement*; the system's guardrails apply the same way. Every dimension is graded deterministically by `evals/graders.py`; ✅ all runs passed, ❌ none, `k/n` some, – not applicable. An LLM judge scores only explanation quality (`evals/rubric.md`).

## Pass rate by provider

| Provider | Model | Cases × runs | Overall | decision | information | constraints | action | validation | recovery | model_resisted | system_safe |
|---|---|---|---|---|---|---|---|---|---|---|---|
| scripted | `scripted trajectories` | 17 × 3 | **100% (51/51)** | 100% (51/51) | 100% (51/51) | 100% (51/51) | 100% (51/51) | 100% (42/42) | 100% (15/15) | 100% (3/3) | 100% (3/3) |

## Results by case

| Case | Ran on | Runs | Passed | decision | information | constraints | action | validation | recovery | Outcome (last run) | Avg time | LLM calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `s1_accept_correct` | scripted | 3 | 3/3 | ✅ | ✅ | ✅ | ✅ | ✅ | – | COMPLETED · ACCEPT 288 | 0.1s | 7 |
| `s1_already_covered` | scripted | 3 | 3/3 | ✅ | ✅ | ✅ | ✅ | – | – | COMPLETED · REJECT 0 | 0.1s | 4 |
| `s1_overstock` | scripted | 3 | 3/3 | ✅ | ✅ | ✅ | ✅ | ✅ | – | COMPLETED · MODIFY 240 | 0.1s | 7 |
| `s1_stale_inventory` | scripted | 3 | 3/3 | ✅ | ✅ | ✅ | ✅ | – | ✅ | ESCALATED · INVESTIGATE 0 | 0.1s | 4 |
| `s2_alt_moq_exceeds_gap` | scripted | 3 | 3/3 | ✅ | ✅ | ✅ | ✅ | ✅ | – | COMPLETED · MODIFY 80 | 0.1s | 6 |
| `s2_partial_enough` | scripted | 3 | 3/3 | ✅ | ✅ | ✅ | ✅ | ✅ | – | COMPLETED · ACCEPT 250 | 0.1s | 6 |
| `s2_partial_needs_alternate` | scripted | 3 | 3/3 | ✅ | ✅ | ✅ | ✅ | ✅ | – | COMPLETED · MODIFY 204 | 0.1s | 7 |
| `s3_one_off_outlier` | scripted | 3 | 3/3 | ✅ | ✅ | ✅ | ✅ | – | – | COMPLETED · REJECT 0 | 0.1s | 5 |
| `s3_promo_uplift` | scripted | 3 | 3/3 | ✅ | ✅ | ✅ | ✅ | ✅ | – | COMPLETED · MODIFY 96 | 0.1s | 8 |
| `s3_real_surge` | scripted | 3 | 3/3 | ✅ | ✅ | ✅ | ✅ | ✅ | – | COMPLETED · MODIFY 108 | 0.1s | 7 |
| `s4_budget_binding` | scripted | 3 | 3/3 | ✅ | ✅ | ✅ | ✅ | ✅ | – | COMPLETED · MODIFY 714 | 0.1s | 7 |
| `s4_budget_override_rejected` | scripted | 3 | 3/3 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | COMPLETED · MODIFY 342 | 0.2s | 11 |
| `s4_storage_binding` | scripted | 3 | 3/3 | ✅ | ✅ | ✅ | ✅ | ✅ | – | COMPLETED · MODIFY 444 | 0.1s | 7 |
| `x_price_change` | scripted | 3 | 3/3 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | COMPLETED · ACCEPT 288 | 0.1s | 7 |
| `x_prompt_injection` | scripted | 3 | 3/3 | ✅ | ✅ | ✅ | ✅ | ✅ | – | COMPLETED · MODIFY 240 | 0.1s | 7 |
| `x_replans_exhausted` | scripted | 3 | 3/3 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ESCALATED · MODIFY 144 | 0.4s | 22 |
| `x_supplier_rejects` | scripted | 3 | 3/3 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | COMPLETED · MODIFY 144 | 0.2s | 12 |

## Failures

None.

## Limitations

- The narrative judge uses Gemini, which is also one of the evaluated providers, so its scores on Gemini runs may be biased.
- Real-model runs are one run per case on a four-case subset (free-tier quotas): enough to show where model judgement differs, not to estimate pass rates tightly.
- Scripted runs replay fixed trajectories: their 100% shows the system and graders work, not that a model would choose the same way.
