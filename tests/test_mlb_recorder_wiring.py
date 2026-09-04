"""Tests for the recorder wiring, the slate receipt, and the deployment rail.

The defect these cover is one shape seen four times: a check that exists,
reads correctly, and is never reached. The 2026-09-01 slate ran with the
recorder deployed and wrote no reads, because running the validator was a
sentence in a prompt; the denominator cross-check could not have run either,
because nothing persisted a scan to check against; the cron reported success
and failure in the same record; and the execution boundary accepted any
non-empty ``model_version`` string because "a version exists" was standing in
for "a model was deployed".
"""

import hashlib
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import vig_policy_state
from scripts import mlb_game_reads
from scripts import mlb_execution_gate
from scripts import mlb_probability_chain_report
from scripts import mlb_probability_model
from scripts import mlb_runtime_policy
from scripts import mlb_slate_receipt
from scripts import mlb_slate_writer
from scripts import mlb_stage2_scan

REPO_ROOT = Path(__file__).resolve().parents[1]


def read(game_pk=823509, **overrides):
    entry = {
        "game_pk": game_pk,
        "event_id": f"4018{game_pk}",
        "away": "Atlanta Braves",
        "home": "Milwaukee Brewers",
        "disposition": "pass",
        "dk_fair_prob": {"away": 0.398, "home": 0.602},
        "polymarket_ask": {"away": 0.460, "home": 0.545},
        "raw_probability": {"away": 0.400, "home": 0.610},
        "uncertainty_haircut": 0.02,
        "conservative_probability": {"away": 0.380, "home": 0.590},
        "model_version": "vig-mlb-market-v1",
        "net_edge": {"away": -0.080, "home": 0.045},
        "refusing_rails": ["price_discipline"],
    }
    entry.update(overrides)
    return entry


def schedule(reads=None, date="2026-09-01", **overrides):
    reads = [read()] if reads is None else reads
    payload = {
        "date": date,
        "sport": "MLB",
        "market_type": "moneyline",
        "candidates": [],
        "lineup_watchlist": [],
        "slate_denominator": {
            "source": "mlb_stage2_scan",
            "fetched_at_utc": f"{date}T15:30:00+00:00",
            "games": [
                {
                    "game_pk": entry["game_pk"],
                    "event_id": entry["event_id"],
                    "away": entry["away"],
                    "home": entry["home"],
                }
                for entry in reads
                if isinstance(entry, dict) and isinstance(entry.get("game_pk"), int)
            ],
        },
        "game_reads": reads,
    }
    payload.update(overrides)
    return payload


def scan_rows(reads):
    return [
        {
            "game_pk": entry["game_pk"],
            "event_id": entry["event_id"],
            "away": entry["away"],
            "home": entry["home"],
        }
        for entry in reads
    ]


class Runtime:
    """A throwaway .picks tree, laid out exactly as the runtime lays it out."""

    def __init__(self, stack, day="2026-09-01"):
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.day = day
        (self.root / ".picks" / "execute").mkdir(parents=True)
        (self.root / ".picks" / "tmp").mkdir(parents=True)
        (self.root / ".picks" / "journal").mkdir(parents=True)
        # Every command in this file loads the deployed selection policy, so
        # the throwaway runtime carries one. A test whose answer depends on
        # whether the developer has a live Vig state dir is not a test.
        stack.enter_context(vig_policy_state.deployed_policy(self.root / "state"))

    @property
    def schedule_path(self):
        return self.root / ".picks" / "execute" / f"{self.day}-schedule.json"

    @property
    def scan_path(self):
        return self.root / ".picks" / "tmp" / f"stage2-{self.day}.json"

    def write_schedule(self, payload):
        self.schedule_path.write_text(json.dumps(payload), encoding="utf-8")

    def write_scan(self, rows):
        self.scan_path.write_text(json.dumps(rows), encoding="utf-8")


class DenominatorIsNotOptionalTests(unittest.TestCase):
    """The cross-check must not depend on anyone remembering a flag."""

    def setUp(self):
        import contextlib

        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.rt = Runtime(self.stack)

    def test_scan_and_validator_agree_on_where_the_denominator_lives(self):
        # The two halves are useless if they look in different directories,
        # and each one alone would pass its own tests while doing so. Assert
        # the SAME path from both sides rather than each against a literal.
        expected = mlb_game_reads.conventional_denominator_path(
            self.rt.schedule_path, {"date": self.rt.day}
        )
        self.assertEqual(
            mlb_stage2_scan.denominator_output_path(self.rt.day, self.rt.root),
            expected.resolve() if expected.is_absolute() else (self.rt.root / expected),
        )

    def test_the_scan_writes_where_the_validator_reads(self):
        rows = scan_rows([read()])
        with mock.patch.object(
            mlb_stage2_scan, "MlbSlateCollector"
        ) as collector, mock.patch.object(sys, "argv", ["x", "--date", self.rt.day]), \
                mock.patch.dict("os.environ", {"SPORTS_PICKS_ROOT": str(self.rt.root)}):
            collector.return_value.collect.return_value = rows
            mlb_stage2_scan.main()
        self.assertTrue(self.rt.scan_path.exists())
        self.assertEqual(json.loads(self.rt.scan_path.read_text()), rows)

    def test_a_missing_scan_is_an_error_not_a_skipped_check(self):
        # This is the whole defect: without the flag the check silently did
        # not run, so "nobody scanned" and "the scan agrees" shared exit 0.
        self.rt.write_schedule(schedule())
        errors = self._cli_errors()
        self.assertTrue(
            any("denominator scan not readable" in error for error in errors), errors
        )

    def test_a_present_scan_is_cross_checked_without_the_flag(self):
        reads = [read()]
        self.rt.write_schedule(schedule(reads))
        self.rt.write_scan(scan_rows(reads))
        self.assertEqual(self._cli_errors(), [])

    def test_a_short_roster_is_caught_without_the_flag(self):
        # A run that trimmed its own denominator to match a short read set is
        # exactly what the cross-check exists to refuse.
        kept, dropped = read(823509), read(824876)
        self.rt.write_schedule(schedule([kept]))
        self.rt.write_scan(scan_rows([kept, dropped]))
        errors = self._cli_errors()
        self.assertTrue(any("824876" in error for error in errors), errors)

    def test_an_undateable_schedule_says_so_rather_than_passing(self):
        payload = schedule()
        payload.pop("date")
        path = self.rt.root / ".picks" / "execute" / "untitled.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        errors = self._cli_errors(path)
        self.assertTrue(
            any("cannot locate the denominator scan" in error for error in errors),
            errors,
        )

    def _cli_errors(self, path=None):
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            mlb_game_reads.main([str(path or self.rt.schedule_path), "--validate"])
        return json.loads(buffer.getvalue())["errors"]


