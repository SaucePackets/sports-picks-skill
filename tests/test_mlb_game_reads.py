import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import mlb_game_reads
from scripts import mlb_lineup_watchlist
from scripts import mlb_postgame_evidence


def read(game_pk=823509, **overrides):
    entry = {
        "game_pk": game_pk,
        "event_id": f"4018{game_pk}",
        "away": "Atlanta Braves",
        "home": "Milwaukee Brewers",
        "disposition": "pass",
        "dk_fair_prob": {"away": 0.398, "home": 0.602},
        "polymarket_ask": {"away": 0.460, "home": 0.545},
        "conservative_probability": {"away": 0.380, "home": 0.580},
        "net_edge": {"away": -0.080, "home": 0.035},
        "refusing_rails": ["price_discipline"],
    }
    entry.update(overrides)
    return entry


def schedule(reads=None, denominator=None, **overrides):
    reads = [read()] if reads is None else reads
    if denominator is None:
        denominator = [
            {
                "game_pk": entry["game_pk"],
                "event_id": entry["event_id"],
                "away": entry["away"],
                "home": entry["home"],
            }
            for entry in reads
            if isinstance(entry, dict) and isinstance(entry.get("game_pk"), int)
        ]
    payload = {
        "date": "2026-08-22",
        "sport": "MLB",
        "market_type": "moneyline",
        "candidates": [],
        "lineup_watchlist": [],
        "slate_denominator": {
            "source": "mlb_stage2_scan",
            "fetched_at_utc": "2026-08-22T15:30:00+00:00",
            "games": denominator,
        },
        "game_reads": reads,
    }
    payload.update(overrides)
    return payload


class GameReadStructureTests(unittest.TestCase):
    def test_a_complete_read_validates(self):
        self.assertEqual(mlb_game_reads.validate_game_reads(schedule()), [])

    def test_both_id_spaces_are_required(self):
        # The slate's event_id is an ESPN id and game_pk is MLB's. A read
        # carrying one of them is exactly the silence the drought diagnostic
        # found when it tried to join 401816733 to gamePk 824876.
        for field, bad in (("game_pk", None), ("event_id", None), ("game_pk", "823509")):
            with self.subTest(field=field, bad=bad):
                errors = mlb_game_reads.validate_read(read(**{field: bad}), 0)
                self.assertTrue(
                    any(field in error for error in errors),
                    f"{field}={bad!r} produced {errors}",
                )

    def test_a_missing_number_must_say_why(self):
        errors = mlb_game_reads.validate_read(read(polymarket_ask=None), 0)
        self.assertIn(
            "game_reads[0].polymarket_ask is absent and game_reads[0].unavailable does not say why",
            errors,
        )

    def test_an_explained_missing_number_is_accepted(self):
        entry = read(
            polymarket_ask=None,
            net_edge=None,
            unavailable={
                "polymarket_ask": "exact Polymarket slug returned no market data",
                "net_edge": "no ask to price against",
            },
            refusing_rails=["no_polymarket_market"],
        )
        self.assertEqual(mlb_game_reads.validate_read(entry, 0), [])

    def test_a_field_cannot_be_recorded_and_unavailable_at_once(self):
        entry = read(unavailable={"polymarket_ask": "no market"})
        self.assertIn(
            "game_reads[0].polymarket_ask is recorded but also listed unavailable; "
            "it is one or the other",
            mlb_game_reads.validate_read(entry, 0),
        )

    def test_an_unavailable_reason_must_not_be_empty(self):
        entry = read(polymarket_ask=None, net_edge=None, unavailable={"polymarket_ask": "  ", "net_edge": "x"})
        self.assertIn(
            "game_reads[0].unavailable['polymarket_ask'] must be a non-empty reason",
            mlb_game_reads.validate_read(entry, 0),
        )

    def test_unavailable_cannot_name_a_field_that_does_not_exist(self):
        entry = read(unavailable={"vibes": "felt wrong"})
        self.assertIn(
            "game_reads[0].unavailable names unknown field 'vibes'",
            mlb_game_reads.validate_read(entry, 0),
        )

    def test_probabilities_must_be_usable_and_inside_the_open_unit_interval(self):
        for value in (float("nan"), float("inf"), 10**400, 0, 1, 1.5, True, "0.5", None):
            with self.subTest(value=repr(value)):
                entry = read(polymarket_ask={"away": value, "home": 0.545})
                self.assertIn(
                    "game_reads[0].polymarket_ask.away must be a usable probability "
                    "strictly inside (0, 1)",
                    mlb_game_reads.validate_read(entry, 0),
                )

    def test_a_signed_edge_may_be_negative_but_must_be_usable(self):
        self.assertEqual(
            mlb_game_reads.validate_read(read(net_edge={"away": -0.4, "home": 0.4}), 0), []
        )
        self.assertIn(
            "game_reads[0].net_edge.away must be a usable number",
            mlb_game_reads.validate_read(read(net_edge={"away": float("nan"), "home": 0.4}), 0),
        )

    def test_a_side_cannot_be_missing_or_invented(self):
        self.assertIn(
            "game_reads[0].dk_fair_prob.home is missing",
            mlb_game_reads.validate_read(read(dk_fair_prob={"away": 0.4}), 0),
        )
        self.assertIn(
            "game_reads[0].dk_fair_prob has unexpected side(s) ['draw']",
            mlb_game_reads.validate_read(
                read(dk_fair_prob={"away": 0.4, "home": 0.5, "draw": 0.1}), 0
            ),
        )


