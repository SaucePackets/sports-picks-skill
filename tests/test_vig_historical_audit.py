"""Tests for the read-only historical MLB pick audit.

The audit's whole value is that it refuses to guess, so most of what is pinned
here is a refusal: a missing contract field must produce `unevaluable` and not
`below_floor`, an unreadable schedule must not read as a quiet no-pick day, a
prose price must not be scraped for a number, and a control day must never
enter an accuracy denominator.

Where an assertion could pass for more than one reason, it asserts the named
predicate instead of the downstream verdict — `no_pick_control` rather than "no
candidates came out", `contract_field_present` rather than "the floor said
unevaluable" — because a verdict several causes can produce cannot discriminate
between them.
"""

from __future__ import annotations

import ast
import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
MODULE = "vig_historical_audit.py"

# Bare, sibling-style imports, matching the convention the scripts themselves
# use. `scripts.mlb_final_scores` and `mlb_final_scores` are two distinct module
# objects holding two distinct function objects, and the identity assertions
# below — the ones proving the audit does not have its own idea of who won —
# only mean anything against the module the audit actually imported.
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import import_closure  # noqa: E402
import mlb_final_scores  # noqa: E402
import mlb_runtime_policy  # noqa: E402
import vig_historical_audit as audit  # noqa: E402


def statsapi_payload(games):
    """A minimal MLB Stats API schedule payload, in the real nesting."""
    return {
        "dates": [{
            "games": [
                {
                    "gamePk": g.get("gamePk", 700000 + i),
                    "status": {"detailedState": g.get("status", "Final")},
                    "teams": {
                        "away": {"team": {"name": g["away"]}, "score": g.get("away_score")},
                        "home": {"team": {"name": g["home"]}, "score": g.get("home_score")},
                    },
                }
                for i, g in enumerate(games)
            ]
        }]
    }


def write_day(root, date, document):
    execute = root / "execute"
    execute.mkdir(parents=True, exist_ok=True)
    path = execute / f"{date}-schedule.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def write_results(root, date, games):
    results = root / "results"
    results.mkdir(parents=True, exist_ok=True)
    (results / f"{date}.json").write_text(json.dumps(statsapi_payload(games)), encoding="utf-8")
    return results


FULL_CONTRACT = {
    "dk_fair_prob": 0.60,
    "raw_probability": 0.64,
    "uncertainty_haircut": 0.02,
    "conservative_probability": 0.62,
    "current_ask": 0.55,
    "projected_edge_at_current_ask": 0.07,
    "model_version": "mlb-v3",
}


class SchemaNormalizationTests(unittest.TestCase):
    """The four top-level shapes actually on disk, each named explicitly."""

    def test_current_schedule_shape_normalizes(self):
        doc = {
            "date": "2026-08-30", "sport": "mlb", "market_type": "moneyline",
            "candidates": [{
                "game": "Colorado Rockies at Atlanta Braves",
                "side": "Atlanta Braves", "price": -235,
                "win_probability": 0.74, "dk_fair_prob": 0.6898,
                "net_edge": 0.055, "polymarket_ask": 0.685, "executed": False,
                "vig_review_needed": True, "vig_approved": False,
            }],
        }
        envelope = audit.load_schedule(self._write(doc, "2026-08-30"))
        self.assertEqual(envelope["schema_variant"], "current")
        self.assertEqual(envelope["errors"], [])
        record = audit.normalize_candidate(envelope["candidates"][0], "2026-08-30")
        self.assertEqual(record["away_team"], "Colorado Rockies")
        self.assertEqual(record["home_team"], "Atlanta Braves")
        self.assertEqual(record["slate_price"], 0.685)
        self.assertEqual(record["slate_price_field"], "polymarket_ask")
        self.assertEqual(record["stated_probability"], 0.74)
        self.assertEqual(record["stated_probability_field"], "win_probability")
        self.assertEqual(record["book_odds"], -235)
        self.assertEqual(record["disposition"], "review_rejected")

    def test_legacy_object_shape_normalizes_abbreviated_side_and_book_only_price(self):
        doc = {
            "date": "2026-05-26", "status": "scheduled", "daily_cap": 3,
            "candidates": [{
                "game": "Tampa Bay Rays at Baltimore Orioles",
                "side": "TB", "pick_side": "Tampa Bay Rays",
                "opponent": "Baltimore Orioles", "price": "-112",
                "executed": True, "skipped": False, "unit_size": 15,
            }],
        }
        envelope = audit.load_schedule(self._write(doc, "2026-05-26"))
        self.assertEqual(envelope["schema_variant"], "legacy_object")
        record = audit.normalize_candidate(envelope["candidates"][0], "2026-05-26")
        # Both side fields are retained; dropping either loses a resolution route.
        self.assertEqual(
            record["side_candidates"], [("side", "TB"), ("pick_side", "Tampa Bay Rays")]
        )
        self.assertEqual(record["book_odds"], -112)
        self.assertIsNone(record["slate_price"])
        self.assertEqual(audit.classify_price_quality(record), "book_price_only")

    def test_bare_list_shape_is_a_recognised_variant(self):
        # 2026-07-17 was written as a bare `[]`. That is a legitimate day with a
        # different envelope, not a broken file.
        envelope = audit.load_schedule(self._write([], "2026-07-17"))
        self.assertEqual(envelope["schema_variant"], "legacy_bare_list")
        self.assertEqual(envelope["errors"], [])
        self.assertEqual(envelope["date"], "2026-07-17")

    def test_an_unreadable_schedule_is_not_a_quiet_empty_day(self):
        # THE discriminating case: a control day and a corrupt file both yield
        # zero candidates. The verdict "no candidates" cannot tell them apart,
        # so the named predicate is asserted instead.
        path = self._write({}, "2026-06-01")
        path.write_text("{not json", encoding="utf-8")
        envelope = audit.load_schedule(path)
        self.assertEqual(envelope["schema_variant"], "unreadable")
        self.assertTrue(envelope["errors"])
        day = audit.audit_day(path, path.parent.parent / "results", 0.05)
        self.assertFalse(day["no_pick_control"], "a corrupt file must never count as a control")

    def test_a_date_field_disagreeing_with_the_filename_is_reported(self):
        envelope = audit.load_schedule(
            self._write({"date": "2026-06-02", "candidates": []}, "2026-06-01")
        )
        self.assertTrue(any("disagrees" in e for e in envelope["errors"]))

    def test_a_missing_candidates_key_is_an_error_not_an_empty_day(self):
        envelope = audit.load_schedule(self._write({"date": "2026-06-01"}, "2026-06-01"))
        self.assertIn("no `candidates` key", envelope["errors"])

    def _write(self, document, date):
        import tempfile
        root = Path(tempfile.mkdtemp())
        return write_day(root, date, document)