class SlateReceiptTests(unittest.TestCase):
    """Honest zero and recorder failure must never share a verdict or an exit code."""

    def setUp(self):
        import contextlib

        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.rt = Runtime(self.stack)

    def test_a_covered_slate_is_complete(self):
        reads = [read(823509), read(824876)]
        self.rt.write_schedule(schedule(reads))
        self.rt.write_scan(scan_rows(reads))
        receipt = mlb_slate_receipt.build_receipt(self.rt.root, self.rt.day)
        self.assertEqual(receipt["verdict"], mlb_slate_receipt.VERDICT_COMPLETE)
        self.assertEqual(receipt["scheduled_games"], 2)
        self.assertEqual(receipt["reads_recorded"], 2)

    def test_a_genuinely_empty_day_is_an_honest_zero(self):
        self.rt.write_schedule(schedule([]))
        self.rt.write_scan([])
        receipt = mlb_slate_receipt.build_receipt(self.rt.root, self.rt.day)
        self.assertEqual(receipt["verdict"], mlb_slate_receipt.VERDICT_HONEST_ZERO)

    def test_the_2026_09_01_shape_is_a_recorder_failure(self):
        # The real artifact: a schedule with neither key, against a scan that
        # enumerated a full card. Zero reads, and not remotely honest.
        reads = [read(823509), read(824876)]
        self.rt.schedule_path.write_text(
            json.dumps(
                {
                    "date": self.rt.day,
                    "sport": "MLB",
                    "market_type": "moneyline",
                    "candidates": [],
                    "lineup_watchlist": [],
                }
            ),
            encoding="utf-8",
        )
        self.rt.write_scan(scan_rows(reads))
        receipt = mlb_slate_receipt.build_receipt(self.rt.root, self.rt.day)
        self.assertEqual(receipt["verdict"], mlb_slate_receipt.VERDICT_RECORDER_FAILED)
        self.assertEqual(receipt["reads_recorded"], 0)
        self.assertEqual(receipt["scheduled_games"], 2)

    def test_zero_reads_with_an_unreadable_scan_is_never_called_honest(self):
        # The dangerous confusion: a scan we cannot read says nothing about
        # the size of the day, and must not be allowed to certify emptiness.
        self.rt.write_schedule(schedule([]))
        receipt = mlb_slate_receipt.build_receipt(self.rt.root, self.rt.day)
        self.assertEqual(receipt["verdict"], mlb_slate_receipt.VERDICT_RECORDER_FAILED)
        self.assertIsNone(receipt["scheduled_games"])

    def test_a_malformed_read_fails_the_receipt(self):
        reads = [read(823509), read(824876, dk_fair_prob={"away": "cheap", "home": 0.6})]
        self.rt.write_schedule(schedule(reads))
        self.rt.write_scan(scan_rows(reads))
        receipt = mlb_slate_receipt.build_receipt(self.rt.root, self.rt.day)
        self.assertEqual(receipt["verdict"], mlb_slate_receipt.VERDICT_RECORDER_FAILED)

    def test_an_orphan_read_fails_the_receipt(self):
        # A read for a game the scan never enumerated is as wrong as a missing
        # one, and in the opposite direction.
        reads = [read(823509), read(999999)]
        self.rt.write_schedule(schedule(reads))
        self.rt.write_scan(scan_rows([read(823509)]))
        receipt = mlb_slate_receipt.build_receipt(self.rt.root, self.rt.day)
        self.assertEqual(receipt["verdict"], mlb_slate_receipt.VERDICT_RECORDER_FAILED)
        self.assertTrue(
            any("999999" in error for error in receipt["recorder_errors"]),
            receipt["recorder_errors"],
        )

    def test_no_schedule_is_its_own_verdict(self):
        receipt = mlb_slate_receipt.build_receipt(self.rt.root, "2026-09-02")
        self.assertEqual(receipt["verdict"], mlb_slate_receipt.VERDICT_NO_SCHEDULE)
        self.assertEqual(
            receipt["writer_provenance"], mlb_slate_receipt.PROVENANCE_UNVERIFIABLE
        )

    def test_an_unreadable_schedule_has_unverifiable_not_absent_provenance(self):
        self.rt.schedule_path.write_text('{"slate_denominator": {"scan_sha')

        receipt = mlb_slate_receipt.build_receipt(self.rt.root, self.rt.day)

        self.assertEqual(receipt["verdict"], mlb_slate_receipt.VERDICT_RECORDER_FAILED)
        self.assertEqual(
            receipt["writer_provenance"], mlb_slate_receipt.PROVENANCE_UNVERIFIABLE
        )

    def test_exit_codes_separate_the_two_zeros(self):
        import contextlib
        import io

        self.rt.write_schedule(schedule([]))
        self.rt.write_scan([])
        with contextlib.redirect_stdout(io.StringIO()):
            honest = mlb_slate_receipt.main(
                ["--day", self.rt.day, "--root", str(self.rt.root)]
            )
        self.rt.scan_path.unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            failed = mlb_slate_receipt.main(
                ["--day", self.rt.day, "--root", str(self.rt.root)]
            )
        self.assertEqual((honest, failed), (0, 1))

    def test_every_verdict_is_in_the_closed_vocabulary(self):
        self.assertIn(mlb_slate_receipt.VERDICT_COMPLETE, mlb_slate_receipt.VERDICTS)
        self.rt.write_schedule(schedule([]))
        self.rt.write_scan([])
        receipt = mlb_slate_receipt.build_receipt(self.rt.root, self.rt.day)
        self.assertIn(receipt["verdict"], mlb_slate_receipt.VERDICTS)

    def test_write_persists_next_to_the_run_journal(self):
        self.rt.write_schedule(schedule([]))
        self.rt.write_scan([])
        receipt = mlb_slate_receipt.build_receipt(self.rt.root, self.rt.day)
        path = mlb_slate_receipt.write_receipt(self.rt.root, receipt)
        self.assertEqual(path.parent, self.rt.root / ".picks" / "journal")
        self.assertEqual(json.loads(path.read_text())["verdict"], receipt["verdict"])

    def test_write_flushes_and_fsyncs_before_replacing_the_receipt(self):
        self.rt.write_schedule(schedule([]))
        self.rt.write_scan([])
        receipt = mlb_slate_receipt.build_receipt(self.rt.root, self.rt.day)
        real_replace = mlb_slate_receipt.os.replace
        events = []

        def fsync(fd):
            events.append("fsync")

        def replace(source, destination):
            events.append("replace")
            real_replace(source, destination)

        with mock.patch.object(mlb_slate_receipt.os, "fsync", side_effect=fsync), \
                mock.patch.object(mlb_slate_receipt.os, "replace", side_effect=replace):
            path = mlb_slate_receipt.write_receipt(self.rt.root, receipt)

        self.assertEqual(events, ["fsync", "replace"])
        self.assertEqual(json.loads(path.read_text())["verdict"], receipt["verdict"])
        self.assertEqual(
            [item for item in path.parent.iterdir() if item.name != path.name], []
        )

    def test_failed_atomic_replace_keeps_the_previous_receipt(self):
        path = mlb_slate_receipt.receipt_path_for(self.rt.root, self.rt.day)
        path.parent.mkdir(parents=True, exist_ok=True)
        before = b'{"verdict": "previous"}\n'
        path.write_bytes(before)
        receipt = {"day": self.rt.day, "verdict": "complete"}

        with mock.patch.object(
            mlb_slate_receipt.os,
            "replace",
            side_effect=OSError("replace refused"),
        ):
            with self.assertRaisesRegex(OSError, "replace refused"):
                mlb_slate_receipt.write_receipt(self.rt.root, receipt)

        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(
            [item for item in path.parent.iterdir() if item.name != path.name], []
        )

    def test_cli_default_uses_the_shared_chicago_schedule_day(self):
        import contextlib
        import io

        with mock.patch.object(
            mlb_slate_receipt, "schedule_day_now", return_value="2042-07-08"
        ), contextlib.redirect_stdout(io.StringIO()) as output:
            status = mlb_slate_receipt.main(["--root", str(self.rt.root)])

        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())["day"], "2042-07-08")

    def test_policy_loader_failure_is_receipt_state_not_an_exception(self):
        self.rt.write_schedule(schedule([]))
        self.rt.write_scan([])

        with mock.patch.object(
            mlb_slate_receipt.mlb_runtime_policy,
            "load_mlb_selection_policy",
            side_effect=RuntimeError("policy store unavailable"),
        ):
            receipt = mlb_slate_receipt.build_receipt(self.rt.root, self.rt.day)

        self.assertEqual(receipt["verdict"], mlb_slate_receipt.VERDICT_HONEST_ZERO)
        self.assertEqual(receipt["policy_status"], mlb_slate_receipt.POLICY_ERROR)
        self.assertIn("RuntimeError", receipt["policy_warning"])
        self.assertIn(receipt["policy_status"], mlb_slate_receipt.POLICY_STATUSES)

    def test_missing_policy_stays_explicit_and_keeps_the_price_rail_closed(self):
        self.rt.write_schedule(schedule())
        self.rt.write_scan(scan_rows([read()]))

        with mock.patch.object(
            mlb_slate_receipt.mlb_runtime_policy,
            "load_mlb_selection_policy",
            return_value=None,
        ):
            receipt = mlb_slate_receipt.build_receipt(self.rt.root, self.rt.day)

        self.assertEqual(receipt["policy_status"], mlb_slate_receipt.POLICY_UNAVAILABLE)
        self.assertEqual(receipt["verdict"], mlb_slate_receipt.VERDICT_RECORDER_FAILED)
        self.assertTrue(
            any("policy is unavailable" in error for error in receipt["recorder_errors"]),
            receipt["recorder_errors"],
        )


