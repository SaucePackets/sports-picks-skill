import json
import unittest
import unittest.mock
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts import vig_drought_diagnostic as diag
from scripts.vig_drought_diagnostic import (
    CANDIDATE_STOPS,
    DAY_CLASSES,
    EVIDENCE_ASSIGNABLE_CLASSES,
    RUN_EVIDENCE_SCHEMA,
    DiagnosticError,
    build_report,
    date_range,
    load_run_evidence,
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
        days = [
            {
                "date": "2026-08-11",
                "counts": {"candidates": 2},
                "candidates": [],
                "roots_with_files": [],
                "run_evidence": None,
            }
        ]
        recon = diag.reconcile(days, {name: 0 for name in DAY_CLASSES},
                               {name: 0 for name in CANDIDATE_STOPS})
        self.assertFalse(recon["ok"])
        failed = [check["check"] for check in recon["checks"] if not check["ok"]]
        self.assertIn("every day is classified exactly once", failed)
        self.assertIn("every candidate has exactly one stop", failed)
        self.assertIn(
            "days with no file in any root are classed by the no-artifact split", failed
        )

    def test_reconciliation_catches_evidence_applied_over_an_artifact(self):
        # The precedence check must be able to go red, or it is a decoration
        # that reads as a guarantee. A day with files AND an applied verdict is
        # exactly the state the classifier is forbidden to produce.
        days = [
            {
                "date": "2026-08-20",
                "counts": {"candidates": 0},
                "candidates": [],
                "roots_with_files": ["sports-picks-skill"],
                "run_evidence": {"verdict": "job_never_fired", "applied": True},
            }
        ]
        recon = diag.reconcile(
            days,
            {**{name: 0 for name in DAY_CLASSES}, "slate_empty": 1},
            {name: 0 for name in CANDIDATE_STOPS},
        )
        self.assertFalse(recon["ok"])
        self.assertIn(
            "applied run evidence only ever explains a day with no files",
            [check["check"] for check in recon["checks"] if not check["ok"]],
        )


def run_evidence(dates, **extra):
    payload = {"schema": RUN_EVIDENCE_SCHEMA, "dates": dates}
    payload.update(extra)
    return payload


def evidence_entry(verdict, quote="a verbatim log line"):
    return {
        "verdict": verdict,
        "basis": "why this verdict follows",
        "receipts": [{"source": "~/.hermes/profiles/vig/logs/agent.log.3", "quote": quote}],
    }


class NoArtifactSplitTests(unittest.TestCase):
    """The three-way split of what used to be one `no_slate_artifact` bucket.

    A day with no files looks identical from a directory listing whether the job
    never fired or ran and never reached the write. They have entirely different
    fixes, so the split has to be driven by evidence and has to refuse to guess
    when there is none.
    """

    def _report(self, evidence=None, corpus_days=()):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus = Corpus(root)
            for date in corpus_days:
                corpus.day(date, candidates=[], watchlist=[], games=[])
            report = build_report(
                picks_dir=root,
                since=parse_date("2026-08-12"),
                until=parse_date("2026-08-14"),
                run_evidence=evidence,
            )
        return {day["date"]: day for day in report["days"]}, report

    def test_each_evidence_verdict_lands_in_its_own_class(self):
        # One arm per value, not one test for the arm that happened to be first.
        # PR #64 regressed on exactly that: a tri-state was read for its polarity
        # and only one value was ever exercised.
        for verdict in EVIDENCE_ASSIGNABLE_CLASSES:
            with self.subTest(verdict=verdict):
                by_date, _ = self._report(
                    run_evidence({"2026-08-13": evidence_entry(verdict)})
                )
                self.assertEqual(by_date["2026-08-13"]["day_class"], verdict)

    def test_a_day_with_no_evidence_stays_unexplained_rather_than_guessed(self):
        # `no_slate_artifact` now means "we do not know", and that is the point
        # of keeping it. A split that silently defaulted to one of its explained
        # values would turn an open question into a finding.
        by_date, report = self._report(
            run_evidence({"2026-08-13": evidence_entry("job_never_fired")})
        )
        self.assertEqual(by_date["2026-08-12"]["day_class"], "no_slate_artifact")
        self.assertEqual(by_date["2026-08-14"]["day_class"], "no_slate_artifact")
        gap = next(g for g in report["data_gaps"] if g["kind"] == "no_slate_artifact")
        self.assertEqual(gap["dates"], ["2026-08-12", "2026-08-14"])

    def test_with_no_evidence_file_at_all_every_silent_day_is_unexplained(self):
        by_date, _ = self._report(None)
        self.assertEqual(
            [d["day_class"] for d in by_date.values()], ["no_slate_artifact"] * 3
        )

    def test_evidence_cannot_overrule_an_artifact_the_corpus_holds(self):
        # The precedence that keeps this from laundering a log line over a file.
        # The corpus has an empty slate for 08-13; evidence claims the job never
        # fired. The file wins, the verdict is recorded, and `applied` says so.
        by_date, _ = self._report(
            run_evidence({"2026-08-13": evidence_entry("job_never_fired")}),
            corpus_days=("2026-08-13",),
        )
        day = by_date["2026-08-13"]
        self.assertEqual(day["day_class"], "slate_empty")
        self.assertEqual(day["run_evidence"]["verdict"], "job_never_fired")
        self.assertFalse(day["run_evidence"]["applied"])
        self.assertIn("outranks run evidence", day["run_evidence"]["not_applied_reason"])

    def test_the_split_stays_exhaustive_over_the_days_it_replaced(self):
        by_date, report = self._report(
            run_evidence(
                {
                    "2026-08-12": evidence_entry("scan_ran_artifact_unwritten"),
                    "2026-08-13": evidence_entry("job_never_fired"),
                }
            )
        )
        counts = report["aggregates"]["day_classes"]
        self.assertEqual(counts["scan_ran_artifact_unwritten"], 1)
        self.assertEqual(counts["job_never_fired"], 1)
        self.assertEqual(counts["no_slate_artifact"], 1)
        self.assertTrue(report["reconciliation"]["ok"], report["reconciliation"])

    def test_every_evidence_assignable_class_is_a_real_day_class(self):
        # A verdict the aggregate cannot count would raise a KeyError on the
        # first real corpus that carried it.
        for verdict in EVIDENCE_ASSIGNABLE_CLASSES:
            self.assertIn(verdict, DAY_CLASSES)

    def test_evidence_may_not_assign_a_class_the_corpus_is_responsible_for(self):
        # Widening the assignable set has to be a deliberate edit. `slate_empty`
        # is a claim about the games and only a file can support it.
        self.assertNotIn("slate_empty", EVIDENCE_ASSIGNABLE_CLASSES)
        self.assertNotIn("candidates_rejected", EVIDENCE_ASSIGNABLE_CLASSES)


class RunEvidenceLoaderTests(unittest.TestCase):
    """The loader is the only path by which a claim about the scheduler enters
    the headline table, so each of its refusals is tested for going red."""

    def _load(self, payload):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence.json"
            path.write_text(json.dumps(payload))
            return load_run_evidence(path)

    def test_a_well_formed_file_loads(self):
        loaded = self._load(run_evidence({"2026-08-19": evidence_entry("job_never_fired")}))
        self.assertEqual(loaded["dates"]["2026-08-19"]["verdict"], "job_never_fired")

    def test_a_foreign_schema_is_refused(self):
        payload = run_evidence({})
        payload["schema"] = "something-else-v1"
        with self.assertRaises(DiagnosticError):
            self._load(payload)

    def test_a_verdict_outside_the_assignable_set_is_refused(self):
        for verdict in ("slate_empty", "candidates_executed", "made_up", None):
            with self.subTest(verdict=verdict):
                entry = evidence_entry("job_never_fired")
                entry["verdict"] = verdict
                with self.assertRaises(DiagnosticError):
                    self._load(run_evidence({"2026-08-19": entry}))

    def test_a_verdict_with_no_receipt_is_refused(self):
        # The sources behind this file rotate; a claim with nothing quoted is
        # unfalsifiable by the time anyone reads the report.
        entry = evidence_entry("job_never_fired")
        entry["receipts"] = []
        with self.assertRaises(DiagnosticError):
            self._load(run_evidence({"2026-08-19": entry}))

    def test_a_receipt_missing_its_source_or_its_quote_is_refused(self):
        for receipt in ({"source": "agent.log"}, {"quote": "a line"}, {}):
            with self.subTest(receipt=receipt):
                entry = evidence_entry("job_never_fired")
                entry["receipts"] = [receipt]
                with self.assertRaises(DiagnosticError):
                    self._load(run_evidence({"2026-08-19": entry}))


class MultiRootEnumerationTests(unittest.TestCase):
    """The 2026-08-20 miss: the window spans a deploy cutover and the slate
    wrote into more than one checkout, so a single-root enumeration reported an
    existing artifact as absent."""

    def _two_roots(self, tmp, *, in_primary=(), in_secondary=()):
        primary, secondary = Path(tmp) / "runtime" / ".picks", Path(tmp) / "dev" / ".picks"
        for root, dates in ((primary, in_primary), (secondary, in_secondary)):
            corpus = Corpus(root)
            for date in dates:
                corpus.day(date, candidates=[], watchlist=[], games=[])
        return primary, secondary

    def test_a_file_only_the_secondary_root_holds_is_found_and_classified(self):
        with TemporaryDirectory() as tmp:
            primary, secondary = self._two_roots(
                tmp, in_primary=("2026-08-21",), in_secondary=("2026-08-20",)
            )
            report = build_report(
                picks_dir=primary,
                extra_picks_dirs=[secondary],
                since=parse_date("2026-08-20"),
                until=parse_date("2026-08-21"),
            )
        by_date = {day["date"]: day for day in report["days"]}
        self.assertEqual(by_date["2026-08-20"]["day_class"], "slate_empty")
        self.assertEqual(by_date["2026-08-20"]["roots_with_files"], ["dev"])
        self.assertEqual(by_date["2026-08-20"]["slate_json_root"], "dev")

    def test_the_same_corpus_read_from_one_root_gets_that_day_wrong(self):
        # The discriminating half. Without it the multi-root support is only
        # shown not to break anything, and the defect it fixes is never pinned.
        with TemporaryDirectory() as tmp:
            primary, _secondary = self._two_roots(
                tmp, in_primary=("2026-08-21",), in_secondary=("2026-08-20",)
            )
            report = build_report(
                picks_dir=primary,
                since=parse_date("2026-08-20"),
                until=parse_date("2026-08-21"),
            )
        by_date = {day["date"]: day for day in report["days"]}
        self.assertEqual(by_date["2026-08-20"]["day_class"], "no_slate_artifact")

    def test_a_root_with_nothing_for_a_date_is_listed_with_an_empty_list(self):
        # An absent key and an empty list read the same to a skimmer and mean
        # opposite things to anyone asking whether the root was searched at all.
        with TemporaryDirectory() as tmp:
            primary, secondary = self._two_roots(
                tmp, in_primary=("2026-08-21",), in_secondary=("2026-08-20",)
            )
            report = build_report(
                picks_dir=primary,
                extra_picks_dirs=[secondary],
                since=parse_date("2026-08-20"),
                until=parse_date("2026-08-21"),
            )
        for day in report["days"]:
            self.assertEqual(set(day["files_by_root"]), {"runtime", "dev"})
        by_date = {day["date"]: day for day in report["days"]}
        self.assertEqual(by_date["2026-08-20"]["files_by_root"]["runtime"], [])
        self.assertEqual(by_date["2026-08-21"]["files_by_root"]["dev"], [])
        self.assertIn(
            "execute/2026-08-20-schedule.json",
            by_date["2026-08-20"]["files_by_root"]["dev"],
        )

    def test_dates_present_in_exactly_one_root_are_named(self):
        with TemporaryDirectory() as tmp:
            primary, secondary = self._two_roots(
                tmp,
                in_primary=("2026-08-20", "2026-08-21"),
                in_secondary=("2026-08-20",),
            )
            report = build_report(
                picks_dir=primary,
                extra_picks_dirs=[secondary],
                since=parse_date("2026-08-20"),
                until=parse_date("2026-08-21"),
            )
        only = report["enumeration"]["dates_in_one_root_only"]
        self.assertEqual([entry["date"] for entry in only], ["2026-08-21"])
        self.assertEqual(only[0]["present_in"], "runtime")
        self.assertEqual(only[0]["absent_from"], ["dev"])

    def test_only_a_date_the_PRIMARY_root_lacks_counts_as_a_miss(self):
        # The asymmetry is the whole point. A date only the primary has is the
        # secondary checkout no longer being written to — benign. A date only
        # the SECONDARY has is a file a primary-only run reports as absent.
        # Collapsing the two buries the one real miss under the benign ones.
        with TemporaryDirectory() as tmp:
            primary, secondary = self._two_roots(
                tmp, in_primary=("2026-08-21",), in_secondary=("2026-08-20",)
            )
            report = build_report(
                picks_dir=primary,
                extra_picks_dirs=[secondary],
                since=parse_date("2026-08-20"),
                until=parse_date("2026-08-21"),
            )
        enumeration = report["enumeration"]
        self.assertEqual(
            sorted(e["date"] for e in enumeration["dates_in_one_root_only"]),
            ["2026-08-20", "2026-08-21"],
        )
        missing = enumeration["dates_missing_from_primary"]
        self.assertEqual([e["date"] for e in missing], ["2026-08-20"])
        self.assertEqual(missing[0]["present_in"], ["dev"])
        self.assertIn(
            "execute/2026-08-20-schedule.json", missing[0]["files"]["dev"]
        )

    def test_a_single_root_run_never_claims_a_one_root_only_date(self):
        # With one root every date is trivially "in one root only". Reporting
        # that would turn the finding into noise on every single-root run.
        with TemporaryDirectory() as tmp:
            primary, _ = self._two_roots(tmp, in_primary=("2026-08-20",))
            report = build_report(
                picks_dir=primary,
                since=parse_date("2026-08-20"),
                until=parse_date("2026-08-20"),
            )
        self.assertEqual(report["enumeration"]["dates_in_one_root_only"], [])

    def test_the_primary_root_wins_a_fact_both_roots_hold(self):
        with TemporaryDirectory() as tmp:
            primary, secondary = self._two_roots(
                tmp, in_primary=("2026-08-20",), in_secondary=("2026-08-20",)
            )
            report = build_report(
                picks_dir=primary,
                extra_picks_dirs=[secondary],
                since=parse_date("2026-08-20"),
                until=parse_date("2026-08-20"),
            )
        self.assertEqual(report["days"][0]["slate_json_root"], "runtime")
        self.assertEqual(report["days"][0]["schedule_cache_root"], "runtime")

    def test_a_corrupt_copy_in_the_primary_root_does_not_veto_a_valid_one(self):
        # Scan to the first VALID copy, keeping the invalid one's provenance.
        # First-PRESENT would let a corrupt primary file suppress a readable
        # secondary — the same shape PR #61 fixed in the other direction.
        with TemporaryDirectory() as tmp:
            primary, secondary = self._two_roots(tmp, in_secondary=("2026-08-20",))
            (primary / "execute" / "2026-08-20-schedule.json").write_text("{not json")
            report = build_report(
                picks_dir=primary,
                extra_picks_dirs=[secondary],
                since=parse_date("2026-08-20"),
                until=parse_date("2026-08-20"),
            )
        day = report["days"][0]
        self.assertEqual(day["slate_json_root"], "dev")
        self.assertTrue(day["slate_json_present"])
        self.assertEqual([c["root"] for c in day["slate_json_corrupt"]], ["runtime"])

    def test_two_roots_with_the_same_parent_name_get_distinct_labels(self):
        # Merging them under one label would hide exactly the discrepancy the
        # enumeration exists to surface.
        with TemporaryDirectory() as tmp:
            a = Path(tmp) / "a" / "same" / ".picks"
            b = Path(tmp) / "b" / "same" / ".picks"
            Corpus(a).day("2026-08-20", candidates=[], watchlist=[], games=[])
            Corpus(b)
            report = build_report(
                picks_dir=a,
                extra_picks_dirs=[b],
                since=parse_date("2026-08-20"),
                until=parse_date("2026-08-20"),
            )
        self.assertEqual(report["enumeration"]["roots_searched"], ["same", "same-2"])


class NamespaceSilenceFindingTests(unittest.TestCase):
    def test_both_instances_are_reported_as_one_pattern(self):
        # The wrong-root lookup and the event_id/gamePk join are the same defect
        # in different clothes: a query against the wrong namespace returns
        # silence, and silence reads as absence. Naming them as one pattern is
        # what makes the third occurrence recognisable.
        with TemporaryDirectory() as tmp:
            primary = Path(tmp) / "runtime" / ".picks"
            secondary = Path(tmp) / "dev" / ".picks"
            Corpus(primary)
            Corpus(secondary).day("2026-08-20", candidates=[], watchlist=[], games=[])
            report = build_report(
                picks_dir=primary,
                extra_picks_dirs=[secondary],
                since=parse_date("2026-08-20"),
                until=parse_date("2026-08-20"),
            )
        finding = next(f for f in report["findings"] if f["pattern"] == "namespace_silence")
        instances = {entry["instance"] for entry in finding["instances"]}
        self.assertIn("event_id joined against gamePk", instances)
        self.assertIn("corpus enumerated from one .picks root", instances)

    def test_the_root_instance_is_derived_from_the_data_not_asserted(self):
        # A hardcoded instance would keep claiming a miss on a corpus that has
        # none. Both roots hold this date, so there is nothing to report.
        with TemporaryDirectory() as tmp:
            primary = Path(tmp) / "runtime" / ".picks"
            secondary = Path(tmp) / "dev" / ".picks"
            Corpus(primary).day("2026-08-20", candidates=[], watchlist=[], games=[])
            Corpus(secondary).day("2026-08-20", candidates=[], watchlist=[], games=[])
            report = build_report(
                picks_dir=primary,
                extra_picks_dirs=[secondary],
                since=parse_date("2026-08-20"),
                until=parse_date("2026-08-20"),
            )
        finding = next(f for f in report["findings"] if f["pattern"] == "namespace_silence")
        instances = {entry["instance"] for entry in finding["instances"]}
        self.assertNotIn("corpus enumerated from one .picks root", instances)

    def test_a_date_only_the_primary_root_has_is_not_reported_as_a_miss(self):
        # The finding must not fire on the benign direction. The secondary root
        # simply stops being written to partway through the real window, and
        # eleven benign dates crowding out one real miss is how a finding stops
        # being read.
        with TemporaryDirectory() as tmp:
            primary = Path(tmp) / "runtime" / ".picks"
            secondary = Path(tmp) / "dev" / ".picks"
            Corpus(primary).day("2026-08-21", candidates=[], watchlist=[], games=[])
            Corpus(secondary)
            report = build_report(
                picks_dir=primary,
                extra_picks_dirs=[secondary],
                since=parse_date("2026-08-21"),
                until=parse_date("2026-08-21"),
            )
        finding = next(f for f in report["findings"] if f["pattern"] == "namespace_silence")
        instances = {entry["instance"] for entry in finding["instances"]}
        self.assertNotIn("corpus enumerated from one .picks root", instances)
        # ...and it IS still recorded in the symmetric enumeration, so the
        # completeness question stays answerable.
        self.assertEqual(
            [e["date"] for e in report["enumeration"]["dates_in_one_root_only"]],
            ["2026-08-21"],
        )


class CommittedEvidenceFileTests(unittest.TestCase):
    """The evidence file that ships with the report has to satisfy the same
    loader the CLI uses. A committed artifact the tool would refuse to read is
    worse than no artifact."""

    PATH = Path(__file__).resolve().parents[1] / "docs" / "drought-run-evidence-2026-08.json"

    def test_the_committed_evidence_file_passes_the_loader(self):
        payload = load_run_evidence(self.PATH)
        self.assertEqual(
            sorted(payload["dates"]),
            ["2026-08-12", "2026-08-13", "2026-08-14", "2026-08-19"],
        )

    def test_the_08_19_absence_argument_carries_its_denominator(self):
        # An absence argument is only as good as the control that shows the
        # observer was looking. Without the heartbeat and the neighbouring
        # dates, "zero job lines" is indistinguishable from "no log".
        payload = load_run_evidence(self.PATH)
        counts = payload["per_date_counts"]["rows"]
        self.assertEqual(counts["2026-08-19"][0], 0)
        self.assertGreater(counts["2026-08-18"][0], 0)
        self.assertGreater(counts["2026-08-20"][0], 0)
        self.assertGreater(counts["2026-08-19"][2], 0)
        self.assertEqual(payload["delivery_channel_messages_per_date"]["rows"]["2026-08-19"], 0)
        self.assertTrue(
            all(
                n > 0
                for date, n in payload["delivery_channel_messages_per_date"]["rows"].items()
                if date != "2026-08-19"
            )
        )

    def test_the_08_19_verdict_keeps_its_cause_an_open_question(self):
        # Knowing the job did not fire is not knowing why. Collapsing those
        # would be the guess this whole design refuses.
        payload = load_run_evidence(self.PATH)
        open_dates = {
            date for q in payload["open_questions"] for date in q.get("dates", [])
        }
        self.assertIn("2026-08-19", open_dates)

    def test_every_rotating_source_states_its_retention_limit(self):
        payload = load_run_evidence(self.PATH)
        for source in payload["sources"]:
            with self.subTest(source=source["name"]):
                self.assertTrue(source.get("retention_limit"))
                self.assertIn("reproducible_by_reviewer", source)

    def test_the_08_20_artifact_receipt_is_checkable(self):
        payload = load_run_evidence(self.PATH)
        receipts = {r["file"]: r for r in payload["artifact_receipts"]}
        slate = receipts[".picks/slate/2026-08-20.md"]
        self.assertEqual(slate["size"], 9017)
        self.assertEqual(slate["mtime_utc"], "2026-08-20T15:36:33Z")
        self.assertEqual(len(slate["sha256"]), 64)
        self.assertEqual(slate["root"], "sports-picks-skill")


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
