"""Scenario clock.

Every scenario fixes "now" (`as_of`) so freshness checks, need dates and PO arrival
days are reproducible. Nothing in the backend reads the wall clock for business logic.
"""

from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session


class Clock:
    def __init__(self, as_of: datetime) -> None:
        self.as_of = as_of

    def now(self) -> datetime:
        return self.as_of

    def today(self) -> date:
        return self.as_of.date()

    def day(self, offset: int) -> date:
        """Calendar date `offset` days from today (negative = past)."""
        return self.today() + timedelta(days=offset)

    def day_offset(self, d: date) -> int:
        """Inverse of `day`: how many days from today `d` is."""
        return (d - self.today()).days


def clock_for(session: Session) -> Clock:
    from app.models import Workspace

    ws = session.get(Workspace, 1)
    if ws is None:
        raise RuntimeError("No scenario loaded: seed a workspace first")
    return Clock(ws.as_of)