class DispositionTests(unittest.TestCase):
    def test_a_refusal_must_name_a_rail(self):
        for disposition in sorted(mlb_game_reads.REFUSING_DISPOSITIONS):
            with self.subTest(disposition=disposition):
                entry = read(disposition=disposition, refusing_rails=[])
                self.assertIn(
                    f"game_reads[0].disposition is {disposition!r} but names no refusing rail",
                    mlb_game_reads.validate_read(entry, 0),
                )

    def test_an_accepted_game_must_name_no_rail(self):
        for disposition in sorted(mlb_game_reads.ACCEPTING_DISPOSITIONS):
            with self.subTest(disposition=disposition):
                entry = read(disposition=disposition, refusing_rails=["price_discipline"])
                self.assertIn(
                    f"game_reads[0].disposition is {disposition!r} but names refusing rails "
                    "['price_discipline']",
                    mlb_game_reads.validate_read(entry, 0),
                )

    def test_pass_and_not_priced_are_kept_apart(self):
        # A game nobody could price was never handicapped. Folding it into
        # "pass" is the collapsed-class defect the drought report was built to
        # avoid, one level down.
        self.assertIn("not_priced", mlb_game_reads.DISPOSITIONS)
        self.assertIn("pass", mlb_game_reads.DISPOSITIONS)
        self.assertNotEqual(
            mlb_game_reads.REFUSING_DISPOSITIONS, mlb_game_reads.ACCEPTING_DISPOSITIONS
        )

    def test_an_unknown_rail_is_an_error_not_an_other_bucket(self):
        errors = mlb_game_reads.validate_read(read(refusing_rails=["vibes"]), 0)
        self.assertTrue(any("unknown rail 'vibes'" in error for error in errors), errors)

    def test_a_repeated_rail_is_an_error(self):
        self.assertIn(
            "game_reads[0].refusing_rails repeats a rail",
            mlb_game_reads.validate_read(
                read(refusing_rails=["price_discipline", "price_discipline"]), 0
            ),
        )

    def test_the_vocabulary_is_the_watchlist_gates_not_a_restatement(self):
        # Three review rounds in this repo have gone to copies of one rule
        # drifting apart. An EQUALITY assertion does not catch that: a
        # hand-written literal that happens to equal the union satisfies it
        # exactly, which is the behaviourally-identical-wrapper case, and it
        # SURVIVED the first mutation sweep of this file.
        #
        # So rebind the source and require the ANSWER to follow, in both
        # directions. The rebind is on the module mlb_game_reads actually
        # imported — this repo loads one file as `scripts.x` AND bare `x`, and
        # those are two module objects, so patching the wrong one patches
        # nothing.
        source = sys.modules["mlb_lineup_watchlist"]
        try:
            with mock.patch.object(source, "REQUIRED_ORIGINAL_GATES", {"invented_gate"}):
                reloaded = importlib.reload(mlb_game_reads)
                self.assertIn("invented_gate", reloaded.REFUSAL_RAILS)
                self.assertNotIn("price_discipline", reloaded.REFUSAL_RAILS)
            with mock.patch.object(source, "ALLOWED_BLOCKERS", ("invented_blocker",)):
                reloaded = importlib.reload(mlb_game_reads)
                self.assertIn("invented_blocker", reloaded.REFUSAL_RAILS)
                self.assertNotIn("lineups_unconfirmed", reloaded.REFUSAL_RAILS)
        finally:
            importlib.reload(mlb_game_reads)
        # And the restored vocabulary is the real one.
        self.assertIn("price_discipline", mlb_game_reads.REFUSAL_RAILS)
        self.assertIn("lineups_unconfirmed", mlb_game_reads.REFUSAL_RAILS)
        self.assertTrue(
            set(mlb_lineup_watchlist.REQUIRED_ORIGINAL_GATES) <= set(mlb_game_reads.REFUSAL_RAILS)
        )

    def test_the_probability_check_consults_the_shared_numeric_rule(self):
        # An identity pin on the import would be satisfied by a re-derived
        # copy at the call site, which is how PR #69 and PR #70 both went. So
        # rebind the rule and require the ANSWER to follow, in both directions.
        probe = 0.4
        original = mlb_game_reads.is_finite_number
        try:
            mlb_game_reads.is_finite_number = lambda value: False
            self.assertFalse(mlb_game_reads._is_probability(probe))
            mlb_game_reads.is_finite_number = lambda value: True
            self.assertTrue(mlb_game_reads._is_probability(probe))
        finally:
            mlb_game_reads.is_finite_number = original
        # Compared against another CONSUMER, never against a directly imported
        # copy: this repo imports one file as ``scripts.x`` and bare ``x``, and
        # Python caches those as two module objects, so a consumer-vs-import
        # assertIs fails while both are one source.
        self.assertIs(mlb_game_reads.is_finite_number, mlb_postgame_evidence.is_finite_number)


