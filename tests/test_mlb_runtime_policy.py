import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "mlb_runtime_policy.py"
spec = importlib.util.spec_from_file_location("mlb_runtime_policy_test", SCRIPT_PATH)
assert spec is not None
mlb_runtime_policy = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(mlb_runtime_policy)


class MlbRuntimePolicyTests(unittest.TestCase):
    def test_missing_policy_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(mlb_runtime_policy.standing_authorization_enabled(Path(tmp)))

    def test_exact_written_mlb_moneyline_authorization_enables_routing(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / "policy.md").write_text(
                "## Standing authorization\n"
                "Current market path: Polymarket sports moneyline where exact mapping is verified\n"
            )
            (state / "risk_limits.md").write_text(
                "Auto-entry: only standing-authorized MLB Polymarket moneyline candidates\n"
            )

            self.assertTrue(mlb_runtime_policy.standing_authorization_enabled(state))


if __name__ == "__main__":
    unittest.main()