class DeploymentRailTests(unittest.TestCase):
    """A version string is not a deployed model."""

    def _state(self, block=None):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(
            lambda: __import__("shutil").rmtree(directory, ignore_errors=True)
        )
        payload = {"daily_cap_usd": 100}
        if block is not None:
            payload["mlb_deployed_models"] = block
        (directory / "risk_limits.json").write_text(json.dumps(payload), encoding="utf-8")
        return directory

    def test_the_market_only_fallback_is_always_eligible(self):
        # It asserts no model: our probability is the book's. Refusing it
        # would halt the only configuration currently reachable.
        self.assertEqual(
            mlb_runtime_policy.model_deployment_errors(
                {"model_version": mlb_runtime_policy.MARKET_MODEL_VERSION},
                state_dir=self._state(),
            ),
            [],
        )

    def test_an_undeployed_version_is_refused_and_named(self):
        errors = mlb_runtime_policy.model_deployment_errors(
            {"model_version": "vig-mlb-elo-v3"}, state_dir=self._state()
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("vig-mlb-elo-v3", errors[0])

    def test_a_deployed_version_is_accepted(self):
        state = self._state(
            {
                "schema": mlb_runtime_policy.DEPLOYED_MODELS_SCHEMA,
                "versions": ["vig-mlb-elo-v3"],
            }
        )
        self.assertEqual(
            mlb_runtime_policy.model_deployment_errors(
                {"model_version": "vig-mlb-elo-v3"}, state_dir=state
            ),
            [],
        )

    def test_the_record_fails_closed_on_every_malformation(self):
        for label, block in (
            ("wrong schema", {"schema": "other", "versions": ["vig-mlb-elo-v3"]}),
            ("versions not a list", {"schema": mlb_runtime_policy.DEPLOYED_MODELS_SCHEMA,
                                     "versions": "vig-mlb-elo-v3"}),
            ("a non-string entry", {"schema": mlb_runtime_policy.DEPLOYED_MODELS_SCHEMA,
                                    "versions": ["vig-mlb-elo-v3", 7]}),
            ("a blank entry", {"schema": mlb_runtime_policy.DEPLOYED_MODELS_SCHEMA,
                               "versions": ["vig-mlb-elo-v3", "  "]}),
            ("not an object", {"schema": mlb_runtime_policy.DEPLOYED_MODELS_SCHEMA}),
        ):
            with self.subTest(label):
                state = self._state(block)
                self.assertEqual(
                    mlb_runtime_policy.load_deployed_model_versions(state), frozenset()
                )
                self.assertTrue(
                    mlb_runtime_policy.model_deployment_errors(
                        {"model_version": "vig-mlb-elo-v3"}, state_dir=state
                    )
                )

    def test_one_bad_entry_invalidates_the_whole_record(self):
        # Skipping the malformed entry and keeping the rest would make the
        # record's own unreadability the reason a version looked deployed.
        state = self._state(
            {
                "schema": mlb_runtime_policy.DEPLOYED_MODELS_SCHEMA,
                "versions": ["vig-mlb-elo-v3", None],
            }
        )
        self.assertEqual(
            mlb_runtime_policy.load_deployed_model_versions(state), frozenset()
        )

    def test_a_missing_file_fails_closed(self):
        empty = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(empty, ignore_errors=True))
        self.assertEqual(
            mlb_runtime_policy.load_deployed_model_versions(empty), frozenset()
        )

    def test_a_blank_version_is_left_to_the_trail_validator(self):
        # Reporting it here too would make one defect look like two, and the
        # trail validator already names it.
        self.assertEqual(
            mlb_runtime_policy.model_deployment_errors(
                {"model_version": "  "}, state_dir=self._state()
            ),
            [],
        )
        self.assertTrue(
            any(
                "model_version" in error
                for error in mlb_runtime_policy.stale_probability_field_errors(
                    {"model_version": "  "}
                )
            )
        )

    def test_the_market_version_has_exactly_one_definition(self):
        # A rename that reached only one of the two spellings would make the
        # market-only fallback — the only configuration that currently runs —
        # ineligible for execution. Consultation, not equality: rebind the
        # source and require the consumer to follow.
        # Against sys.modules["mlb_runtime_policy"], NOT the `scripts.` alias:
        # the repo imports this file under both names, so those are two module
        # objects and `is` between them fails for reasons that have nothing to
        # do with the property under test. The bare one is what
        # mlb_probability_model actually imported.
        bare = sys.modules["mlb_runtime_policy"]
        self.assertIs(
            mlb_probability_model.MARKET_MODEL_VERSION, bare.MARKET_MODEL_VERSION
        )
        with mock.patch.object(bare, "MARKET_MODEL_VERSION", "vig-mlb-renamed-v9"):
            reloaded = importlib.reload(sys.modules["mlb_probability_model"])
            try:
                self.assertEqual(reloaded.MARKET_MODEL_VERSION, "vig-mlb-renamed-v9")
            finally:
                importlib.reload(sys.modules["mlb_probability_model"])