class CoverageTests(unittest.TestCase):
    def test_a_scheduled_game_with_no_read_fails(self):
        payload = schedule()
        payload["slate_denominator"]["games"].append(
            {"game_pk": 999, "event_id": "401999", "away": "A", "home": "B"}
        )
        self.assertIn(
            "scheduled game 999 has no game_reads entry",
            mlb_game_reads.validate_game_reads(payload),
        )

    def test_a_read_for_an_unscheduled_game_fails(self):
        payload = schedule(reads=[read(), read(game_pk=424242)])
        payload["slate_denominator"]["games"] = payload["slate_denominator"]["games"][:1]
        self.assertIn(
            "game_reads entry 424242 is not in slate_denominator",
            mlb_game_reads.validate_game_reads(payload),
        )

    def test_two_reads_for_one_game_fail(self):
        payload = schedule(reads=[read(), read()])
        self.assertIn(
            "game_reads has 2 entries for game 823509",
            mlb_game_reads.validate_game_reads(payload),
        )

    def test_a_slate_with_no_denominator_is_refused_not_waved_through(self):
        # audit-results/ looked like the denominator and is not: every file in
        # it carries an _audit_fetched_at_utc from the drought lane's own
        # analysis run, so at slate time it does not exist. Absent denominator
        # means unverifiable, and unverifiable fails.
        payload = schedule()
        del payload["slate_denominator"]
        self.assertIn(
            "slate_denominator is missing; coverage cannot be checked",
            mlb_game_reads.validate_game_reads(payload),
        )

    def test_the_denominator_must_say_where_it_came_from(self):
        payload = schedule()
        payload["slate_denominator"]["source"] = ""
        payload["slate_denominator"]["fetched_at_utc"] = None
        errors = mlb_game_reads.validate_game_reads(payload)
        self.assertIn(
            "slate_denominator.source must name where the game list came from", errors
        )
        self.assertIn(
            "slate_denominator.fetched_at_utc must be a non-empty timestamp", errors
        )

    def test_an_empty_slate_still_needs_a_recorded_denominator(self):
        payload = schedule(reads=[], denominator=[])
        self.assertEqual(mlb_game_reads.validate_game_reads(payload), [])
        del payload["slate_denominator"]
        self.assertIn(
            "slate_denominator is missing; coverage cannot be checked",
            mlb_game_reads.validate_game_reads(payload),
        )