class PriceParsingTests(unittest.TestCase):
    def test_american_odds_parse_is_strict(self):
        cases = [
            ("-110", (-110, "parsed")), ("+109", (109, "parsed")),
            (-235, (-235, "parsed")), (-110.0, (-110, "parsed")),
            (None, (None, "absent")), ("", (None, "absent")),
            (True, (None, "not_a_price")), (50, (None, "not_a_price")),
            ("even", (None, "prose_unparsed")),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(audit.parse_american_odds(value), expected)

    def test_a_prose_price_is_reported_not_scraped(self):
        # "DK/ESPN CLE -131; Polymarket ask 0.56" contains two numbers that mean
        # different things. Picking one is a guess that would land in a
        # calibration bucket, so neither is picked.
        odds, status = audit.parse_american_odds("DK/ESPN CLE -131; Polymarket ask 0.56")
        self.assertIsNone(odds)
        self.assertEqual(status, "prose_unparsed")
        record = audit.normalize_candidate(
            {"game": "A at B", "price": "DK/ESPN CLE -131; Polymarket ask 0.56"}, "2026-05-30"
        )
        self.assertEqual(record["book_price_raw"], "DK/ESPN CLE -131; Polymarket ask 0.56")
        self.assertEqual(audit.classify_price_quality(record), "prose_price_unparsed")

    def test_a_prose_price_alongside_a_real_market_price_is_still_market_priced(self):
        record = audit.normalize_candidate(
            {"game": "A at B", "price": "DK/ESPN CLE -131; Polymarket ask 0.56",
             "polymarket_price": 0.56}, "2026-05-30"
        )
        self.assertEqual(audit.classify_price_quality(record), "market_price")
        self.assertEqual(record["slate_price"], 0.56)

    def test_implied_breakeven_from_american_odds(self):
        self.assertAlmostEqual(audit.american_implied_probability(-110), 0.5238, places=4)
        self.assertAlmostEqual(audit.american_implied_probability(100), 0.5, places=6)
        self.assertAlmostEqual(audit.american_implied_probability(109), 0.4785, places=4)

    def test_the_paid_price_outranks_the_asked_price(self):
        record = audit.normalize_candidate(
            {"game": "A at B", "polymarket_ask": 0.51, "fill_price": 0.53}, "2026-07-12"
        )
        self.assertEqual(audit.effective_price(record), (0.53, "entry"))

    def test_an_invalid_higher_priority_price_does_not_veto_a_valid_fallback(self):
        # Reviewer's medium at tip 6e7d551: `fill_price: "n/a"` used to win the
        # priority scan by being non-None and then parse to nothing, silently
        # discarding the numeric execution_price behind it. Priority prefers a
        # field's answer; it must not let a junk field silence the rest. The
        # field skipped as invalid stays on the record as provenance — skipped
        # and never-present are different facts.
        record = audit.normalize_candidate(
            {"game": "A at B", "fill_price": "n/a", "execution_price": 0.52}, "2026-07-12"
        )
        self.assertEqual(record["entry_price"], 0.52)
        self.assertEqual(record["entry_price_field"], "execution_price")
        self.assertEqual(record["entry_price_invalid_fields"], ["fill_price"])

    def test_an_invalid_slate_ask_falls_through_to_a_later_slate_field(self):
        record = audit.normalize_candidate(
            {"game": "A at B", "polymarket_ask": "pending", "polymarket_price": 0.44}, "2026-07-12"
        )
        self.assertEqual(record["slate_price"], 0.44)
        self.assertEqual(record["slate_price_field"], "polymarket_price")
        self.assertEqual(record["slate_price_invalid_fields"], ["polymarket_ask"])

    def test_all_invalid_price_fields_yield_no_price_with_their_provenance(self):
        # 1.5 is out of (0, 1) and "n/a" is prose; neither becomes a price and
        # both are named as present-but-unusable.
        record = audit.normalize_candidate(
            {"game": "A at B", "fill_price": "n/a", "execution_price": 1.5}, "2026-07-12"
        )
        self.assertIsNone(record["entry_price"])
        self.assertIsNone(record["entry_price_field"])
        self.assertEqual(record["entry_price_invalid_fields"], ["fill_price", "execution_price"])
        self.assertEqual(audit.classify_price_quality(record), "no_price")

    def test_an_invalid_stated_probability_falls_through_too(self):
        record = audit.normalize_candidate(
            {"game": "A at B", "win_probability": "0.74-ish", "raw_probability": 0.64}, "2026-07-12"
        )
        self.assertEqual(record["stated_probability"], 0.64)
        self.assertEqual(record["stated_probability_field"], "raw_probability")
        self.assertEqual(record["stated_probability_invalid_fields"], ["win_probability"])


class SideAndWinnerMappingTests(unittest.TestCase):
    ROWS = audit.final_scores(statsapi_payload([
        {"away": "Athletics", "home": "Detroit Tigers", "away_score": 1, "home_score": 6},
        {"away": "New York Yankees", "home": "Boston Red Sox", "away_score": 2, "home_score": 5},
    ]))

    def _record(self, raw, date="2026-07-08"):
        return audit.audit_candidate(raw, date, self.ROWS, [], 0.05)

    def test_a_full_name_side_maps_to_win_and_loss_in_both_directions(self):
        won = self._record({"game": "Athletics at Detroit Tigers", "side": "Detroit Tigers"})
        self.assertEqual(won["side_outcome"], "win")
        lost = self._record({"game": "Athletics at Detroit Tigers", "side": "Athletics"})
        self.assertEqual(lost["side_outcome"], "loss")

    def test_a_market_suffix_on_the_side_is_stripped(self):
        record = self._record({"game": "Athletics at Detroit Tigers", "side": "Detroit Tigers ML"})
        self.assertEqual(record["resolved_side"], "Detroit Tigers")
        self.assertEqual(record["side_outcome"], "win")

    def test_the_market_suffix_strip_is_anchored_and_does_not_eat_a_name(self):
        # "MLB" must not be treated as the " ML" suffix plus a stray B.
        self.assertEqual(audit.MARKET_SUFFIX_RE.sub("", "Detroit Tigers MLB"), "Detroit Tigers MLB")
        self.assertEqual(audit.MARKET_SUFFIX_RE.sub("", "Detroit Tigers ML"), "Detroit Tigers")

    def test_nickname_only_matchups_reconcile(self):
        record = self._record({"game": "Yankees at Red Sox", "side": "Red Sox"})
        self.assertEqual(record["match_method"], "team_pair")
        self.assertEqual(record["side_resolution"], "matched_by_nickname")
        self.assertEqual(record["side_outcome"], "win")

    def test_the_at_sign_separator_reconciles(self):
        record = self._record({"game": "NYY @ BOS", "side": "BOS"})
        self.assertEqual(record["official"]["home"], "Boston Red Sox")
        self.assertEqual(record["side_outcome"], "win")

    def test_a_tie_is_a_push_not_a_loss(self):
        rows = audit.final_scores(statsapi_payload(
            [{"away": "Chicago Cubs", "home": "Milwaukee Brewers", "away_score": 3, "home_score": 3}]
        ))
        record = audit.audit_candidate(
            {"game": "Chicago Cubs at Milwaukee Brewers", "side": "CHC"}, "2026-06-27", rows, [], 0.05
        )
        self.assertIsNone(record["official"]["winner"])
        self.assertEqual(record["side_outcome"], "push")

    def test_a_full_name_beats_an_abbreviation_so_the_table_is_not_consulted(self):
        # 2026-05-26's shape. The full name resolves first; the abbreviation
        # route is never reached, which is why this is worth pinning.
        record = self._record({
            "game": "Athletics at Detroit Tigers", "side": "DET", "pick_side": "Detroit Tigers",
        })
        self.assertEqual(record["side_resolution"], "matched_by_name")

    def test_an_abbreviation_naming_a_team_that_did_not_play_stays_unresolved(self):
        # The cross-check working: "SEA" is a valid entry, but Seattle is not in
        # this game, so nothing is credited to either side.
        record = self._record({"game": "Athletics at Detroit Tigers", "side": "SEA"})
        self.assertEqual(record["side_outcome"], "side_unresolved")
        self.assertEqual(record["side_resolution"], "side_names_a_team_not_in_this_game")
        self.assertIsNone(record["resolved_side"])

    def test_the_cross_check_cannot_catch_a_table_entry_that_names_the_opponent(self):
        # The gap, demonstrated rather than claimed away. A table entry that is
        # wrong by naming a team IN this game resolves, and resolves wrongly.
        # Nothing available offline can distinguish it, so the limit is
        # documented in TEAM_ABBREVIATIONS and shown here.
        original = audit.TEAM_ABBREVIATIONS["DET"]
        audit.TEAM_ABBREVIATIONS["DET"] = "Athletics"
        try:
            record = self._record({"game": "Athletics at Detroit Tigers", "side": "DET"})
            self.assertEqual(record["resolved_side"], "Athletics")
            self.assertEqual(record["side_outcome"], "loss")
        finally:
            audit.TEAM_ABBREVIATIONS["DET"] = original

    def test_a_doubleheader_without_a_game_pk_fails_closed(self):
        rows = audit.final_scores(statsapi_payload([
            {"away": "New York Mets", "home": "Atlanta Braves", "away_score": 1, "home_score": 0, "gamePk": 1},
            {"away": "New York Mets", "home": "Atlanta Braves", "away_score": 2, "home_score": 7, "gamePk": 2},
        ]))
        card = {"game": "New York Mets at Atlanta Braves", "side": "NYM"}
        record = audit.audit_candidate(card, "2026-06-15", rows, [], 0.05)
        self.assertEqual(record["match_method"], "ambiguous_doubleheader")
        self.assertEqual(record["side_outcome"], "unreconciled")
        # ...and a game_pk on the card resolves it to the right one of the two.
        pinned = audit.audit_candidate({**card, "game_pk": 2}, "2026-06-15", rows, [], 0.05)
        self.assertEqual(pinned["match_method"], "game_pk")
        self.assertEqual(pinned["side_outcome"], "loss")

    def test_a_wrong_but_existing_game_pk_never_grades_against_the_wrong_game(self):
        # Reviewer's blocker at tip 6e7d551: a stale pk that names a row that
        # EXISTS — some other game — must not reconcile the candidate against
        # it. The pk is only trusted when its row also names the card's
        # matchup; here it doesn't, so matching falls back to the team pair
        # and grades against the game these teams actually played.
        rows = audit.final_scores(statsapi_payload([
            {"away": "New York Mets", "home": "Atlanta Braves", "away_score": 3, "home_score": 1, "gamePk": 1},
            {"away": "New York Yankees", "home": "Boston Red Sox", "away_score": 2, "home_score": 5, "gamePk": 99},
        ]))
        record = audit.audit_candidate(
            {"game": "New York Mets at Atlanta Braves", "side": "New York Mets", "game_pk": 99},
            "2026-06-15", rows, [], 0.05,
        )
        self.assertEqual(record["match_method"], "team_pair_after_game_pk_mismatch")
        self.assertEqual(record["official"]["gamePk"], 1)
        self.assertEqual(record["side_outcome"], "win")

    def test_a_mismatched_game_pk_cannot_break_a_doubleheader_tie(self):
        # Once the pk is known to name the wrong game, nothing it says is
        # usable — including as a tiebreaker between the pair's two rows.
        rows = audit.final_scores(statsapi_payload([
            {"away": "New York Mets", "home": "Atlanta Braves", "away_score": 1, "home_score": 0, "gamePk": 1},
            {"away": "New York Mets", "home": "Atlanta Braves", "away_score": 2, "home_score": 7, "gamePk": 2},
            {"away": "New York Yankees", "home": "Boston Red Sox", "away_score": 2, "home_score": 5, "gamePk": 99},
        ]))
        record = audit.audit_candidate(
            {"game": "New York Mets at Atlanta Braves", "side": "NYM", "game_pk": 99},
            "2026-06-15", rows, [], 0.05,
        )
        self.assertEqual(record["match_method"], "ambiguous_doubleheader")
        self.assertEqual(record["side_outcome"], "unreconciled")

    def test_a_game_pk_with_no_matchup_to_corroborate_it_is_not_a_join(self):
        # A card carrying only a pk gives the audit nothing to verify the row
        # against, and an unverifiable join is treated as no join at all.
        rows = audit.final_scores(statsapi_payload([
            {"away": "New York Mets", "home": "Atlanta Braves", "away_score": 3, "home_score": 1, "gamePk": 1},
        ]))
        record = audit.audit_candidate({"side": "New York Mets", "game_pk": 1}, "2026-06-15", rows, [], 0.05)
        self.assertEqual(record["match_method"], "game_pk_unverifiable_no_matchup_on_card")
        self.assertEqual(record["side_outcome"], "unreconciled")

    def test_a_game_that_had_not_finished_is_distinguished_from_one_that_never_existed(self):
        payload = statsapi_payload([
            {"away": "Colorado Rockies", "home": "Atlanta Braves", "status": "Pre-Game"},
        ])
        rows, unfinished = audit.final_scores(payload), audit.unfinished_games(payload)
        pending = audit.audit_candidate(
            {"game": "Colorado Rockies at Atlanta Braves", "side": "ATL"}, "2026-08-30", rows, unfinished, 0.05
        )
        self.assertEqual(pending["match_method"], "not_final: Pre-Game")
        absent = audit.audit_candidate(
            {"game": "Seattle Mariners at Miami Marlins", "side": "SEA"}, "2026-08-30", rows, unfinished, 0.05
        )
        self.assertEqual(absent["match_method"], "no_official_game")
        # Neither invents a result.
        for record in (pending, absent):
            self.assertEqual(record["side_outcome"], "unreconciled")

    def test_no_cached_results_is_unreconciled_and_never_a_loss(self):
        record = audit.audit_candidate(
            {"game": "Athletics at Detroit Tigers", "side": "ATH"}, "2026-07-08", None, [], 0.05
        )
        self.assertEqual(record["side_outcome"], "unreconciled")
        self.assertIn("no cached official results", record["unreconciled_reason"])

    def test_a_card_result_contradicting_the_official_final_is_surfaced(self):
        agreeing = self._record({"game": "Athletics at Detroit Tigers", "side": "DET", "result": "win"})
        self.assertIs(agreeing["recorded_result_agrees"], True)
        contradicting = self._record({"game": "Athletics at Detroit Tigers", "side": "DET", "result": "loss"})
        self.assertIs(contradicting["recorded_result_agrees"], False)

    def test_the_legacy_recorded_result_vocabulary_is_compared_not_dropped(self):
        # Reviewer's blocker at tip 6e7d551: the 2026-06-10/11 cards say "W",
        # and the old `in ("win", "loss")` gate silently dropped them from the
        # one check that corroborates the mapping layer. Pin every form the
        # corpus contains plus the obvious casings.
        for raw, expected_agrees in (
            ("W", True), ("w", True), ("won", True), ("Win", True),
            ("L", False), ("l", False), ("lost", False), ("Loss", False),
        ):
            with self.subTest(raw=raw):
                record = self._record(
                    {"game": "Athletics at Detroit Tigers", "side": "DET", "result": raw}
                )
                self.assertEqual(record["side_outcome"], "win")
                self.assertIs(record["recorded_result_agrees"], expected_agrees)

    def test_an_unrecognized_recorded_result_is_flagged_never_silently_skipped(self):
        record = self._record(
            {"game": "Athletics at Detroit Tigers", "side": "DET", "result": "victory"}
        )
        self.assertIsNone(record["recorded_result_normalized"])
        self.assertNotIn("recorded_result_agrees", record)
        # The raw value survives so the aggregate can name it.
        self.assertEqual(record["recorded_result"], "victory")

    def test_a_final_row_missing_a_score_is_a_data_defect_not_a_push(self):
        # A Final whose payload lacks a score has winner None — exactly like a
        # genuine tie. The audit must not report a data defect as a baseball
        # outcome; neither enters the win-rate denominator.
        tie_rows = audit.final_scores(statsapi_payload([
            {"away": "Athletics", "home": "Detroit Tigers", "away_score": 4, "home_score": 4},
        ]))
        tied = audit.audit_candidate(
            {"game": "Athletics at Detroit Tigers", "side": "DET"}, "2026-07-08", tie_rows, [], 0.05
        )
        self.assertEqual(tied["side_outcome"], "push")
        scoreless_rows = audit.final_scores(statsapi_payload([
            {"away": "Athletics", "home": "Detroit Tigers", "away_score": None, "home_score": 6},
        ]))
        defective = audit.audit_candidate(
            {"game": "Athletics at Detroit Tigers", "side": "DET"}, "2026-07-08", scoreless_rows, [], 0.05
        )
        self.assertEqual(defective["side_outcome"], "final_score_missing")


class MissingDataAndFloorTests(unittest.TestCase):
    """Missing fields fail closed. That is the whole contract of this section."""

    def test_a_missing_contract_field_is_unevaluable_not_below_floor(self):
        # The distinction the audit exists to make. A card with no
        # conservative_probability did not fail the floor — the floor could not
        # be applied to it — and calling that "below_floor" would manufacture a
        # rejection that never happened.
        record = audit.normalize_candidate(
            {"game": "A at B", "side": "A", "polymarket_ask": 0.55, "win_probability": 0.58}, "2026-08-30"
        )
        verdict = audit.evaluate_floor(record, 0.05)
        self.assertEqual(verdict["verdict"], "unevaluable")
        self.assertNotEqual(verdict["verdict"], "below_floor")
        self.assertEqual(verdict["reason"], "missing conservative_probability, current_ask")
        self.assertIsNone(verdict["conservative_edge"])

    def test_an_advisory_stated_edge_is_labelled_and_never_becomes_the_verdict(self):
        record = audit.normalize_candidate(
            {"game": "A at B", "polymarket_ask": 0.50, "win_probability": 0.62}, "2026-08-30"
        )
        verdict = audit.evaluate_floor(record, 0.05)
        self.assertEqual(verdict["advisory_stated_edge"], 0.12)
        self.assertEqual(verdict["advisory_basis"], "win_probability - slate_price")
        # 0.12 clears 0.05 comfortably, and the verdict is STILL unevaluable.
        self.assertEqual(verdict["verdict"], "unevaluable")

    def test_the_floor_discriminates_in_both_directions_on_a_full_contract(self):
        for edge, expected in ((0.07, "cleared"), (0.05, "cleared"), (0.04, "below_floor")):
            with self.subTest(edge=edge):
                raw = {**FULL_CONTRACT, "game": "A at B",
                       "current_ask": round(0.62 - edge, 6),
                       "projected_edge_at_current_ask": edge}
                verdict = audit.evaluate_floor(audit.normalize_candidate(raw, "2026-08-30"), 0.05)
                self.assertEqual(verdict["verdict"], expected)
                self.assertAlmostEqual(verdict["conservative_edge"], edge, places=6)

    def test_the_floor_is_a_parameter_and_a_different_floor_changes_the_verdict(self):
        record = audit.normalize_candidate({**FULL_CONTRACT, "game": "A at B"}, "2026-08-30")
        self.assertEqual(audit.evaluate_floor(record, 0.05)["verdict"], "cleared")
        self.assertEqual(audit.evaluate_floor(record, 0.09)["verdict"], "below_floor")

    def test_contract_presence_agrees_with_the_execution_gate_field_by_field(self):
        # `contract_field_present` duplicates the gate's per-field rule. This
        # pins them together: for every required field, breaking that field
        # alone must make BOTH this predicate false and the gate complain about
        # that same field. A drift in either direction reds here.
        self.assertEqual(mlb_runtime_policy.stale_probability_field_errors(dict(FULL_CONTRACT)), [])
        for field in mlb_runtime_policy.REQUIRED_EXECUTION_FIELDS:
            with self.subTest(field=field):
                self.assertTrue(audit.contract_field_present(field, FULL_CONTRACT[field]))
                broken = dict(FULL_CONTRACT)
                broken.pop(field)
                self.assertFalse(audit.contract_field_present(field, broken.get(field)))
                errors = mlb_runtime_policy.stale_probability_field_errors(broken)
                self.assertTrue(any(field in e for e in errors), errors)

    def test_data_quality_separates_no_contract_from_partial_contract(self):
        none_present = audit.normalize_candidate({"game": "A at B", "side": "A"}, "2026-05-26")
        self.assertEqual(audit.classify_data_quality(none_present), "no_contract_fields")
        partial = audit.normalize_candidate(
            {"game": "A at B", "side": "A", "dk_fair_prob": 0.69}, "2026-08-30"
        )
        self.assertEqual(audit.classify_data_quality(partial), "partial_contract")
        full = audit.normalize_candidate({**FULL_CONTRACT, "game": "A at B"}, "2026-08-30")
        self.assertEqual(audit.classify_data_quality(full), "full_contract")
        self.assertEqual(full["contract_fields_missing"], [])

    def test_a_candidate_with_no_price_yields_no_edge_of_any_kind(self):
        record = audit.normalize_candidate(
            {"game": "A at B", "side": "A", "win_probability": 0.7}, "2026-05-26"
        )
        self.assertEqual(audit.classify_price_quality(record), "no_price")
        verdict = audit.evaluate_floor(record, 0.05)
        self.assertIsNone(verdict["advisory_stated_edge"])
        self.assertEqual(verdict["verdict"], "unevaluable")

    def test_an_out_of_range_probability_is_absent_not_clamped(self):
        for bad in (0, 1, 1.4, -0.2, "0.6", True, None):
            with self.subTest(value=bad):
                record = audit.normalize_candidate(
                    {"game": "A at B", "win_probability": bad}, "2026-08-30"
                )
                self.assertIsNone(record["stated_probability"])

    def test_a_stake_is_derived_only_from_fields_that_are_present(self):
        no_stake = audit.normalize_candidate({"game": "A at B", "executed": True}, "2026-07-12")
        self.assertIsNone(no_stake["stake_usd"])
        notional = audit.normalize_candidate(
            {"game": "A at B", "fill_notional": 17.85}, "2026-07-12"
        )
        self.assertEqual(notional["stake_usd"], 17.85)
        derived = audit.normalize_candidate(
            {"game": "A at B", "fill_price": 0.51, "fill_quantity": 35}, "2026-07-12"
        )
        self.assertAlmostEqual(derived["stake_usd"], 17.85, places=6)


class NoPickControlTests(unittest.TestCase):
    def test_a_day_with_no_candidates_is_a_control(self):
        import tempfile
        root = Path(tempfile.mkdtemp())
        path = write_day(root, "2026-08-28", {
            "date": "2026-08-28", "sport": "mlb", "market_type": "moneyline", "candidates": [],
        })
        day = audit.audit_day(path, root / "results", 0.05)
        self.assertTrue(day["no_pick_control"])
        self.assertEqual(day["candidates"], [])

    def test_a_day_whose_candidates_were_all_skipped_is_not_a_control(self):
        # A control is "proposed nothing", not "bet nothing". Collapsing the two
        # would hide every day the model proposed a play and the gate refused it.
        import tempfile
        root = Path(tempfile.mkdtemp())
        path = write_day(root, "2026-08-18", {
            "date": "2026-08-18", "sport": "mlb", "market_type": "moneyline",
            "candidates": [{"game": "A at B", "side": "A", "executed": False, "skipped": True,
                            "skip_reason": "review gate failed closed"}],
        })
        day = audit.audit_day(path, root / "results", 0.05)
        self.assertFalse(day["no_pick_control"])
        self.assertEqual(day["candidates"][0]["disposition"], "skipped")

    def test_controls_contribute_to_no_accuracy_denominator(self):
        import tempfile
        root = Path(tempfile.mkdtemp())
        write_results(root, "2026-08-27", [
            {"away": "Athletics", "home": "Detroit Tigers", "away_score": 1, "home_score": 6}
        ])
        loss_day = audit.audit_day(
            write_day(root, "2026-08-27", {
                "date": "2026-08-27", "sport": "mlb", "market_type": "moneyline",
                "candidates": [{"game": "Athletics at Detroit Tigers", "side": "ATH",
                                "executed": True, "polymarket_ask": 0.45}],
            }),
            root / "results", 0.05,
        )
        controls = [
            audit.audit_day(
                write_day(root, date, {"date": date, "sport": "mlb",
                                       "market_type": "moneyline", "candidates": []}),
                root / "results", 0.05,
            )
            for date in ("2026-08-28", "2026-08-29")
        ]
        report = audit.aggregate([loss_day, *controls], 0.05, 0.05, 20)

        self.assertEqual(report["days"]["total"], 3)
        self.assertEqual(report["days"]["no_pick_controls"], 2)
        self.assertEqual(sorted(report["days"]["control_dates"]), ["2026-08-28", "2026-08-29"])
        # The denominator is 1, not 3. Two controls did not become two losses,
        # and the day count did not become the sample size.
        self.assertEqual(report["side_correctness"]["decided"], 1)
        self.assertEqual(report["side_correctness"]["wins"], 0)
        self.assertEqual(report["side_correctness"]["win_rate"], 0.0)
        self.assertEqual(report["candidates"]["total"], 1)

    def test_an_all_control_history_reports_no_rate_at_all(self):
        # 0/0 is not 0%. With nothing decided there is no rate to report, and
        # the field is None rather than a zero someone could quote.
        import tempfile
        root = Path(tempfile.mkdtemp())
        days = [
            audit.audit_day(
                write_day(root, date, {"date": date, "sport": "mlb",
                                       "market_type": "moneyline", "candidates": []}),
                root / "results", 0.05,
            )
            for date in ("2026-08-28", "2026-08-29")
        ]
        report = audit.aggregate(days, 0.05, 0.05, 20)
        self.assertIsNone(report["side_correctness"]["win_rate"])
        self.assertEqual(report["side_correctness"]["decided"], 0)
        self.assertIn("no rate is reportable", audit.render({
            "execute_dir": "x", "results_dir": "y", "days": days, "aggregate": report,
        }))


class AggregateAndCalibrationTests(unittest.TestCase):
    def _records(self, specs):
        """specs: (stated_probability, outcome) -> classified records."""
        rows = audit.final_scores(statsapi_payload([
            {"away": "Athletics", "home": "Detroit Tigers", "away_score": 1, "home_score": 6},
        ]))
        out = []
        for probability, outcome in specs:
            side = "Detroit Tigers" if outcome == "win" else "Athletics"
            out.append(audit.audit_candidate(
                {"game": "Athletics at Detroit Tigers", "side": side,
                 "win_probability": probability, "polymarket_ask": 0.5, "executed": True},
                "2026-07-08", rows, [], 0.05,
            ))
        return out

    def test_calibration_buckets_split_on_the_stated_width(self):
        records = self._records([(0.549, "win"), (0.55, "loss"), (0.599, "loss")])
        buckets = audit.calibration_buckets(records, 0.05, 20)
        self.assertEqual([b["bucket"] for b in buckets], ["[0.50,0.55)", "[0.55,0.60)"])
        self.assertEqual([b["n"] for b in buckets], [1, 2])
        self.assertEqual(buckets[0]["wins"], 1)
        self.assertEqual(buckets[1]["wins"], 0)

    def test_insufficient_samples_are_flagged_and_the_flag_flips_at_the_threshold(self):
        records = self._records([(0.62, "win")] * 3)
        self.assertFalse(audit.calibration_buckets(records, 0.05, 20)[0]["sufficient"])
        self.assertTrue(audit.calibration_buckets(records, 0.05, 3)[0]["sufficient"])

    def test_a_candidate_without_a_stated_probability_is_outside_calibration(self):
        rows = audit.final_scores(statsapi_payload([
            {"away": "Athletics", "home": "Detroit Tigers", "away_score": 1, "home_score": 6},
        ]))
        record = audit.audit_candidate(
            {"game": "Athletics at Detroit Tigers", "side": "DET", "polymarket_ask": 0.5},
            "2026-07-08", rows, [], 0.05,
        )
        self.assertEqual(record["side_outcome"], "win")
        self.assertEqual(audit.calibration_buckets([record], 0.05, 1), [])

    def test_an_unreconciled_candidate_is_outside_calibration(self):
        record = audit.audit_candidate(
            {"game": "Athletics at Detroit Tigers", "side": "DET", "win_probability": 0.6},
            "2026-07-08", None, [], 0.05,
        )
        self.assertEqual(audit.calibration_buckets([record], 0.05, 1), [])

    def test_economics_covers_only_executed_candidates_carrying_a_pnl(self):
        import tempfile
        root = Path(tempfile.mkdtemp())
        write_results(root, "2026-07-12", [
            {"away": "Boston Red Sox", "home": "New York Mets", "away_score": 3, "home_score": 2},
        ])
        day = audit.audit_day(
            write_day(root, "2026-07-12", {
                "date": "2026-07-12", "status": "postgame_complete",
                "candidates": [
                    {"game": "Boston Red Sox at New York Mets", "side": "Boston Red Sox",
                     "executed": True, "fill_notional": 17.85, "pnl": 16.63, "polymarket_ask": 0.51},
                    # Executed, settled nowhere on the card: in the stake total,
                    # out of the ROI population.
                    {"game": "Boston Red Sox at New York Mets", "side": "New York Mets",
                     "executed": True, "fill_notional": 10.0, "polymarket_ask": 0.49},
                    # Never executed: out of both.
                    {"game": "Boston Red Sox at New York Mets", "side": "New York Mets",
                     "executed": False, "skipped": True},
                ],
            }),
            root / "results", 0.05,
        )
        report = audit.aggregate([day], 0.05, 0.05, 20)
        economics = report["economics"]
        self.assertEqual(economics["executed"], 2)
        self.assertEqual(economics["executed_with_stake"], 2)
        self.assertEqual(economics["staked_usd"], 27.85)
        self.assertEqual(economics["executed_with_pnl"], 1)
        self.assertEqual(economics["pnl_usd"], 16.63)
        # ROI divides by 17.85 (the P&L-carrying stake), not 27.85.
        self.assertAlmostEqual(economics["roi"], 16.63 / 17.85, places=6)
        self.assertFalse(economics["roi_sufficient_for_a_claim"])
        # And side correctness covers all three, including the one never bet.
        self.assertEqual(report["side_correctness"]["decided"], 3)

    def test_process_counters_cover_every_candidate_exactly_once(self):
        # A classifier typo that invents a bucket would otherwise show up as a
        # plausible-looking distribution that quietly does not add up.
        import tempfile
        root = Path(tempfile.mkdtemp())
        day = audit.audit_day(
            write_day(root, "2026-06-05", {
                "date": "2026-06-05",
                "candidates": [
                    {"game": "A at B", "side": "A", "executed": True, "polymarket_ask": 0.5},
                    {"game": "C at D", "side": "C", "executed": False, "skipped": True},
                    {"game": "E at F", "side": "E", "price": "-110"},
                ],
            }),
            root / "results", 0.05,
        )
        report = audit.aggregate([day], 0.05, 0.05, 20)
        total = report["candidates"]["total"]
        self.assertEqual(total, 3)
        for name in ("data_quality", "price_quality", "disposition", "floor_verdict"):
            with self.subTest(counter=name):
                self.assertEqual(sum(report["process"][name].values()), total)
                self.assertLessEqual(
                    set(report["process"][name]),
                    set(getattr(audit, {
                        "data_quality": "DATA_QUALITY", "price_quality": "PRICE_QUALITY",
                        "disposition": "DISPOSITIONS", "floor_verdict": "FLOOR_VERDICTS",
                    }[name])),
                    "a classifier produced a value outside its declared vocabulary",
                )

    def test_reconciled_plus_unreconciled_is_every_candidate(self):
        import tempfile
        root = Path(tempfile.mkdtemp())
        write_results(root, "2026-06-05", [
            {"away": "Athletics", "home": "Detroit Tigers", "away_score": 1, "home_score": 6},
        ])
        day = audit.audit_day(
            write_day(root, "2026-06-05", {
                "date": "2026-06-05",
                "candidates": [
                    {"game": "Athletics at Detroit Tigers", "side": "DET"},
                    {"game": "Nowhere at Nothing", "side": "Nowhere"},
                ],
            }),
            root / "results", 0.05,
        )
        report = audit.aggregate([day], 0.05, 0.05, 20)
        counts = report["candidates"]
        self.assertEqual(counts["reconciled_to_official"] + counts["unreconciled"], counts["total"])
        self.assertEqual(counts["unreconciled_reasons"], {"no_official_game": 1})

    def test_the_recorded_result_cross_check_states_its_whole_population(self):
        # Every card that records a result is accounted for in exactly one of:
        # compared, unrecognized form, or recognized-but-uncompared. No card
        # falls through in silence — that silence was the round-2 blocker.
        import tempfile
        root = Path(tempfile.mkdtemp())
        write_results(root, "2026-06-10", [
            {"away": "Athletics", "home": "Detroit Tigers", "away_score": 1, "home_score": 6},
        ])
        day = audit.audit_day(
            write_day(root, "2026-06-10", {
                "date": "2026-06-10",
                "candidates": [
                    # Legacy vocabulary, agrees.
                    {"game": "Athletics at Detroit Tigers", "side": "DET", "result": "W"},
                    # Current vocabulary, disagrees.
                    {"game": "Athletics at Detroit Tigers", "side": "DET", "result": "loss"},
                    # Unrecognized form: named, never compared.
                    {"game": "Athletics at Detroit Tigers", "side": "DET", "result": "victory"},
                    # Recognized form on a card that never reconciles.
                    {"game": "Nowhere at Nothing", "side": "Nowhere", "result": "win"},
                    # No recorded result at all: outside the population.
                    {"game": "Athletics at Detroit Tigers", "side": "ATH"},
                ],
            }),
            root / "results", 0.05,
        )
        cross = audit.aggregate([day], 0.05, 0.05, 20)["recorded_vs_official"]
        self.assertEqual(cross["cards_with_a_recorded_result"], 4)
        self.assertEqual(cross["compared_to_official"], 2)
        self.assertEqual(cross["agree"], 1)
        self.assertEqual(len(cross["disagreements"]), 1)
        self.assertEqual(cross["disagreements"][0]["recorded"], "loss")
        self.assertEqual(cross["unrecognized_forms"], {"victory": 1})
        self.assertEqual(cross["recognized_but_uncompared"], 1)
        # The accounting closes: compared + unrecognized + uncompared = carrying.
        self.assertEqual(
            cross["compared_to_official"]
            + sum(cross["unrecognized_forms"].values())
            + cross["recognized_but_uncompared"],
            cross["cards_with_a_recorded_result"],
        )
        # And the report names the unrecognized form and the score caveat.
        rendered = audit.render({
            "execute_dir": "x", "results_dir": "y", "days": [day],
            "aggregate": audit.aggregate([day], 0.05, 0.05, 20),
        })
        self.assertIn("UNRECOGNIZED", rendered)
        self.assertIn("'victory'=1", rendered)
        self.assertIn("NOT checked against the official score", rendered)

    def test_unevaluable_candidates_with_a_legacy_price_are_counted(self):
        import tempfile
        root = Path(tempfile.mkdtemp())
        day = audit.audit_day(
            write_day(root, "2026-06-05", {
                "date": "2026-06-05",
                "candidates": [
                    {"game": "A at B", "side": "A", "polymarket_ask": 0.5},
                    {"game": "C at D", "side": "C"},
                ],
            }),
            root / "results", 0.05,
        )
        process = audit.aggregate([day], 0.05, 0.05, 20)["process"]
        self.assertEqual(process["floor_verdict"], {"unevaluable": 2})
        # Only the card that actually carries a price is counted; the caveat
        # must never claim more coverage than exists.
        self.assertEqual(process["floor_unevaluable_with_a_legacy_price"], 1)


class ReadOnlyAndProvenanceTests(unittest.TestCase):
    """The audit must stay a report. These pin that it cannot become anything else.

    Deliberately a separate guard from `test_observability_adds_no_execution`
    rather than a fourth root added to it. That guard forbids an observability
    module from NAMING `min_conservative_edge`, because a module that writes a
    rail is deciding what gets bet. This audit legitimately reads that floor —
    it is the thing being reported against — so folding it in would mean
    deleting a token from someone else's guard to accommodate a module it was
    not written for, which weakens a check that is doing its job. The read-only
    properties that do apply are pinned here instead.
    """

    def test_the_default_floor_is_the_repo_constant_not_a_second_copy_of_it(self):
        # A hard-coded 0.05 here would agree with the gate exactly until the day
        # the gate's default moved, and then disagree silently.
        self.assertIs(
            audit.DEFAULT_MIN_CONSERVATIVE_EDGE, mlb_runtime_policy.DEFAULT_MIN_CONSERVATIVE_EDGE
        )
        self.assertNotIn("0.05", _floor_default_source())

    def test_sibling_imports_are_pinned_to_the_declared_set(self):
        # Reds on any new dependency, including one on the execution path. The
        # audit reaching execution_guard or the review gate would not be caught
        # by a token scan, but it is caught here.
        self.assertEqual(
            import_closure.sibling_imports(MODULE),
            {"mlb_final_scores.py", "mlb_runtime_policy.py",
             "vig_calibration_report.py", "http_util.py"},
        )

    def test_every_sibling_import_is_name_scoped(self):
        # A whole-module bind takes every name on the module, which is strictly
        # wider than the four this needs and is invisible to the check above.
        tree = ast.parse((SCRIPTS / MODULE).read_text(encoding="utf-8"))
        bound_whole = [
            alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
            for alias in node.names
            if (SCRIPTS / f"{alias.name.split('.', 1)[0]}.py").is_file()
        ]
        self.assertEqual(bound_whole, [])

    def test_the_audit_names_no_execution_entrypoint(self):
        text = (SCRIPTS / MODULE).read_text(encoding="utf-8").lower()
        for token in ("create_order", "post_order", "submit_order", "place_order",
                      "sign_order", "clob", "order_args", "private_key",
                      "polymarket_us_sdk_bet", "execution_guard"):
            with self.subTest(token=token):
                self.assertNotIn(token, text)

    def test_the_audit_never_writes_outside_the_results_fetch_helper(self):
        # Not "it does not write" — it does, once, behind --fetch, into the
        # results cache. The claim is that every write is inside that one
        # function, and the vacuity guard is that writes exist to be found.
        tree = ast.parse((SCRIPTS / MODULE).read_text(encoding="utf-8"))
        fetcher = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "fetch_missing_results"
        )
        writes = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("write_text", "write_bytes", "mkdir", "unlink", "rmtree")
        ]
        self.assertTrue(writes, "no write calls found at all — this guard would be vacuous")
        for node in writes:
            with self.subTest(line=node.lineno):
                self.assertTrue(
                    fetcher.lineno <= node.lineno <= (fetcher.end_lineno or fetcher.lineno),
                    "a write escaped fetch_missing_results",
                )

    def test_official_results_carry_their_provenance(self):
        import tempfile
        root = Path(tempfile.mkdtemp())
        write_results(root, "2026-07-08", [
            {"away": "Athletics", "home": "Detroit Tigers", "away_score": 1, "home_score": 6},
        ])
        rows, unfinished, provenance = audit.load_official_rows(root / "results", "2026-07-08")
        self.assertEqual(provenance["status"], "ok")
        self.assertEqual(provenance["source"], "mlb-statsapi-schedule")
        self.assertIn("2026-07-08", provenance["url"])
        self.assertTrue(provenance["cache_path"].endswith("2026-07-08.json"))
        self.assertEqual(provenance["final_games"], 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(unfinished, [])

    def test_a_malformed_cached_payload_is_not_a_valid_empty_day(self):
        # Reviewer's medium at tip 6e7d551: any JSON object used to be handed
        # to final_scores and stamped "ok", so a corrupt cache read as a day
        # with no Final games and every candidate on it became
        # `no_official_game` — a claim that official data was sourced and the
        # game wasn't in it. A payload without the Stats API's `dates` list is
        # now refused before any row is derived, and the day is unreconciled.
        import tempfile
        root = Path(tempfile.mkdtemp())
        results = root / "results"
        results.mkdir(parents=True)
        for name, payload in (("2026-07-08", {"error": "rate limited"}), ("2026-07-09", {"dates": "nope"})):
            with self.subTest(payload=payload):
                (results / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
                rows, unfinished, provenance = audit.load_official_rows(results, name)
                self.assertIsNone(rows)
                self.assertEqual(unfinished, [])
                self.assertEqual(provenance["status"], "malformed: payload has no 'dates' list")
                self.assertNotIn("final_games", provenance)
        # And at the day level the candidate is unreconciled, never
        # `no_official_game`.
        write_day(root, "2026-07-08", {
            "date": "2026-07-08",
            "candidates": [{"game": "Athletics at Detroit Tigers", "side": "DET"}],
        })
        day = audit.audit_day(root / "execute" / "2026-07-08-schedule.json", results, 0.05)
        self.assertEqual(day["results_provenance"]["status"], "malformed: payload has no 'dates' list")
        record = day["candidates"][0]
        self.assertEqual(record["side_outcome"], "unreconciled")
        self.assertIsNone(record["match_method"])
        self.assertEqual(record["unreconciled_reason"], "no cached official results for this date")

    def test_a_missing_results_payload_is_reported_as_missing(self):
        import tempfile
        root = Path(tempfile.mkdtemp())
        rows, unfinished, provenance = audit.load_official_rows(root, "2026-07-08")
        self.assertIsNone(rows)
        self.assertEqual(unfinished, [])
        self.assertEqual(provenance["status"], "missing")

    def test_the_result_comes_from_the_repos_own_final_scores_function(self):
        # Provenance in the strongest available form: the audit does not have
        # its own idea of who won.
        self.assertIs(audit.final_scores, mlb_final_scores.final_scores)
        self.assertIs(audit.SCHEDULE_URL, mlb_final_scores.SCHEDULE_URL)


def _floor_default_source() -> str:
    """The argparse default line, so a re-hard-coded 0.05 is visible."""
    text = (SCRIPTS / MODULE).read_text(encoding="utf-8")
    return next(line for line in text.splitlines() if '"--edge-floor"' in line)


class CliTests(unittest.TestCase):
    def test_a_missing_schedule_directory_is_an_error_not_an_empty_report(self):
        import tempfile
        with self.assertRaises(audit.AuditError):
            audit.schedule_paths(Path(tempfile.mkdtemp()) / "execute", None, None)

    def test_the_date_range_filters_by_filename_date(self):
        import tempfile
        root = Path(tempfile.mkdtemp())
        for date in ("2026-06-01", "2026-06-02", "2026-06-03"):
            write_day(root, date, {"date": date, "candidates": []})
        (root / "execute" / "notes.txt").write_text("ignored", encoding="utf-8")
        paths = audit.schedule_paths(root / "execute", "2026-06-02", "2026-06-03")
        self.assertEqual([p.name for p in paths],
                         ["2026-06-02-schedule.json", "2026-06-03-schedule.json"])

    def test_the_report_renders_without_official_results(self):
        import tempfile
        root = Path(tempfile.mkdtemp())
        write_day(root, "2026-06-01", {
            "date": "2026-06-01",
            "candidates": [{"game": "A at B", "side": "A", "price": "-110"}],
        })
        report = audit.build_report(root / "execute", root / "results", 0.05, None, None, 0.05, 20)
        rendered = audit.render(report)
        self.assertIn("MLB historical pick audit", rendered)
        self.assertIn("no cached official results", str(report["aggregate"]["candidates"]))


if __name__ == "__main__":
    unittest.main()