class ExecutionBoundaryTests(unittest.TestCase):
    """The rail has to be reached from the paths that place orders."""

    def test_the_execution_gate_calls_the_deployment_rail(self):
        # Mutating the CALLEE proves the rail works; this asserts the CALL
        # SITE exists, which is the half that was missing for a month.
        candidate = {"model_version": "vig-mlb-elo-v3"}
        with mock.patch.object(
            mlb_execution_gate, "model_deployment_errors", return_value=["refused"]
        ) as rail:
            mlb_execution_gate.candidate_is_eligible(candidate, _now())
        # Called only if the earlier trail checks did not already return; use
        # a full candidate so the rail is genuinely reached.
        rail.reset_mock()
        with mock.patch.object(
            mlb_execution_gate, "stale_probability_field_errors", return_value=[]
        ), mock.patch.object(
            mlb_execution_gate, "model_deployment_errors", return_value=["refused"]
        ) as rail:
            self.assertFalse(
                mlb_execution_gate.candidate_is_eligible(_eligible_candidate(), _now())
            )
            rail.assert_called()

    def test_deleting_the_gate_call_site_changes_the_verdict(self):
        # The red-run in the other direction: with the rail returning nothing,
        # the same candidate must survive this check, so the assertion above
        # is not passing for some unrelated reason.
        with mock.patch.object(
            mlb_execution_gate, "stale_probability_field_errors", return_value=[]
        ), mock.patch.object(
            mlb_execution_gate, "model_deployment_errors", return_value=[]
        ), mock.patch.object(
            mlb_execution_gate, "baseball_evidence_errors", return_value=["stop here"]
        ):
            # Reaching the NEXT gate proves the deployment rail let it past.
            self.assertFalse(
                mlb_execution_gate.candidate_is_eligible(_eligible_candidate(), _now())
            )

    def test_the_final_lock_refuses_an_undeployed_version(self):
        from scripts import execution_guard

        candidate = dict(_eligible_candidate(), execution_mode="standing_authorized")
        with mock.patch.object(
            execution_guard, "stale_probability_field_errors", return_value=[]
        ), mock.patch.object(
            execution_guard, "model_deployment_errors", return_value=["not deployed"]
        ), mock.patch.object(
            execution_guard, "load_mlb_selection_policy"
        ) as policy, mock.patch.object(
            execution_guard, "RISK_LIMITS_PATH", _empty_limits_path(self)
        ):
            policy.return_value = mock.Mock(min_conservative_edge=0.05)
            violation = execution_guard._risk_limit_violation(candidate, None, _now())
        self.assertIsNotNone(violation)
        self.assertIn("model deployment violation", violation)