class ScanCrossCheckTests(unittest.TestCase):
    def test_a_denominator_trimmed_to_match_a_short_read_set_is_caught(self):
        # Without this the denominator is only as honest as the run that wrote
        # it: a run could record the games it felt like reading and be
        # perfectly self-consistent. The scan output is the independent copy.
        payload = schedule()
        scan = [
            {"game_pk": 823509, "event_id": "401823509"},
            {"game_pk": 823743, "event_id": "401823743"},
        ]
        self.assertIn(
            "scan lists game 823743 but slate_denominator does not",
            mlb_game_reads.scan_denominator_errors(payload, scan),
        )

    def test_a_denominator_game_the_scan_never_saw_is_caught(self):
        payload = schedule()
        self.assertIn(
            "slate_denominator lists game 823509 but the scan does not",
            mlb_game_reads.scan_denominator_errors(payload, []),
        )

    def test_a_scan_row_with_no_game_pk_blocks_verification(self):
        payload = schedule()
        scan = [
            {"game_pk": 823509, "event_id": "401823509"},
            {"game_pk": None, "event_id": "9", "error": "unmatched: no MLB StatsAPI game"},
        ]
        errors = mlb_game_reads.scan_denominator_errors(payload, scan)
        self.assertIn(
            "1 scan row(s) carry no game_pk; the denominator cannot be verified against a "
            "scan that failed to identify every game",
            errors,
        )


class CardReconciliationTests(unittest.TestCase):
    def test_reads_and_card_must_agree_on_what_was_taken(self):
        payload = schedule(reads=[read(disposition="candidate", refusing_rails=[])])
        self.assertIn(
            "1 game_reads entries say 'candidate' but the schedule carries 0 candidates",
            mlb_game_reads.validate_game_reads(payload),
        )

    def test_agreement_validates(self):
        payload = schedule(reads=[read(disposition="candidate", refusing_rails=[])])
        payload["candidates"] = [{"sport": "MLB"}]
        self.assertEqual(mlb_game_reads.validate_game_reads(payload), [])

    def test_watchlist_reads_and_watchlist_entries_must_agree(self):
        payload = schedule(reads=[read(disposition="lineup_watchlist", refusing_rails=[])])
        self.assertIn(
            "1 game_reads entries say 'lineup_watchlist' but the schedule carries 0 "
            "lineup_watchlist",
            mlb_game_reads.validate_game_reads(payload),
        )


class CliTests(unittest.TestCase):
    def run_cli(self, payload, scan=None):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "schedule.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            argv = [str(path)]
            if scan is not None:
                scan_path = Path(tmp) / "scan.json"
                scan_path.write_text(json.dumps(scan), encoding="utf-8")
                argv += ["--denominator", str(scan_path)]
            return mlb_game_reads.main(argv)

    def test_a_valid_slate_exits_zero(self):
        self.assertEqual(self.run_cli(schedule()), 0)

    def test_an_invalid_slate_exits_nonzero(self):
        payload = schedule()
        del payload["slate_denominator"]
        self.assertEqual(self.run_cli(payload), 1)

    def test_the_denominator_cross_check_can_fail_an_otherwise_valid_slate(self):
        self.assertEqual(self.run_cli(schedule()), 0)
        self.assertEqual(
            self.run_cli(
                schedule(),
                scan=[
                    {"game_pk": 823509, "event_id": "401823509"},
                    {"game_pk": 823743, "event_id": "401823743"},
                ],
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()
