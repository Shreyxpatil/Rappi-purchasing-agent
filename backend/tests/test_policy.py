import pytest
import yaml
from pydantic import ValidationError

from app.policy import POLICY_PATH, load_policy


def test_policy_file_loads_with_expected_thresholds() -> None:
    p = load_policy()
    assert p.auto_execute_max_value == {"COP": 3000000, "MXN": 15000}
    assert (p.accept_tolerance_pct, p.price_variance_pct, p.max_replans) == (10, 5, 3)
    assert p.freshness_limits_hours["inventory"] == 24


def test_unknown_or_missing_keys_are_rejected(tmp_path) -> None:
    data = yaml.safe_load(POLICY_PATH.read_text())
    data["auto_aprove_typo"] = 1
    bad = tmp_path / "p.yaml"
    bad.write_text(yaml.safe_dump(data))
    with pytest.raises(ValidationError):
        load_policy(bad)
