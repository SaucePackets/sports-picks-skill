import json
import unittest
import unittest.mock
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts import vig_drought_diagnostic as diag
from scripts.vig_drought_diagnostic import (
    CANDIDATE_STOPS,
    DAY_CLASSES,
    DiagnosticError,
    build_report,
    date_range,
    parse_date,
)


def schedule_payload(games):
    """A minimal MLB schedule payload: [(away, home, status, a_score, h_score, winner)]."""
    return {
        "dates": [
            {
                "games": [
                    {
                        "gamePk": 900000 + index,
                        "status": {"detailedState": status},
                        "teams": {
                            "away": {
                                "team": {"name": away},
                                "score": a_score,
                                "isWinner": winner == away,
                            },
                            "home": {
                                "team": {"name": home},
                                "score": h_score,
                                "isWinner": winner == home,
                            },
                        },
                    }
                    for index, (away, home, status, a_score, h_score, winner) in enumerate(games)
                ]
            }
        ]
    }


def candidate(**overrides):
    base = {
        # Deliberately NOT the gamePk: the real slate records an id from a
        # different space, and the outcome join must not depend on it.
        "event_id": "401816733",
        "game": "Colorado Rockies at Atlanta Braves",
        "side": "Atlanta Braves",
        "net_edge": 0.055,
        "polymarket_ask": 0.685,
        "execution_mode": "standing_authorized",
        "vig_approved": False,
        "vig_review_needed": True,
        "executed": False,
    }
    base.update(overrides)
    return base


def watchlist_entry(**overrides):
    base = {
        "id": "LW-20260811-MIL-SD",
        "game": "Milwaukee Brewers at San Diego Padres",
        "status": "pending_lineup_recheck",
        "blocked_only_by": ["lineups_unconfirmed"],
    }
    base.update(overrides)
    return base


class Corpus:
    """A .picks directory built one day at a time."""

    def __init__(self, root: Path):
        self.root = root
        for name in ("execute", "audit-results", "slate"):
            (root / name).mkdir(parents=True, exist_ok=True)

    def day(self, date, *, candidates=None, watchlist=None, games=None, writeup=True):
        if candidates is not None or watchlist is not None:
            (self.root / "execute" / f"{date}-schedule.json").write_text(
                json.dumps(
                    {
                        "date": date,
                        "sport": "MLB",
                        "market_type": "moneyline",
                        "candidates": candidates or [],
                        "lineup_watchlist": watchlist or [],
                    }
                )
            )
        if games is not None:
            (self.root / "audit-results" / f"{date}.json").write_text(
                json.dumps(schedule_payload(games))
            )
        if writeup:
            (self.root / "slate" / f"{date}.md").write_text(f"# MLB Slate — {date}\n")
        return self