class ProbabilityChainReportTests(unittest.TestCase):
    """Substitution is decided by the numbers, never by the label."""

    def test_dk_fair_at_a_zero_haircut_is_named_a_substitution(self):
        entry = read(
            raw_probability={"away": 0.398, "home": 0.602},
            conservative_probability={"away": 0.398, "home": 0.602},
            uncertainty_haircut=0.0,
        )
        row = mlb_probability_chain_report.classify_read(entry)
        self.assertEqual(
            row["classification"], mlb_probability_chain_report.CLASS_SUBSTITUTION
        )

    def test_a_departing_raw_probability_is_an_independent_handicap(self):
        row = mlb_probability_chain_report.classify_read(read())
        self.assertEqual(
            row["classification"], mlb_probability_chain_report.CLASS_INDEPENDENT
        )

    def test_the_label_never_decides_the_classification(self):
        # A read tagged market-only whose numbers are its own is NOT a
        # market-only read, and the report has to say so — this is the same
        # error as classifying a refusal by which fields are present rather
        # than by what they say.
        mislabelled = read(model_version="vig-mlb-market-v1")
        row = mlb_probability_chain_report.classify_read(mislabelled)
        self.assertEqual(
            row["classification"], mlb_probability_chain_report.CLASS_INDEPENDENT
        )
        self.assertFalse(row["label_agrees_with_numbers"])

        substituted_but_labelled_otherwise = read(
            raw_probability={"away": 0.398, "home": 0.602},
            conservative_probability={"away": 0.398, "home": 0.602},
            uncertainty_haircut=0.0,
            model_version="vig-mlb-elo-v3",
        )
        row = mlb_probability_chain_report.classify_read(
            substituted_but_labelled_otherwise
        )
        self.assertEqual(
            row["classification"], mlb_probability_chain_report.CLASS_SUBSTITUTION
        )
        self.assertFalse(row["label_agrees_with_numbers"])

    def test_one_substituted_side_is_not_a_substitution(self):
        # Both sides, or it is a handicap that happens to agree on one team.
        entry = read(
            raw_probability={"away": 0.398, "home": 0.650},
            conservative_probability={"away": 0.398, "home": 0.650},
            uncertainty_haircut=0.0,
        )
        self.assertEqual(
            mlb_probability_chain_report.classify_read(entry)["classification"],
            mlb_probability_chain_report.CLASS_INDEPENDENT,
        )

    def test_a_nonzero_haircut_on_equal_numbers_is_not_a_substitution(self):
        # conservative departs from dk_fair even though raw does not, so our
        # executable number is not the book's.
        entry = read(
            raw_probability={"away": 0.398, "home": 0.602},
            conservative_probability={"away": 0.378, "home": 0.582},
            uncertainty_haircut=0.02,
        )
        self.assertEqual(
            mlb_probability_chain_report.classify_read(entry)["classification"],
            mlb_probability_chain_report.CLASS_INDEPENDENT,
        )

    def test_a_missing_field_is_indeterminate_not_folded_into_either_class(self):
        entry = read()
        entry.pop("uncertainty_haircut")
        row = mlb_probability_chain_report.classify_read(entry)
        self.assertEqual(
            row["classification"], mlb_probability_chain_report.CLASS_INDETERMINATE
        )
        self.assertIn("uncertainty_haircut", row["basis"])

    def test_a_game_with_no_trail_is_unhandicapped(self):
        entry = read(disposition="not_priced")
        for field in ("raw_probability", "uncertainty_haircut",
                      "conservative_probability", "model_version"):
            entry.pop(field, None)
        self.assertEqual(
            mlb_probability_chain_report.classify_read(entry)["classification"],
            mlb_probability_chain_report.CLASS_UNHANDICAPPED,
        )

    def test_the_report_consults_the_real_execution_rail(self):
        # Not a second opinion about eligibility — the same function the money
        # gates call, so the report cannot drift into disagreeing with them.
        row = mlb_probability_chain_report.classify_read(
            read(model_version="vig-mlb-elo-v3")
        )
        self.assertFalse(row["execution_eligible_version"])
        self.assertTrue(
            mlb_probability_chain_report.classify_read(read())[
                "execution_eligible_version"
            ]
        )

    def test_counts_are_zero_filled_over_the_closed_class_set(self):
        report = mlb_probability_chain_report.build_report(
            [("d.json", {"game_reads": [read()]})]
        )
        self.assertEqual(
            set(report["counts"]), set(mlb_probability_chain_report.CLASSES)
        )
        self.assertEqual(report["counts"][mlb_probability_chain_report.CLASS_SUBSTITUTION], 0)

    def test_a_day_with_no_reads_is_named_not_dropped(self):
        report = mlb_probability_chain_report.build_report(
            [("2026-09-01-schedule.json", {"date": "2026-09-01"})]
        )
        self.assertEqual(report["reads"], 0)
        self.assertEqual(len(report["skipped"]), 1)
        self.assertIn("2026-09-01", report["skipped"][0])


