"""Run one scenario end to end in a throwaway database and print the trace.

    uv run python -m app.cli s1_overstock                     # scripted: no key, no cost, ~1 s
    uv run python -m app.cli s2_partial_needs_alternate --provider openai_compat
Approvals are answered from the fixture's approval_responses, as a buyer would.
"""

import argparse
import logging
import time

from sqlalchemy import select

from app.agent.loop import PurchasingAgent
from app.db import create_schema, make_engine, make_session_factory
from app.fixtures import load_fixture
from app.llm.factory import make_client
from app.logs import configure_logging
from app.models import Approval, PurchaseOrder, StockTransfer
from app.seed import seed_workspace


def run_case(case_id: str, provider: str = "scripted", database_url: str = "sqlite://", script_variant: str = ""):
    """Seed, run and answer approvals; returns (session, run). Shared with the eval runner."""
    fx = load_fixture(case_id)
    engine = make_engine(database_url)
    create_schema(engine)
    session = make_session_factory(engine)()
    seed_workspace(session, fx)
    session.commit()
    agent = PurchasingAgent(session, make_client(provider, case_id=case_id, script_variant=script_variant))
    run = agent.run(agent.start(fx.id, fx.trigger.model_dump()))
    answers = list(fx.approval_responses)
    while run.status == "AWAITING_APPROVAL":
        approval = session.scalars(select(Approval).filter_by(run_id=run.id, status="PENDING")).one()
        approve = (answers.pop(0) if answers else "REJECT") == "APPROVE"
        run = agent.resolve_and_resume(run, approval.id, approve, decided_by="cli", comment="from fixture")
    return session, run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("case")
    parser.add_argument("--provider", default="scripted", choices=["scripted", "gemini", "openai_compat"])
    parser.add_argument("--verbose", action="store_true", help="log every model call (default: warnings only)")
    args = parser.parse_args()
    configure_logging(logging.INFO if args.verbose else logging.WARNING)
    start = time.perf_counter()
    session, run = run_case(args.case, args.provider)
    d = run.decision or {}
    print(f"{args.case} [{args.provider}] -> {run.status}: {d.get('outcome')} {d.get('quantity')} "
          f"({d.get('option_id')}), replans={run.replan_count}, {time.perf_counter() - start:.1f}s")
    for st in run.steps:
        if st.kind in ("transition", "llm"):
            continue
        flag = "" if st.ok else "  <-- " + str((st.output or {}).get("error", {}).get("code", "not ok")
                                              if isinstance(st.output, dict) else "not ok")
        print(f"  {st.seq:>3} {st.state:<15} {st.kind:<15} {st.name}{flag}")
    for po in session.scalars(select(PurchaseOrder).filter_by(run_id=run.id)):
        print(f"  PO {po.id} {po.supplier_id} {[l.qty_ordered for l in po.lines]} {po.status}")
    for t in session.scalars(select(StockTransfer).filter_by(run_id=run.id)):
        print(f"  transfer {t.id} {t.from_node_id}->{t.to_node_id} {t.qty} {t.status}")
    print("\n" + (run.narrative or ""))


if __name__ == "__main__":
    main()
