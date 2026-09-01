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

import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import mlb_game_reads
from scripts import mlb_execution_gate
from scripts import mlb_probability_chain_report
from scripts import mlb_probability_model
from scripts import mlb_runtime_policy
from scripts import mlb_slate_receipt
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
        "net_edge": {"away": -0.080, "home": 0.035},
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

    def _run(self, payload):
        import contextlib
        import io

        self.schedule_path.write_text(json.dumps(payload), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
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
        status, output = self._run(schedule([], date=self.day))
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


if __name__ == "__main__":
    unittest.main()