class ReviewGateRecorderGapTests(unittest.TestCase):
    """The scheduled run must be able to tell a refused card from an unrecorded one."""

    def setUp(self):
        import contextlib

        from scripts import vig_review_gate_common

        self.gate = vig_review_gate_common
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        original = self.gate.ROOT
        self.gate.ROOT = self.root
        self.addCleanup(lambda: setattr(self.gate, "ROOT", original))
        # The gate's OWN day function, never a second clock call: two now()
        # calls straddling Chicago midnight write one file and read another.
        self.day = self.gate.schedule_day_now()
        self.schedule_path = self.root / ".picks" / "execute" / f"{self.day}-schedule.json"
        self.schedule_path.parent.mkdir(parents=True)
        self.scan_path = self.root / ".picks" / "tmp" / f"stage2-{self.day}.json"
        self.scan_path.parent.mkdir(parents=True)

    def _run(self, payload, scan=None, child=None):
        """One gate cycle. `scan` is the independent artifact, written or not.

        `child` replaces subprocess.run, which is how a case reaches the
        has-work branch without spawning a reviewer: the notice is emitted
        before the child is invoked, so any child outcome exercises it.
        """
        import contextlib
        import io

        self.schedule_path.write_text(json.dumps(payload), encoding="utf-8")
        if scan is not None:
            self.scan_path.write_text(json.dumps(scan), encoding="utf-8")
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            if child is not None:
                stack.enter_context(
                    mock.patch.object(self.gate.subprocess, "run", side_effect=child)
                )
            stack.enter_context(
                mock.patch.object(
                    self.gate, "standing_authorization_enabled", return_value=True
                )
            )
            stack.enter_context(contextlib.redirect_stdout(out))
            status = self.gate.run_gate("MLB")
        return status, out.getvalue()

    def _journal(self):
        from scripts import vig_run_journal

        records, _errors = vig_run_journal.read_records(
            vig_run_journal.journal_path(self.root, self.day)
        )
        return records

    def test_an_unrecorded_empty_day_is_not_journalled_as_plain_no_work(self):
        # THE 2026-09-01 SHAPE. Before this, a schedule with fifteen refused
        # games and no game_reads produced the identical `no_reviewable_work`
        # record as a day with no card at all.
        status, output = self._run(
            {"date": self.day, "candidates": [], "lineup_watchlist": []}
        )
        self.assertEqual(status, 0)
        stages = [record.get("stage") for record in self._journal()]
        self.assertIn(self.gate.RECORDER_GAP_STAGE, stages)
        self.assertNotIn("no_reviewable_work", stages)
        self.assertIn(self.gate.RECORDER_GAP_NOTICE_PREFIX, output)

    def test_a_properly_recorded_empty_day_stays_plain_no_work(self):
        # The contrast case. Without it, the test above would pass for a gate
        # that shouted on every quiet day, which is a different bug.
        status, output = self._run(schedule([], date=self.day), scan=[])
        self.assertEqual(status, 0)
        stages = [record.get("stage") for record in self._journal()]
        self.assertIn("no_reviewable_work", stages)
        self.assertNotIn(self.gate.RECORDER_GAP_STAGE, stages)
        self.assertNotIn(self.gate.RECORDER_GAP_NOTICE_PREFIX, output)

    def test_the_notice_is_printed_once_a_day_not_every_cycle(self):
        # Ninety-six identical warnings is how the stuck watchlist entry
        # taught this lane that a repeating alarm is the same as no alarm.
        payload = {"date": self.day, "candidates": [], "lineup_watchlist": []}
        _first_status, first = self._run(payload)
        _second_status, second = self._run(payload)
        self.assertIn(self.gate.RECORDER_GAP_NOTICE_PREFIX, first)
        self.assertNotIn(self.gate.RECORDER_GAP_NOTICE_PREFIX, second)
        # ...and the journal still carries it on BOTH cycles: stdout is the
        # notification, disk is the record.
        gaps = [
            record for record in self._journal()
            if record.get("stage") == self.gate.RECORDER_GAP_STAGE
        ]
        self.assertEqual(len(gaps), 2)

    def test_an_unreadable_journal_reprints_rather_than_going_silent(self):
        payload = {"date": self.day, "candidates": [], "lineup_watchlist": []}
        self._run(payload)
        with mock.patch.object(self.gate, "read_records", side_effect=OSError("boom")):
            _status, output = self._run(payload)
        self.assertIn(self.gate.RECORDER_GAP_NOTICE_PREFIX, output)

    def test_the_scheduled_gate_catches_a_self_consistently_trimmed_slate(self):
        # PR #77 review, blocker 1. This is the failure the cross-check was
        # written for and the one the SCHEDULED run could not see: a slate
        # that cut `game_reads` and `slate_denominator` to the same short set
        # agrees with itself perfectly. The schedule-only validator passes it
        # and the gate journals `no_reviewable_work` — bit for bit the record
        # the 09-01 run produced, reached a different way. Only the scan, which
        # this run did not write, is an independent witness.
        kept, dropped = read(823509), read(824876)
        trimmed = schedule([kept], date=self.day)
        # The premise, asserted rather than assumed: without the scan there is
        # nothing here to find. If this ever stops holding the test below is
        # passing for the wrong reason.
        self.assertEqual(mlb_game_reads.validate_game_reads(trimmed), [])

        status, output = self._run(trimmed, scan=scan_rows([kept, dropped]))

        self.assertEqual(status, 0)
        self.assertIn(self.gate.RECORDER_GAP_NOTICE_PREFIX, output)
        self.assertIn("824876", output)
        stages = [record.get("stage") for record in self._journal()]
        self.assertIn(self.gate.RECORDER_GAP_STAGE, stages)
        self.assertNotIn("no_reviewable_work", stages)

    def test_the_scheduled_gate_reports_a_day_nobody_scanned(self):
        # "Nobody ran the scan" and "the scan agrees" must not look alike from
        # the gate either — that is the same collapse one level up, and it is
        # what makes the check above unfakeable by simply not scanning.
        status, output = self._run(schedule([], date=self.day))
        self.assertEqual(status, 0)
        self.assertIn(self.gate.RECORDER_GAP_NOTICE_PREFIX, output)
        self.assertIn("denominator scan not readable", output)

    def _has_work_payload(self):
        # A card with review work on it AND no per-game record. `candidates`
        # is what carries the gate past the empty-card early return, which is
        # the only route to the second report site.
        return {
            "date": self.day,
            "candidates": [{"event_id": "1", "side": "CWS"}],
            "lineup_watchlist": [],
        }

    def test_a_day_with_review_work_reports_its_recorder_gap_too(self):
        # PR #77 review, blocker 2. Deleting the whole `if recorder_errors:`
        # block below the early return left the full suite byte-identical:
        # every case here exited above it on an empty card. A day WITH work
        # can be exactly as unrecorded, and it is the busy days whose refusals
        # the dataset most wants.
        status, output = self._run(
            self._has_work_payload(), child=OSError("no reviewer child in tests")
        )
        # The child failure is what it is; the recorder notice is what this
        # asserts, and it is emitted before the child is ever invoked.
        self.assertEqual(status, 1)
        self.assertIn(self.gate.RECORDER_GAP_NOTICE_PREFIX, output)
        records = self._journal()
        stages = [record.get("stage") for record in records]
        # The justification for keying the throttle on the notice PREFIX
        # rather than the journal stage, made checkable: this branch never
        # journals `recorder_missing`. A throttle keyed on the stage would
        # look throttled here and print on all ninety-six cycles.
        self.assertNotIn(self.gate.RECORDER_GAP_STAGE, stages)
        carried = [
            text
            for record in records
            for text in (record.get("notices") or [])
            if isinstance(text, str)
            and text.startswith(self.gate.RECORDER_GAP_NOTICE_PREFIX)
        ]
        self.assertEqual(len(carried), 1, records)

    def test_the_has_work_notice_is_throttled_on_the_second_cycle(self):
        # And it is throttled THROUGH the notices list, since no
        # `recorder_missing` stage is ever written on this path.
        payload = self._has_work_payload()
        child = OSError("no reviewer child in tests")
        _first_status, first = self._run(payload, child=child)
        _second_status, second = self._run(payload, child=child)
        self.assertIn(self.gate.RECORDER_GAP_NOTICE_PREFIX, first)
        self.assertNotIn(self.gate.RECORDER_GAP_NOTICE_PREFIX, second)
        # Disk still carries it on both cycles: stdout is the notification,
        # the journal is the record.
        carried = [
            text
            for record in self._journal()
            for text in (record.get("notices") or [])
            if isinstance(text, str)
            and text.startswith(self.gate.RECORDER_GAP_NOTICE_PREFIX)
        ]
        self.assertEqual(len(carried), 2)

    def test_a_recorded_day_with_review_work_stays_quiet(self):
        # The contrast the has-work cases need: without it they would pass for
        # a gate that shouts on every busy day.
        entry = read()
        payload = schedule(
            # A game we TOOK refuses nothing, so the rails come off with the
            # disposition — the coherence check is right to insist.
            [dict(entry, disposition="candidate", refusing_rails=[])],
            date=self.day,
            candidates=[{"event_id": "1", "side": "CWS"}],
        )
        _status, output = self._run(
            payload,
            scan=scan_rows([entry]),
            child=OSError("no reviewer child in tests"),
        )
        self.assertNotIn(self.gate.RECORDER_GAP_NOTICE_PREFIX, output)

    def test_the_gap_never_changes_the_gate_verdict(self):
        # A defect in a measurement artifact must not take the reviewer
        # offline; same asymmetry the journal already holds to.
        status, _output = self._run(
            {"date": self.day, "candidates": [], "lineup_watchlist": []}
        )
        self.assertEqual(status, 0)