class WindowCoverageTests(unittest.TestCase):
    def test_every_date_in_the_window_appears_including_silent_ones(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            Corpus(root).day("2026-08-11", candidates=[], watchlist=[], games=[])
            report = build_report(
                picks_dir=root,
                since=parse_date("2026-08-11"),
                until=parse_date("2026-08-15"),
            )
        dates = [day["date"] for day in report["days"]]
        self.assertEqual(
            dates,
            ["2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14", "2026-08-15"],
        )
        self.assertEqual(report["window"]["days"], 5)

    def test_a_day_with_no_artifact_is_not_the_same_as_an_empty_slate(self):
        # The whole point of the report. An empty slate is a scan that ran and
        # found nothing; a missing artifact is a scan that left no trace. They
        # have different fixes and must never share a bucket.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            Corpus(root).day("2026-08-11", candidates=[], watchlist=[], games=[])
            report = build_report(
                picks_dir=root,
                since=parse_date("2026-08-11"),
                until=parse_date("2026-08-12"),
            )
        by_date = {day["date"]: day for day in report["days"]}
        self.assertEqual(by_date["2026-08-11"]["day_class"], "slate_empty")
        self.assertEqual(by_date["2026-08-12"]["day_class"], "no_slate_artifact")

    def test_a_writeup_alone_still_counts_as_the_scan_having_run(self):
        # A slate writeup with no schedule JSON is evidence the job ran. Calling
        # that "no artifact" would blame the scheduler for an empty slate.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            Corpus(root).day("2026-08-11", writeup=True)
            report = build_report(
                picks_dir=root,
                since=parse_date("2026-08-11"),
                until=parse_date("2026-08-11"),
            )
        self.assertEqual(report["days"][0]["day_class"], "slate_empty")

    def test_a_missing_schedule_reports_None_games_not_zero(self):
        # None means "no evidence about games"; 0 means "no games". A reader who
        # cannot tell them apart reads a fetch outage as an off-day.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            Corpus(root).day("2026-08-11", candidates=[], watchlist=[])
            Corpus(root).day("2026-08-12", candidates=[], watchlist=[], games=[])
            report = build_report(
                picks_dir=root,
                since=parse_date("2026-08-11"),
                until=parse_date("2026-08-12"),
            )
        by_date = {day["date"]: day for day in report["days"]}
        self.assertIsNone(by_date["2026-08-11"]["games_scheduled"])
        self.assertEqual(by_date["2026-08-12"]["games_scheduled"], 0)
        kinds = {gap["kind"] for gap in report["data_gaps"]}
        self.assertIn("no_cached_mlb_schedule", kinds)


class DayClassAndStopTests(unittest.TestCase):
    def _report(self, **day_kwargs):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            Corpus(root).day("2026-08-30", games=[], **day_kwargs)
            return build_report(
                picks_dir=root,
                since=parse_date("2026-08-30"),
                until=parse_date("2026-08-30"),
            )

    def test_every_value_of_vig_approved_gets_its_own_stop(self):
        # A tri-state signal needs a classifier arm AND a test per value. PR #64
        # regressed on exactly this: `vig_approved` was read for its polarity
        # and only the True arm was ever exercised.
        for approved, executed, expected in (
            (False, False, "review_gate_rejected"),
            (True, False, "approved_not_executed"),
            (True, True, "executed"),
            (None, False, "unknown"),
        ):
            with self.subTest(vig_approved=approved, executed=executed):
                report = self._report(
                    candidates=[candidate(vig_approved=approved, executed=executed)],
                    watchlist=[],
                )
                self.assertEqual(report["days"][0]["candidates"][0]["stop"], expected)

    def test_day_classes_and_stops_are_zero_filled_over_the_closed_set(self):
        # A class that never occurred must print 0, not vanish. A missing key
        # and a zero read the same to a skimmer and mean opposite things.
        report = self._report(candidates=[candidate()], watchlist=[])
        self.assertEqual(set(report["aggregates"]["day_classes"]), set(DAY_CLASSES))
        self.assertEqual(set(report["aggregates"]["candidate_stops"]), set(CANDIDATE_STOPS))
        self.assertEqual(report["aggregates"]["day_classes"]["candidates_executed"], 0)
        self.assertEqual(report["aggregates"]["candidate_stops"]["executed"], 0)

    def test_watchlist_without_candidates_is_watchlist_only(self):
        report = self._report(candidates=[], watchlist=[watchlist_entry()])
        self.assertEqual(report["days"][0]["day_class"], "watchlist_only")

    def test_candidates_outrank_watchlist_in_the_day_class(self):
        report = self._report(candidates=[candidate()], watchlist=[watchlist_entry()])
        self.assertEqual(report["days"][0]["day_class"], "candidates_rejected")


class OutcomeTests(unittest.TestCase):
    """Outcomes are reported, and never allowed to colour the process verdict."""

    GAMES = [("Colorado Rockies", "Atlanta Braves", "Final", 2, 3, "Atlanta Braves")]

    def _report(self, games, **overrides):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            Corpus(root).day(
                "2026-08-30",
                candidates=[candidate(**overrides)],
                watchlist=[],
                games=games,
            )
            return build_report(
                picks_dir=root,
                since=parse_date("2026-08-30"),
                until=parse_date("2026-08-30"),
            )

    def test_the_outcome_join_survives_an_event_id_from_another_id_space(self):
        # The real corpus records event_id 401816733 for the game the schedule
        # calls gamePk 824876. Joining on the id matches nothing and reads as
        # "no outcome data" when it means "wrong key".
        trace = self._report(self.GAMES)["days"][0]["candidates"][0]
        self.assertNotEqual(str(trace["event_id"]), "900000")
        self.assertTrue(trace["outcome_known"])
        self.assertEqual(trace["outcome_winner"], "Atlanta Braves")
        self.assertTrue(trace["outcome_side_won"])

    def test_passing_on_a_winner_is_still_recorded_as_a_gate_rejection(self):
        # The separation that keeps this report honest: the chosen side won and
        # the process verdict is unchanged. A pass on a winner is not thereby a
        # mistake, and hindsight must not rewrite the stop.
        trace = self._report(self.GAMES)["days"][0]["candidates"][0]
        self.assertTrue(trace["outcome_side_won"])
        self.assertEqual(trace["stop"], "review_gate_rejected")

    def test_a_cached_payload_taken_before_the_final_says_so(self):
        # Not a missing outcome — a stale snapshot. The live corpus's 08-30
        # payload was cached at 19:42Z with eleven games in progress.
        in_progress = [
            ("Colorado Rockies", "Atlanta Braves", "In Progress", None, None, None)
        ]
        trace = self._report(in_progress)["days"][0]["candidates"][0]
        self.assertFalse(trace["outcome_known"])
        self.assertIn("In Progress", trace["outcome_reason"])
        self.assertIn("cached before", trace["outcome_reason"])

    def test_a_matchup_absent_from_the_schedule_says_which_matchup(self):
        elsewhere = [("Miami Marlins", "Washington Nationals", "Final", 6, 2, "Miami Marlins")]
        trace = self._report(elsewhere)["days"][0]["candidates"][0]
        self.assertFalse(trace["outcome_known"])
        self.assertIn("Colorado Rockies at Atlanta Braves", trace["outcome_reason"])

    def test_the_default_run_never_reaches_the_network(self):
        # Read-only over the corpus is a property, not an intention. Without the
        # opt-in flag the fetch must not be called even when the cache is stale.
        in_progress = [
            ("Colorado Rockies", "Atlanta Braves", "In Progress", None, None, None)
        ]
        with unittest.mock.patch.object(diag, "refetch_schedule") as fetch:
            self._report(in_progress)
        fetch.assert_not_called()

    def test_a_refetch_failure_is_reported_and_never_fatal(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            Corpus(root).day(
                "2026-08-30",
                candidates=[candidate()],
                watchlist=[],
                games=[("Colorado Rockies", "Atlanta Braves", "In Progress", None, None, None)],
            )
            with unittest.mock.patch.object(
                diag, "refetch_schedule", side_effect=OSError("network down")
            ):
                report = build_report(
                    picks_dir=root,
                    since=parse_date("2026-08-30"),
                    until=parse_date("2026-08-30"),
                    fetch_outcomes=True,
                )
        day = report["days"][0]
        self.assertEqual(day["outcome_source"], "cache")
        self.assertIn("network down", day["outcome_refetch_error"])
        self.assertFalse(day["candidates"][0]["outcome_known"])


class WatchlistValidityTests(unittest.TestCase):
    def _report(self, entry):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            Corpus(root).day("2026-08-11", candidates=[], watchlist=[entry], games=[])
            return build_report(
                picks_dir=root,
                since=parse_date("2026-08-11"),
                until=parse_date("2026-08-11"),
            )

    def test_the_invalid_status_the_live_corpus_actually_carries_is_flagged(self):
        # 2026-08-11's two entries carry status "recheck_complete", which the
        # validator's set does not contain — it appears in this repo ONLY as an
        # invalid test fixture. Same prompt/validator set mismatch that has bitten
        # this fleet twice.
        report = self._report(watchlist_entry(status="recheck_complete"))
        trace = report["days"][0]["watchlist"][0]
        self.assertFalse(trace["status_is_valid"])
        self.assertTrue(trace["validator_errors"])
        kinds = {gap["kind"] for gap in report["data_gaps"]}
        self.assertIn("invalid_watchlist_status", kinds)

    def test_a_valid_status_is_not_flagged(self):
        report = self._report(watchlist_entry(status="passed"))
        self.assertTrue(report["days"][0]["watchlist"][0]["status_is_valid"])
        kinds = {gap["kind"] for gap in report["data_gaps"]}
        self.assertNotIn("invalid_watchlist_status", kinds)

    def test_validity_CALLS_the_real_validator_rather_than_restating_its_set(self):
        # Importing `validate_entry` says nothing about whether this module
        # consults it. Rebind the module's view of the accepted set and require
        # the ANSWER to follow — in both directions, since a one-directional
        # check is satisfied by anything that returns False.
        #
        # A second copy of the status rule would agree with the first only until
        # one of them changed, which is the failure this lane spent PRs #68-#71
        # on. Here the rule lives in mlb_lineup_watchlist and must stay there.
        with unittest.mock.patch.object(diag, "VALID_STATUSES", frozenset()):
            report = self._report(watchlist_entry(status="passed"))
            self.assertFalse(report["days"][0]["watchlist"][0]["status_is_valid"])
        with unittest.mock.patch.object(
            diag, "VALID_STATUSES", frozenset({"recheck_complete"})
        ):
            report = self._report(watchlist_entry(status="recheck_complete"))
            self.assertTrue(report["days"][0]["watchlist"][0]["status_is_valid"])

    def test_validator_errors_come_from_the_shared_validator(self):
        with unittest.mock.patch.object(
            diag, "validate_entry", return_value=["sentinel error"]
        ):
            report = self._report(watchlist_entry())
        self.assertEqual(
            report["days"][0]["watchlist"][0]["validator_errors"], ["sentinel error"]
        )


class ReconciliationTests(unittest.TestCase):
    def test_the_funnel_reconciles_on_a_mixed_window(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus = Corpus(root)
            corpus.day("2026-08-11", candidates=[], watchlist=[watchlist_entry()], games=[])
            corpus.day("2026-08-12", candidates=[], watchlist=[], games=[])
            corpus.day("2026-08-14", candidates=[candidate()], watchlist=[], games=[])
            report = build_report(
                picks_dir=root,
                since=parse_date("2026-08-11"),
                until=parse_date("2026-08-14"),
            )
        recon = report["reconciliation"]
        self.assertTrue(recon["ok"], recon)
        self.assertTrue(all(check["ok"] for check in recon["checks"]))
        agg = report["aggregates"]
        self.assertEqual(sum(agg["day_classes"].values()), 4)
        self.assertEqual(sum(agg["candidate_stops"].values()), agg["total_candidates"])
        self.assertEqual(agg["day_classes"]["no_slate_artifact"], 1)

    def test_reconciliation_reports_a_mismatch_instead_of_hiding_it(self):
        # The checks exist to catch a defect in THIS script. Proving they can
        # go red is what separates them from decoration.
        days = [{"date": "2026-08-11", "counts": {"candidates": 2}, "candidates": []}]
        recon = diag.reconcile(days, {name: 0 for name in DAY_CLASSES},
                               {name: 0 for name in CANDIDATE_STOPS})
        self.assertFalse(recon["ok"])
        failed = [check["check"] for check in recon["checks"] if not check["ok"]]
        self.assertIn("every day is classified exactly once", failed)
        self.assertIn("every candidate has exactly one stop", failed)


class PortablePathTests(unittest.TestCase):
    def test_recorded_source_paths_are_home_relative(self):
        # A committed artifact must not name the machine that produced it.
        # `test_deployed_scripts_and_docs_have_no_baked_in_home` fails on an
        # absolute home path in docs/, and it caught this report's first draft.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            Corpus(root).day("2026-08-11", candidates=[], watchlist=[], games=[])
            report = build_report(
                picks_dir=root,
                since=parse_date("2026-08-11"),
                until=parse_date("2026-08-11"),
            )
        rendered = json.dumps(report)
        self.assertNotIn(str(Path.home()), rendered)

    def test_portable_rewrites_only_the_home_prefix(self):
        home = Path.home()
        self.assertEqual(diag.portable(home / "a" / "b"), "~/a/b")
        self.assertEqual(diag.portable(home), "~")
        self.assertEqual(diag.portable(Path("/opt/elsewhere")), "/opt/elsewhere")
        self.assertIsNone(diag.portable(None))
        # A sibling directory whose name merely STARTS with the home path must
        # not be rewritten: `/Users/jerry-backup` is not inside `/Users/jerry`.
        self.assertEqual(
            diag.portable(Path(str(home) + "-backup")), str(home) + "-backup"
        )


class ArgumentTests(unittest.TestCase):
    def test_an_impossible_calendar_date_is_rejected(self):
        # A YYYY-MM-DD shape check accepts 2026-02-30. Validate as a real date.
        for bad in ("2026-02-30", "2026-13-01", "not-a-date", "2026-8-1x"):
            with self.subTest(value=bad):
                with self.assertRaises(DiagnosticError):
                    parse_date(bad)

    def test_a_backwards_window_is_refused(self):
        with self.assertRaises(DiagnosticError):
            date_range(parse_date("2026-08-31"), parse_date("2026-08-11"))

    def test_a_single_day_window_is_one_day(self):
        self.assertEqual(len(date_range(parse_date("2026-08-11"), parse_date("2026-08-11"))), 1)

    def test_a_missing_ledger_is_a_named_gap_not_a_crash(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            Corpus(root).day("2026-08-11", candidates=[], watchlist=[], games=[])
            report = build_report(
                picks_dir=root,
                since=parse_date("2026-08-11"),
                until=parse_date("2026-08-11"),
                ledger_path=root / "nope.json",
            )
        self.assertIn("ledger", {gap["kind"] for gap in report["data_gaps"]})


if __name__ == "__main__":
    unittest.main()
