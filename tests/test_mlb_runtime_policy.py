import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "mlb_runtime_policy.py"
spec = importlib.util.spec_from_file_location("mlb_runtime_policy_test", SCRIPT_PATH)
assert spec is not None
mlb_runtime_policy = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(mlb_runtime_policy)


def _write_flag(state: Path, **overrides):
    flag = {
        "schema": "vig-standing-authorization-v1",
        "enabled": True,
        "scope": "MLB Polymarket US moneyline only",
    }
    flag.update(overrides)
    (state / "standing_authorization.json").write_text(json.dumps(flag))


class MlbRuntimePolicyTests(unittest.TestCase):
    def test_missing_flag_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(mlb_runtime_policy.standing_authorization_enabled(Path(tmp)))

    def test_explicit_flag_enables_routing(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            _write_flag(state)
            self.assertTrue(mlb_runtime_policy.standing_authorization_enabled(state))

    def test_disabled_flag_suspends_routing(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            _write_flag(state, enabled=False)
            self.assertFalse(mlb_runtime_policy.standing_authorization_enabled(state))

    def test_wrong_schema_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            _write_flag(state, schema="something-else")
            self.assertFalse(mlb_runtime_policy.standing_authorization_enabled(state))

    def test_corrupt_flag_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / "standing_authorization.json").write_text("{not json")
            self.assertFalse(mlb_runtime_policy.standing_authorization_enabled(state))

    def test_prose_policy_files_no_longer_grant_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / "policy.md").write_text(
                "## Standing authorization\n"
                "Current market path: Polymarket sports moneyline where exact mapping is verified\n"
            )
            (state / "risk_limits.md").write_text(
                "Auto-entry: only standing-authorized MLB Polymarket moneyline candidates\n"
            )
            self.assertFalse(mlb_runtime_policy.standing_authorization_enabled(state))


if __name__ == "__main__":
    unittest.main()
