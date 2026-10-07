"""Purchasing policy: thresholds (policy.yaml) and the gate that enforces them in code."""

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

POLICY_PATH = Path(__file__).parent / "policy.yaml"


class DemandShiftPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    recent_days: int
    elevated_pct: float
    sustained_min_days: int
    bulk_share: float


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    accept_tolerance_pct: float
    auto_execute_max_value: dict[str, float]
    price_variance_pct: float
    freshness_limits_hours: dict[str, float]
    max_replans: int
    max_validation_failures: int
    transfer_lead_days: int
    demand_shift: DemandShiftPolicy
    reliability_ewma_alpha: float


def load_policy(path: Path = POLICY_PATH) -> Policy:
    return Policy.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


@lru_cache
def get_policy() -> Policy:
    return load_policy()