def _empty_limits_path(case):
    directory = Path(tempfile.mkdtemp())
    case.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
    path = directory / "risk_limits.json"
    path.write_text("{}", encoding="utf-8")
    return path


def _now():
    from datetime import datetime, timezone

    return datetime(2026, 9, 1, 22, 0, tzinfo=timezone.utc)


def _eligible_candidate():
    return {
        "first_pitch_utc": "2026-09-01T22:40:00+00:00",
        "polymarket_slug": "aec-mlb-atl-mil-2026-09-01",
        "side": "ATL",
        "max_polymarket_price": 0.55,
        "model_version": "vig-mlb-elo-v3",
        "unit_size": 20,
        "conservative_probability": 0.62,
        "current_ask": 0.55,
        "projected_edge_at_current_ask": 0.07,
    }


class WriterProvenanceTests(unittest.TestCase):
    """Whether the record names the bytes it was built from — and what that is worth.

    This is DIAGNOSIS, not a rail, and the tests are written to hold that line.
    A hand-authored schedule can copy a correct digest exactly as easily as a
    correct roster, so ``corroborated`` is evidence of a consistent record and
    not proof of a code path; the load-bearing value is ``absent``, which a
    bypass cannot avoid without going to the trouble of forging one. The rule
    this lane keeps re-learning is that a guard keyed on a field the producer
    writes is measuring the producer's copy of itself — so the verdict stays
    keyed on content validation and provenance is reported beside it.
    """

    def setUp(self):
        import contextlib

        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.rt = Runtime(self.stack)
        self.reads = [read(823509), read(824876)]

    def _land(self):
        """Land through the writer, the way the supported path does it."""
        self.rt.write_scan(scan_rows(self.reads))
        draft = {
            "date": self.rt.day,
            "sport": "MLB",
            "market_type": "moneyline",
            "candidates": [],
            "lineup_watchlist": [],
            "game_reads": self.reads,
        }
        return mlb_slate_writer.land(self.rt.root, self.rt.day, draft)

    def test_a_landed_schedule_names_the_scan_bytes_it_derived_the_roster_from(self):
        _path, schedule = self._land()
        digest = hashlib.sha256(self.rt.scan_path.read_bytes()).hexdigest()
        self.assertEqual(schedule["slate_denominator"]["scan_sha256"], digest)
        receipt = mlb_slate_receipt.build_receipt(self.rt.root, self.rt.day)
        self.assertEqual(
            receipt["writer_provenance"], mlb_slate_receipt.PROVENANCE_CORROBORATED
        )
        self.assertEqual(receipt["scan_sha256_actual"], digest)
        self.assertEqual(receipt["verdict"], mlb_slate_receipt.VERDICT_COMPLETE)

    def test_a_hand_authored_schedule_is_absent_not_corroborated(self):
        # The 2026-09-04 route: the schedule written directly, skipping the
        # writer. Absence is the half that cannot be produced by accident, and
        # it must never read as agreement.
        self.rt.write_schedule(schedule(self.reads, date=self.rt.day))
        self.rt.write_scan(scan_rows(self.reads))
        receipt = mlb_slate_receipt.build_receipt(self.rt.root, self.rt.day)
        self.assertEqual(
            receipt["writer_provenance"], mlb_slate_receipt.PROVENANCE_ABSENT
        )
        self.assertIsNone(receipt["scan_sha256_recorded"])
        # And the record is otherwise perfectly valid: the bypass is visible
        # here and NOWHERE in the verdict, which is the intended split.
        self.assertEqual(receipt["verdict"], mlb_slate_receipt.VERDICT_COMPLETE)

    def test_a_scan_rewritten_after_landing_contradicts_the_recorded_digest(self):
        # The denominator was derived from bytes a later reader will not find.
        # The cross-check still runs — against different evidence than the one
        # the roster came from, which is the fact worth surfacing.
        self._land()
        rows = scan_rows(self.reads)
        rows[0] = dict(rows[0], away="Someone Else")
        self.rt.write_scan(rows)
        receipt = mlb_slate_receipt.build_receipt(self.rt.root, self.rt.day)
        self.assertEqual(
            receipt["writer_provenance"], mlb_slate_receipt.PROVENANCE_CONTRADICTED
        )
        self.assertNotEqual(
            receipt["scan_sha256_recorded"], receipt["scan_sha256_actual"]
        )

    def test_a_reformatted_scan_contradicts_even_with_an_identical_roster(self):
        # The digest is over BYTES, not over the parsed roster: re-serialising
        # the same games with different whitespace is a different artifact, and
        # a check that called this corroborated would be asserting something
        # weaker than it claims.
        self._land()
        rows = json.loads(self.rt.scan_path.read_text(encoding="utf-8"))
        self.rt.scan_path.write_text(json.dumps(rows, indent=4), encoding="utf-8")
        receipt = mlb_slate_receipt.build_receipt(self.rt.root, self.rt.day)
        self.assertEqual(
            receipt["writer_provenance"], mlb_slate_receipt.PROVENANCE_CONTRADICTED
        )

    def test_a_missing_scan_is_unverifiable_and_never_contradicted(self):
        # "The digest is wrong" and "the digest could not be judged" are
        # different facts, and only one of them accuses the record. The missing
        # scan is already reported as an error by check_denominator; this field
        # must not report it a second time as a contradiction.
        self._land()
        self.rt.scan_path.unlink()
        receipt = mlb_slate_receipt.build_receipt(self.rt.root, self.rt.day)
        self.assertEqual(
            receipt["writer_provenance"], mlb_slate_receipt.PROVENANCE_UNVERIFIABLE
        )
        self.assertIsNone(receipt["scan_sha256_actual"])

    def test_a_non_string_digest_is_contradicted_rather_than_absent(self):
        # A garbage value is not an absence. Reading it as one would let a
        # record that claims provenance and cannot support it look exactly like
        # a record that never claimed any.
        self._land()
        payload = json.loads(self.rt.schedule_path.read_text(encoding="utf-8"))
        payload["slate_denominator"]["scan_sha256"] = {"sha256": "nope"}
        self.rt.write_schedule(payload)
        receipt = mlb_slate_receipt.build_receipt(self.rt.root, self.rt.day)
        self.assertEqual(
            receipt["writer_provenance"], mlb_slate_receipt.PROVENANCE_CONTRADICTED
        )

    def test_provenance_is_not_an_input_to_the_verdict(self):
        # The property that keeps this diagnosis: strip the digest, or corrupt
        # it, and the verdict and the error list must be byte-identical. If
        # provenance ever starts moving the verdict, this is the test that says
        # the docstring stopped being true.
        _path, landed = self._land()
        with_digest = mlb_slate_receipt.build_receipt(self.rt.root, self.rt.day)

        stripped = json.loads(json.dumps(landed))
        stripped["slate_denominator"].pop("scan_sha256")
        self.rt.write_schedule(stripped)
        without = mlb_slate_receipt.build_receipt(self.rt.root, self.rt.day)

        corrupted = json.loads(json.dumps(landed))
        corrupted["slate_denominator"]["scan_sha256"] = "0" * 64
        self.rt.write_schedule(corrupted)
        wrong = mlb_slate_receipt.build_receipt(self.rt.root, self.rt.day)

        # Three different provenances, so the fixture genuinely varies the field
        # the assertion is about — a same-provenance triple would prove nothing.
        self.assertEqual(
            [r["writer_provenance"] for r in (with_digest, without, wrong)],
            [
                mlb_slate_receipt.PROVENANCE_CORROBORATED,
                mlb_slate_receipt.PROVENANCE_ABSENT,
                mlb_slate_receipt.PROVENANCE_CONTRADICTED,
            ],
        )
        for other in (without, wrong):
            self.assertEqual(other["verdict"], with_digest["verdict"])
            self.assertEqual(other["recorder_errors"], with_digest["recorder_errors"])
            self.assertEqual(
                other["scheduled_games"], with_digest["scheduled_games"]
            )

    def test_every_provenance_is_in_the_closed_vocabulary(self):
        self.rt.write_schedule(schedule(self.reads, date=self.rt.day))
        self.rt.write_scan(scan_rows(self.reads))
        receipt = mlb_slate_receipt.build_receipt(self.rt.root, self.rt.day)
        self.assertIn(receipt["writer_provenance"], mlb_slate_receipt.PROVENANCES)

    def test_the_digest_is_of_the_bytes_the_roster_was_parsed_from(self):
        # Not of a second read of the path. A writer that hashed the file again
        # after parsing it would certify whatever the file said a moment later,
        # which is exactly the substitution the digest exists to rule out.
        raw = json.dumps(scan_rows(self.reads)).encode("utf-8")
        replacement = json.dumps(scan_rows([read(999999)])).encode("utf-8")
        self.rt.scan_path.write_bytes(raw)
        real_loads = json.loads

        def parse_then_replace(payload):
            rows = real_loads(payload)
            self.rt.scan_path.write_bytes(replacement)
            return rows

        with mock.patch.object(
            mlb_slate_writer.json, "loads", side_effect=parse_then_replace
        ):
            rows, digest = mlb_slate_writer.load_scan(self.rt.scan_path)

        self.assertEqual(len(rows), len(self.reads))
        self.assertEqual(digest, hashlib.sha256(raw).hexdigest())
        self.assertNotEqual(digest, hashlib.sha256(replacement).hexdigest())


if __name__ == "__main__":
    unittest.main()
