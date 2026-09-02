import importlib
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import mlb_game_reads
from scripts import mlb_lineup_watchlist
from scripts import mlb_postgame_evidence
from scripts import mlb_probability_model

REPO_ROOT = Path(__file__).resolve().parents[1]
NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}


def read(game_pk=823509, **overrides):
    entry = {
        "game_pk": game_pk,
        "event_id": f"4018{game_pk}",
        "away": "Atlanta Braves",
        "home": "Milwaukee Brewers",
        "disposition": "pass",
        "dk_fair_prob": {"away": 0.398, "home": 0.602},
        "polymarket_ask": {"away": 0.460, "home": 0.545},
        # conservative == raw - haircut on BOTH sides, which the validator now
        # checks. The fixture has to hold together or it stops being a fixture
        # for anything.
        "raw_probability": {"away": 0.400, "home": 0.610},
        "uncertainty_haircut": 0.02,
        "conservative_probability": {"away": 0.380, "home": 0.590},
        "model_version": "vig-mlb-market-v1",
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

    def test_a_zero_haircut_is_legal_and_is_not_checked_as_a_probability(self):
        # The market-only fallback's OWN contract is raw == dk_fair with
        # uncertainty_haircut == 0, and it is the state the live runtime is in.
        # Checking this field with the probability rule (0 < x < 1) would
        # reject every read the fallback produces — the field would be
        # unwritable in exactly the configuration we are trying to measure.
        entry = read(
            raw_probability={"away": 0.398, "home": 0.602},
            uncertainty_haircut=0,
            conservative_probability={"away": 0.398, "home": 0.602},
        )
        self.assertEqual(mlb_game_reads.validate_read(entry, 0), [])
        self.assertFalse(mlb_game_reads._is_probability(0))

    def test_a_negative_haircut_is_refused(self):
        errors = mlb_game_reads.validate_read(read(uncertainty_haircut=-0.01), 0)
        self.assertTrue(
            any("uncertainty_haircut must be a usable number" in e for e in errors), errors
        )

    def test_coherence_holds_at_every_legal_haircut_not_just_the_fixture_one(self):
        # The original coherence tests all inherited uncertainty_haircut 0.02
        # from the fixture, which is how the excused-haircut hole survived
        # them. Vary the haircut across its legal range, including zero.
        for haircut in (0, 0.0, 0.005, 0.05):
            with self.subTest(haircut=haircut):
                coherent = read(
                    raw_probability={"away": 0.400, "home": 0.610},
                    uncertainty_haircut=haircut,
                    conservative_probability={
                        "away": 0.400 - haircut,
                        "home": 0.610 - haircut,
                    },
                )
                self.assertEqual(mlb_game_reads.validate_read(coherent, 0), [])
                broken = dict(coherent)
                broken["conservative_probability"] = {
                    "away": 0.400 - haircut + 0.05,
                    "home": 0.610 - haircut,
                }
                self.assertTrue(
                    any(
                        "conservative_probability.away is" in e
                        for e in mlb_game_reads.validate_read(broken, 0)
                    )
                )

    def test_the_three_model_numbers_must_reconcile_on_both_sides(self):
        # conservative == raw - haircut is the contract in mlb_probability_model.
        # A row that disagrees with itself is worse than a missing row, because
        # the evaluator counts it.
        for side in ("away", "home"):
            with self.subTest(side=side):
                conservative = dict(read()["conservative_probability"])
                conservative[side] = conservative[side] + 0.05
                errors = mlb_game_reads.validate_read(
                    read(conservative_probability=conservative), 0
                )
                self.assertTrue(
                    any(f"conservative_probability.{side} is" in e for e in errors), errors
                )

    def test_the_model_trail_is_recorded_whole_or_not_at_all(self):
        # Every field of the trail, excused one at a time. Each excuse removes
        # the evidence some OTHER check needs, so "explained away" must not
        # read as "nothing to check here".
        for field in mlb_game_reads.MODEL_TRAIL_FIELDS:
            with self.subTest(field=field):
                entry = read()
                entry[field] = None
                entry["unavailable"] = {field: "not recorded by this run"}
                errors = mlb_game_reads.validate_read(entry, 0)
                self.assertTrue(
                    any("records part of the model trail" in e for e in errors), errors
                )

    def test_an_excused_haircut_cannot_launder_an_incoherent_handicap(self):
        # The concrete escape hatch: with the haircut merely excused, the
        # coherence loop had nothing to subtract and a fifty-point disagreement
        # between raw and conservative validated clean, reached the dataset,
        # and got scored.
        entry = read(
            raw_probability={"away": 0.400, "home": 0.610},
            conservative_probability={"away": 0.900, "home": 0.100},
            uncertainty_haircut=None,
            unavailable={"uncertainty_haircut": "not recorded by this run"},
        )
        self.assertNotEqual(mlb_game_reads.validate_read(entry, 0), [])

    def test_a_game_that_was_never_handicapped_may_say_so(self):
        # not_priced games — no DK line, no market — were never handicapped at
        # all. Requiring a probability there would force the run to invent one,
        # which is the opposite of what this record is for.
        entry = read(
            disposition="not_priced",
            refusing_rails=["no_dk_price"],
            dk_fair_prob=None,
            polymarket_ask=None,
            raw_probability=None,
            conservative_probability=None,
            net_edge=None,
            uncertainty_haircut=None,
            model_version=None,
            unavailable={
                "dk_fair_prob": "DK line unavailable",
                "polymarket_ask": "no Polymarket market for this game",
                "raw_probability": "never handicapped; no DK prior to anchor",
                "conservative_probability": "never handicapped",
                "net_edge": "no price to compute an edge against",
                "uncertainty_haircut": "never handicapped",
                "model_version": "never handicapped",
            },
        )
        self.assertEqual(mlb_game_reads.validate_read(entry, 0), [])

    def test_an_absent_model_number_still_has_to_say_why(self):
        for field in ("raw_probability", "uncertainty_haircut", "model_version"):
            with self.subTest(field=field):
                entry = read()
                entry[field] = None
                errors = mlb_game_reads.validate_read(entry, 0)
                self.assertIn(
                    f"game_reads[0].{field} is absent and game_reads[0].unavailable does not say why",
                    errors,
                )

    def test_the_coherence_tolerance_matches_the_probability_contract(self):
        # Defined independently so a recording check does not drag the
        # execution-path model module into its import closure — and pinned
        # here, because two copies of one number agree only until one changes.
        self.assertEqual(
            mlb_game_reads.COHERENCE_TOLERANCE,
            mlb_probability_model.COMPONENT_SUM_TOLERANCE,
        )

    def test_the_committed_deployment_margins_load_through_the_real_loader(self):
        # The margins are predeclared in the repo BEFORE any evaluation row
        # exists. A committed block that the real loader rejects would be a
        # declaration in name only — the gate would still fail closed and
        # nobody would find out until they tried to run it.
        block = json.loads(
            (
                REPO_ROOT
                / "skills"
                / "sports-picks"
                / "references"
                / "mlb_model_deployment_policy.json"
            ).read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / "risk_limits.json").write_text(
                json.dumps({"mlb_model_deployment_policy": block}), encoding="utf-8"
            )
            policy = mlb_probability_model.load_model_deployment_policy(state)
        self.assertIsNotNone(policy, "the committed margins do not load; the gate would fail closed")
        self.assertEqual(policy["min_evaluation_picks"], 300)
        self.assertEqual(policy["max_calibration_regression"], 0.0)

    def test_the_prompt_enumerates_exactly_the_vocabulary_the_validator_accepts(self):
        # The validator is fail-closed and the run only knows the rails the
        # prompt names, so a rail in code but not in mlb.md has no legal way to
        # be recorded: naming nothing hard-fails the slate, and naming the
        # nearest listed rail writes a judgement that was never made. That is
        # how `incomplete_input_data` — 8 of 109 parsed reads — shipped
        # invisible to the run. Pin the doc against the source so the next rail
        # cannot drift the same way.
        text = " ".join(
            (REPO_ROOT / "skills" / "sports-picks" / "references" / "mlb.md")
            .read_text()
            .split()
        )
        paragraph = text.split("The rail vocabulary is", 1)[1].split(
            "Name every rail", 1
        )[0]
        groups = (
            ("handicapping gates", set(mlb_lineup_watchlist.REQUIRED_ORIGINAL_GATES)),
            ("deferrable blockers", set(mlb_lineup_watchlist.ALLOWED_BLOCKERS)),
            ("structural rails", set(mlb_game_reads.STRUCTURAL_RAILS)),
        )
        documented = set()
        for label, expected in groups:
            with self.subTest(group=label):
                match = re.search(
                    rf"(\w+) {label}[^(]*\(([^)]*)\)", paragraph
                )
                self.assertIsNotNone(match, f"mlb.md no longer enumerates the {label}")
                # The COUNT WORD is checked too: "five structural rails" was
                # the whole of the defect, and a reader trusts the count.
                self.assertEqual(
                    NUMBER_WORDS.get(match.group(1)),
                    len(expected),
                    f"mlb.md says '{match.group(1)} {label}', code has {len(expected)}",
                )
                named = set(re.findall(r"`([a-z_]+)`", match.group(2)))
                self.assertEqual(named, expected)
                documented |= named
        # And the three groups together are the closed vocabulary, so a rail
        # added to REFUSAL_RAILS outside them cannot slip past either.
        self.assertEqual(documented, set(mlb_game_reads.REFUSAL_RAILS))
        # Every rail is a token the run can be told to write; the newest one
        # carries its meaning, because a name with no gloss is not guidance.
        self.assertIn("incomplete_input_data", documented)
        self.assertIn("a required input never arrived", text)

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


class IdentityAgreementTests(unittest.TestCase):
    """A matching ``game_pk`` selects the pair; it does not corroborate it.

    Every one of this module's joins keyed on ``game_pk`` alone, and both sides
    of each join carry an ``event_id`` and two club names that were never
    compared. A read copied off the wrong game keeps well-formed probabilities
    about a game nobody asked about, and it was counted as coverage.

    Each refusal below states its premise first — the same payload with the one
    field put right validates clean — so none of them can pass because the
    fixture was broken in some other way.
    """

    def denominator_of(self, payload):
        return payload["slate_denominator"]["games"]

    def test_a_read_carrying_another_games_event_id_is_refused(self):
        payload = schedule()
        self.assertEqual(mlb_game_reads.validate_game_reads(payload), [])

        payload["game_reads"][0]["event_id"] = "401999999"

        self.assertIn(
            "game_reads[0].event_id is '401999999' but the same game_pk is "
            "'4018823509'; one of these records is about a different game — "
            "matching game_pk alone does not make them the same game",
            mlb_game_reads.validate_game_reads(payload),
        )

    def test_a_read_with_the_clubs_the_wrong_way_round_is_refused(self):
        payload = schedule()
        self.assertEqual(mlb_game_reads.validate_game_reads(payload), [])

        entry = payload["game_reads"][0]
        entry["away"], entry["home"] = entry["home"], entry["away"]

        errors = mlb_game_reads.validate_game_reads(payload)
        self.assertTrue(
            any("away/home transposed" in error for error in errors), errors
        )

    def test_one_crossed_side_is_the_same_backwards_row(self):
        """Requiring BOTH sides to cross would let the half-transposed row land.

        A read whose away slot holds the home club is already wrong about every
        per-side number it carries, whatever its other slot says.
        """
        payload = schedule()
        self.assertEqual(mlb_game_reads.validate_game_reads(payload), [])

        payload["game_reads"][0]["away"] = "Milwaukee Brewers"

        errors = mlb_game_reads.validate_game_reads(payload)
        self.assertTrue(
            any("away/home transposed" in error for error in errors), errors
        )

    def test_a_crossing_written_in_another_case_is_still_caught(self):
        """The normalisation has to actually match, or the check fails silent.

        A swap detector that only recognises one spelling is indistinguishable
        from no swap detector at all: it passes every row and reports nothing.
        """
        payload = schedule()
        entry = payload["game_reads"][0]
        entry["away"] = "  MILWAUKEE   BREWERS "
        entry["home"] = "atlanta braves"

        errors = mlb_game_reads.validate_game_reads(payload)
        self.assertTrue(
            any("away/home transposed" in error for error in errors), errors
        )

    def test_a_differently_written_name_on_the_right_side_is_not_a_finding(self):
        """Crossing, not equality. The two sides may name a club differently.

        No corpus of hand-filled reads exists yet to measure how far the
        vocabularies drift, so demanding equality would refuse honest rows for
        cosmetic difference — and a rail that fires on correct records is a
        rail somebody turns off.
        """
        payload = schedule()
        entry = payload["game_reads"][0]
        entry["away"] = "ATL"
        entry["home"] = "MIL"

        self.assertEqual(mlb_game_reads.validate_game_reads(payload), [])

    def test_whitespace_and_case_alone_are_not_a_finding(self):
        payload = schedule()
        entry = payload["game_reads"][0]
        entry["away"] = "atlanta   braves "
        entry["home"] = " MILWAUKEE BREWERS"

        self.assertEqual(mlb_game_reads.validate_game_reads(payload), [])

    def test_a_denominator_that_cannot_tell_its_sides_apart_reports_no_crossing(self):
        """Against ``away == home`` every read reads as swapped.

        That record is broken and says so through the fields it actually got
        wrong; a phantom transposition finding on top would send the reader to
        the wrong record.
        """
        payload = schedule()
        game = self.denominator_of(payload)[0]
        game["away"] = game["home"] = "Atlanta Braves"

        errors = mlb_game_reads.validate_game_reads(payload)
        self.assertEqual([error for error in errors if "transposed" in error], [])

    def test_an_absent_identity_field_is_reported_once_not_twice(self):
        payload = schedule()
        payload["game_reads"][0]["event_id"] = None

        errors = mlb_game_reads.validate_game_reads(payload)
        self.assertIn("game_reads[0].event_id must be a non-empty string", errors)
        self.assertEqual(
            [error for error in errors if "is about a different game" in error], []
        )

    def test_a_denominator_listing_one_game_twice_is_refused(self):
        """An ambiguous join cannot be corroborated at all.

        With two entries under one ``game_pk`` there is no fact of the matter
        about which one a read agrees with, so the agreement question below is
        unanswerable rather than merely unasked.
        """
        payload = schedule()
        self.assertEqual(mlb_game_reads.validate_game_reads(payload), [])

        self.denominator_of(payload).append(dict(self.denominator_of(payload)[0]))

        self.assertIn(
            "slate_denominator lists game 823509 more than once; a join on game_pk "
            "cannot say which entry a read belongs to",
            mlb_game_reads.validate_game_reads(payload),
        )

    def test_a_doubleheader_read_filled_in_from_its_twin_is_caught(self):
        """The live shape of this defect, not a synthetic one.

        A doubleheader's two games share a date and both clubs and differ only
        by ``game_pk`` and ``event_id``. Filling the second card in from the
        first — the obvious thing to do by hand — leaves an ``event_id`` that
        matching ``game_pk`` values would otherwise wave through.
        """
        first = read(game_pk=823509)
        second = read(game_pk=823510)
        payload = schedule(reads=[first, second])
        self.assertEqual(mlb_game_reads.validate_game_reads(payload), [])

        second["event_id"] = first["event_id"]

        errors = mlb_game_reads.validate_game_reads(payload)
        self.assertTrue(
            any("game_reads[1].event_id" in error for error in errors), errors
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


    def test_a_denominator_entry_that_disagrees_with_its_scan_row_is_caught(self):
        """Same ids on both sides is not the same games on both sides.

        The set comparison above says the two rosters are the same SIZE and
        carry the same ``game_pk`` values. It cannot see a denominator entry
        that files another game's ``event_id`` under one of them — and the scan
        is the independent copy, so it is the side that decides.
        """
        payload = schedule()
        scan = [
            {
                "game_pk": 823509,
                "event_id": "4018823509",
                "away": "Atlanta Braves",
                "home": "Milwaukee Brewers",
            }
        ]
        self.assertEqual(mlb_game_reads.scan_denominator_errors(payload, scan), [])

        payload["slate_denominator"]["games"][0]["event_id"] = "401700000"

        self.assertIn(
            "slate_denominator game 823509.event_id is '401700000' but the same "
            "game_pk is '4018823509'; one of these records is about a different "
            "game — matching game_pk alone does not make them the same game",
            mlb_game_reads.scan_denominator_errors(payload, scan),
        )

    def test_a_denominator_entry_transposed_against_the_scan_is_caught(self):
        payload = schedule()
        scan = [
            {
                "game_pk": 823509,
                "event_id": "4018823509",
                "away": "Atlanta Braves",
                "home": "Milwaukee Brewers",
            }
        ]
        self.assertEqual(mlb_game_reads.scan_denominator_errors(payload, scan), [])

        game = payload["slate_denominator"]["games"][0]
        game["away"], game["home"] = game["home"], game["away"]

        errors = mlb_game_reads.scan_denominator_errors(payload, scan)
        self.assertTrue(
            any("away/home transposed" in error for error in errors), errors
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
    def run_cli(self, payload, scan=None, conventional_scan=True):
        """Run the CLI in a real .picks layout.

        The schedule sits where the runtime puts it, because --denominator is
        no longer optional: with the flag omitted the validator resolves the
        scan by convention and treats its ABSENCE as an error. A tmpdir with a
        loose schedule.json used to exercise the no-cross-check path, which is
        precisely the path that no longer exists.
        """
        with tempfile.TemporaryDirectory() as tmp:
            date = payload.get("date", "2026-08-22") if isinstance(payload, dict) else "x"
            path = Path(tmp) / ".picks" / "execute" / f"{date}-schedule.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
            argv = [str(path)]
            if scan is not None:
                scan_path = Path(tmp) / "scan.json"
                scan_path.write_text(json.dumps(scan), encoding="utf-8")
                argv += ["--denominator", str(scan_path)]
            elif conventional_scan:
                # Derived from the module's own helper, never a second literal
                # spelling of the layout.
                conventional = mlb_game_reads.conventional_denominator_path(path, payload)
                conventional.parent.mkdir(parents=True, exist_ok=True)
                games = []
                if isinstance(payload, dict):
                    block = payload.get("slate_denominator")
                    if isinstance(block, dict) and isinstance(block.get("games"), list):
                        games = block["games"]
                conventional.write_text(json.dumps(games), encoding="utf-8")
            return mlb_game_reads.main(argv)

    def test_a_missing_conventional_scan_fails_the_slate(self):
        # The cross-check is not opt-in any more: "nobody ran the scan" and
        # "the scan agrees" must not share an exit code.
        self.assertEqual(self.run_cli(schedule(), conventional_scan=False), 1)

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
