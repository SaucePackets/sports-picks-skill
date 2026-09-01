import importlib
import json
import sys
import unittest
import unittest.mock
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts import vig_slate_gate_replay as replay
from scripts.vig_slate_gate_replay import (
    CANONICAL_CLUBS,
    EDGE_COMPARISON_EPSILON,
    FAIR_AGREEMENT_TOLERANCE,
    POPULATIONS,
    ReplayError,
    apply_daily_cap,
    build_report,
    canonical_club,
    extract_blocks,
    gate_edge,
    grade,
    looks_like_matchup,
    outcome_index,
    parse_probability,
    replay_policy,
    replay_sides,
    resolve_ask,
    resolve_dk_fair,
    resolve_haircut,
    resolve_handicap,
    resolve_side,
    select_hypothetical,
    split_matchup,
)

AWAY = "Seattle Mariners"
HOME = "New York Yankees"


def rebound(module_name, attribute, replacement, call):
    """Rebind a name in the SOURCE module and re-run the code that consults it.

    The module under test binds its imports by name at import time, so patching
    the source module alone changes nothing — the proof of consultation is that
    reloading picks the replacement up. The patch targets the BARE module in
    sys.modules because the module under test imported it bare while this test
    package imports it as `scripts.<name>`: two module objects for one file, and
    patching the wrong one silently proves nothing.

    Reloading rebuilds the module's classes, so anything compared by identity
    afterwards must be read off `replay.` rather than from this file's top-level
    imports. The final reload restores the real bindings.
    """
    source = sys.modules[module_name]
    try:
        with unittest.mock.patch.object(source, attribute, replacement) as patched:
            result = call(importlib.reload(replay))
    finally:
        # OUTSIDE the with-block on purpose. Reloading while the patch is still
        # installed re-imports the mock permanently and leaks it into every
        # later test — which is how this helper first failed.
        importlib.reload(replay)
    return patched, result


def schedule_payload(games):
    """A minimal MLB schedule payload: [(away, home, status, away_score, home_score)]."""
    return {
        "dates": [
            {
                "games": [
                    {
                        "gamePk": 900000 + index,
                        "status": {"detailedState": status},
                        "teams": {
                            "away": {"team": {"name": away}, "score": away_score,
                                     "isWinner": away_score > home_score},
                            "home": {"team": {"name": home}, "score": home_score,
                                     "isWinner": home_score > away_score},
                        },
                    }
                    for index, (away, home, status, away_score, home_score)
                    in enumerate(games)
                ]
            }
        ]
    }


class ProbabilityTokenTests(unittest.TestCase):
    def test_percent_and_fraction_forms_both_read(self):
        self.assertAlmostEqual(parse_probability("58.5%"), 0.585)
        self.assertAlmostEqual(parse_probability("0.585"), 0.585)
        self.assertAlmostEqual(parse_probability(".585"), 0.585)

    def test_a_bare_number_above_one_is_refused_not_divided_by_a_hundred(self):
        """`fair 58.5` could be a percentage or a typo, and choosing is imputation."""
        self.assertIsNone(parse_probability("58.5"))

    def test_the_open_interval_is_enforced_at_both_ends(self):
        self.assertIsNone(parse_probability("0"))
        self.assertIsNone(parse_probability("1.0"))
        self.assertIsNone(parse_probability("100%"))

    def test_unreadable_tokens_return_none_rather_than_raising(self):
        for token in ("", "   ", "abc", "%", None):
            self.assertIsNone(parse_probability(token))


class ClubResolutionTests(unittest.TestCase):
    def test_the_club_list_is_derived_and_covers_the_league(self):
        self.assertEqual(len(CANONICAL_CLUBS), 30)

    def test_full_name_nickname_and_abbreviation_all_resolve(self):
        self.assertEqual(canonical_club("New York Yankees"), "New York Yankees")
        self.assertEqual(canonical_club("Yankees"), "New York Yankees")
        self.assertEqual(canonical_club("NYY"), "New York Yankees")

    def test_a_city_only_token_resolves_when_the_city_names_one_club(self):
        """2026-08-21 titles every game as "Atlanta at Milwaukee"."""
        self.assertEqual(canonical_club("Atlanta"), "Atlanta Braves")
        self.assertEqual(canonical_club("Milwaukee"), "Milwaukee Brewers")

    def test_a_city_shared_by_two_clubs_is_refused(self):
        for city in ("Chicago", "New York", "Los Angeles"):
            self.assertIsNone(canonical_club(city), city)

    def test_the_doubled_nickname_a_run_wrote_resolves_through_the_table(self):
        """"Athletics Athletics" is a "{city} {nickname}" template on a club with no city."""
        self.assertEqual(canonical_club("Athletics Athletics"), "Athletics")

    def test_a_doubled_token_that_is_not_a_club_still_refuses(self):
        """The collapse is retried through the resolver, so it cannot invent a name."""
        self.assertIsNone(canonical_club("Rovers Rovers"))

    def test_a_nickname_shared_by_two_clubs_is_refused(self):
        """"Sox" is a suffix of both Boston's and Chicago's official names.

        Ambiguity has to refuse at the nickname/abbreviation step too, not only
        at the city step: preferring the first match would put half the games in
        a Sox series on the wrong club.
        """
        self.assertIsNone(canonical_club("Sox"))
        self.assertEqual(canonical_club("Red Sox"), "Boston Red Sox")
        self.assertEqual(canonical_club("White Sox"), "Chicago White Sox")

    def test_an_unknown_token_refuses(self):
        self.assertIsNone(canonical_club("Montreal Expos"))
        self.assertIsNone(canonical_club(""))


class SideResolutionTests(unittest.TestCase):
    def test_each_side_resolves_to_the_club_it_names(self):
        self.assertEqual(resolve_side("SEA", AWAY, HOME), "away")
        self.assertEqual(resolve_side("NYY", AWAY, HOME), "home")

    def test_a_token_matching_neither_side_refuses(self):
        self.assertIsNone(resolve_side("BOS", AWAY, HOME))

    def test_a_token_matching_both_sides_refuses_rather_than_preferring_one(self):
        self.assertIsNone(resolve_side("NYY", HOME, HOME))


class MatchupTitleTests(unittest.TestCase):
    def test_a_bullet_title_keeps_its_colon_outside_the_parenthetical(self):
        """The regression that broke every side lookup in every bullet block.

        Stripping the parenthetical before the trailing colon left
        "New York Yankees (6:05 PM CT):" as the home club, so no price token in
        the block could resolve and the whole bullet corpus read as unpriced.
        """
        parsed = split_matchup("Seattle Mariners at New York Yankees (6:05 PM CT):")
        self.assertEqual(parsed["away"], AWAY)
        self.assertEqual(parsed["home"], HOME)

    def test_a_dash_suffix_is_stripped(self):
        parsed = split_matchup("Boston Red Sox at Miami Marlins — 5:40 PM CT")
        self.assertEqual((parsed["away"], parsed["home"]),
                         ("Boston Red Sox", "Miami Marlins"))

    def test_a_comma_time_suffix_is_stripped_and_retained(self):
        parsed = split_matchup("Astros at Mets, 20:10Z")
        self.assertEqual((parsed["away"], parsed["home"]),
                         ("Houston Astros", "New York Mets"))
        self.assertEqual(parsed["suffix"], "20:10Z")
        self.assertIsNone(parsed["doubleheader_marker"])

    def test_a_doubleheader_marker_is_read_from_either_position(self):
        """"Giants DH1, 20:05Z" and "Giants, DH2" both occur in the window."""
        attached = split_matchup("Diamondbacks at Giants DH1, 20:05Z — pass:")
        self.assertEqual(attached["home"], "San Francisco Giants")
        self.assertEqual(attached["doubleheader_marker"].upper(), "DH1")
        suffixed = split_matchup("Red Sox at Yankees, DH2")
        self.assertEqual(suffixed["home"], HOME)
        self.assertEqual(suffixed["doubleheader_marker"].upper(), "DH2")

    def test_every_spelling_of_one_game_collapses_to_one_key(self):
        """Three spellings counted as three games and pushed a day past 100%."""
        keys = {
            (split_matchup(title)["away"], split_matchup(title)["home"])
            for title in (
                "Boston Red Sox at New York Yankees, DH2",
                "Red Sox at Yankees, 17:05Z",
                "Boston at New York Yankees — 5:05 PM CT",
            )
        }
        self.assertEqual(len(keys), 1)

    def test_a_title_with_no_resolvable_pair_refuses(self):
        self.assertIsNone(split_matchup("Clean read"))
        self.assertIsNone(split_matchup("Montreal Expos at Brooklyn Dodgers"))

    def test_extraction_does_not_depend_on_resolution(self):
        """A block boundary must survive a club the table cannot resolve.

        When extraction called the resolver, an unresolvable title stopped being
        a boundary and its text was absorbed into the PREVIOUS game's body —
        one unreadable title corrupting two games instead of reporting one.
        """
        self.assertTrue(looks_like_matchup("Montreal Expos at Brooklyn Dodgers"))
        self.assertIsNone(split_matchup("Montreal Expos at Brooklyn Dodgers"))
        self.assertFalse(looks_like_matchup("Clean read"))


class OrientationTests(unittest.TestCase):
    """The join this module could get wrong in silence: which side is which."""

    def test_club_tokens_decide_the_sides_and_the_written_order_does_not(self):
        result = resolve_ask("PM asks NYY 0.550/SEA 0.455", AWAY, HOME)
        self.assertEqual(result["provenance"], "recorded")
        self.assertAlmostEqual(result["values"]["away"], 0.455)
        self.assertAlmostEqual(result["values"]["home"], 0.550)

    def test_crossing_one_name_is_caught_as_well_as_crossing_both(self):
        """A single crossed side is the same backwards row as two crossed sides."""
        straight = resolve_ask("Polymarket ask SEA 0.455 / NYY 0.550", AWAY, HOME)
        crossed = resolve_ask("Polymarket ask NYY 0.550 / SEA 0.455", AWAY, HOME)
        self.assertEqual(straight["values"], crossed["values"])
        one_crossed = resolve_ask("Polymarket ask NYY 0.550 / NYY 0.455", AWAY, HOME)
        self.assertIsNone(one_crossed["values"])
        self.assertIn("home side", one_crossed["reason"])

    def test_an_unlabelled_pair_is_carried_but_labelled_inferred_order(self):
        result = resolve_ask("Polymarket ask 0.455 / 0.550", AWAY, HOME)
        self.assertEqual(result["provenance"], "inferred_order")
        self.assertAlmostEqual(result["values"]["away"], 0.455)

    def test_a_token_that_resolves_to_neither_side_refuses_the_pair(self):
        result = resolve_ask("Polymarket ask BOS 0.455 / TOR 0.550", AWAY, HOME)
        self.assertIsNone(result["values"])
        self.assertEqual(result["provenance"], "unavailable")

    def test_the_two_sided_ask_sum_is_carried_for_the_unrecorded_spread(self):
        result = resolve_ask("PM asks SEA 0.455/NYY 0.550", AWAY, HOME)
        self.assertAlmostEqual(result["two_sided_sum"], 1.005)

    def test_a_block_with_no_ask_phrase_reports_a_reason(self):
        result = resolve_ask("no price here", AWAY, HOME)
        self.assertEqual(result["provenance"], "unavailable")
        self.assertTrue(result["reason"])


class FairProbabilityTests(unittest.TestCase):
    LINE = "DK SEA +137 / NYY -147"

    def test_the_american_line_is_de_vigged_and_labelled_reconstructed(self):
        result = resolve_dk_fair(self.LINE, AWAY, HOME)
        self.assertEqual(result["provenance"], "reconstructed")
        self.assertAlmostEqual(result["values"]["away"], 0.4152, places=3)
        self.assertAlmostEqual(
            result["values"]["away"] + result["values"]["home"], 1.0, places=9
        )

    def test_the_de_vig_is_the_scan_s_own_function_not_a_second_copy(self):
        """Rebind the SOURCE and require the answer to follow.

        An equality assertion against a hand-written literal is satisfied by any
        formula that happens to agree; only a rebind proves consultation. The
        patch targets the bare module in sys.modules, because the module under
        test imported `mlb_stage2_scan` bare while the test package imports
        `scripts.mlb_stage2_scan` — two module objects for one file.
        """
        patched, result = rebound(
            "mlb_stage2_scan", "devig",
            unittest.mock.Mock(return_value=(0.25, 0.75)),
            lambda module: module.resolve_dk_fair(self.LINE, AWAY, HOME),
        )
        self.assertTrue(patched.called)
        self.assertEqual(result["values"], {"away": 0.25, "home": 0.75})
        # And the real function is back afterwards, so the pin cannot leak.
        self.assertNotEqual(
            resolve_dk_fair(self.LINE, AWAY, HOME)["values"], {"away": 0.25, "home": 0.75}
        )

    def test_a_stated_fair_agreeing_with_the_line_is_recorded_as_agreement(self):
        body = f"{self.LINE} -> de-vig fair SEA 0.415 / NYY 0.585"
        result = resolve_dk_fair(body, AWAY, HOME)
        self.assertEqual(result["cross_check"], "agree")
        self.assertEqual(result["provenance"], "reconstructed")

    def test_a_stated_fair_disagreeing_with_the_line_refuses_rather_than_prefers(self):
        """Disagreement means a pattern matched something it should not have."""
        body = f"{self.LINE} -> de-vig fair SEA 0.700 / NYY 0.300"
        result = resolve_dk_fair(body, AWAY, HOME)
        self.assertEqual(result["cross_check"], "disagree")
        self.assertIsNone(result["values"])
        self.assertEqual(result["provenance"], "unavailable")

    def test_disagreement_is_measured_against_the_stated_tolerance(self):
        inside = resolve_dk_fair(
            f"{self.LINE} -> fair SEA {0.4152 + FAIR_AGREEMENT_TOLERANCE / 2:.4f} "
            f"/ NYY {0.5848 - FAIR_AGREEMENT_TOLERANCE / 2:.4f}",
            AWAY, HOME,
        )
        self.assertEqual(inside["cross_check"], "agree")

    def test_a_stated_fair_alone_is_usable_and_labelled_recorded(self):
        result = resolve_dk_fair("DK fair SEA 0.415 / NYY 0.585", AWAY, HOME)
        self.assertEqual(result["provenance"], "recorded")

    def test_a_side_labelled_reading_outranks_an_unlabelled_one(self):
        body = "DK SEA +137 / NYY -147 -> fair 0.415 / 0.585"
        self.assertEqual(resolve_dk_fair(body, AWAY, HOME)["provenance"], "reconstructed")

    def test_an_unlabelled_line_is_still_carried_as_inferred_order(self):
        result = resolve_dk_fair("DraftKings +137/-147", AWAY, HOME)
        self.assertEqual(result["provenance"], "inferred_order")

    def test_a_block_with_no_price_at_all_reports_a_reason(self):
        result = resolve_dk_fair("DraftKings unavailable, game in progress", AWAY, HOME)
        self.assertIsNone(result["values"])
        self.assertTrue(result["reason"])


class HandicapAndHaircutTests(unittest.TestCase):
    def test_a_stated_win_probability_with_a_side_is_recorded(self):
        result = resolve_handicap("win probability NYY 0.600 gives +0.050", AWAY, HOME)
        self.assertEqual((result["side"], result["value"]), ("home", 0.6))

    def test_a_win_probability_with_no_resolvable_side_is_refused(self):
        """A one-sided number has no writing convention to fall back on.

        Guessing puts the handicap on the wrong club half the time, and a
        wrongly-sided handicap is a counted, anti-correlated row.
        """
        result = resolve_handicap("provisional win probability 0.510", AWAY, HOME)
        self.assertIsNone(result["side"])
        self.assertEqual(result["provenance"], "unavailable")

    def test_zero_is_a_legal_haircut(self):
        """The market-only fallback's own contract value.

        Checking this field with the probability rule would make it unwritable
        in exactly the configuration that dominates the window.
        """
        result = resolve_haircut("0 uncertainty haircut, still no edge")
        self.assertEqual(result["value"], 0.0)
        self.assertEqual(result["provenance"], "recorded")

    def test_a_haircut_reads_in_either_word_order(self):
        self.assertAlmostEqual(resolve_haircut("0.020 uncertainty haircut")["value"], 0.02)
        self.assertAlmostEqual(
            resolve_haircut("an uncertainty haircut of 0.020")["value"], 0.02
        )

    def test_a_haircut_of_one_or_more_is_refused(self):
        self.assertIsNone(resolve_haircut("uncertainty haircut of 1.5")["value"])

    def test_an_absent_haircut_reports_a_reason(self):
        result = resolve_haircut("no buffer mentioned")
        self.assertIsNone(result["value"])
        self.assertTrue(result["reason"])


class GateArithmeticTests(unittest.TestCase):
    def test_the_edge_is_the_gate_s_own_function_not_a_subtraction(self):
        """Rebind the source and require the answer to follow."""
        patched, value = rebound(
            "mlb_runtime_policy", "live_conservative_edge",
            unittest.mock.Mock(return_value=0.123),
            lambda module: module.gate_edge(0.6, 0.5),
        )
        self.assertTrue(patched.called)
        self.assertEqual(value, 0.123)
        self.assertAlmostEqual(gate_edge(0.6, 0.5), 0.1)

    def test_the_edge_matches_conservative_minus_ask(self):
        self.assertAlmostEqual(gate_edge(0.6, 0.55), 0.05)

    def test_a_probability_the_gate_refuses_yields_no_edge(self):
        self.assertIsNone(gate_edge(1.5, 0.55))
        self.assertIsNone(gate_edge(0.6, 0.0))

    def test_an_edge_exactly_on_the_floor_clears_it(self):
        sides = replay_sides(
            {"values": {"away": 0.60, "home": 0.40}},
            {"values": {"away": 0.55, "home": 0.45}},
            {"side": None}, {"value": None}, 0.05,
        )
        self.assertTrue(sides["market_only"]["away"]["cleared"])
        self.assertIsNone(sides["market_only"]["away"]["shortfall"])

    def test_a_shortfall_is_reported_as_a_number_not_a_category(self):
        sides = replay_sides(
            {"values": {"away": 0.60, "home": 0.40}},
            {"values": {"away": 0.58, "home": 0.42}},
            {"side": None}, {"value": None}, 0.05,
        )
        away = sides["market_only"]["away"]
        self.assertFalse(away["cleared"])
        self.assertAlmostEqual(away["shortfall"], 0.03)
        self.assertIn("short of the", away["stop_reason"])
        self.assertIn("0.0200", away["stop_reason"])

    def test_the_market_only_fallback_charges_no_haircut(self):
        sides = replay_sides(
            {"values": {"away": 0.60, "home": 0.40}},
            {"values": {"away": 0.50, "home": 0.50}},
            {"side": None}, {"value": None}, 0.05,
        )
        away = sides["market_only"]["away"]
        self.assertEqual(away["uncertainty_haircut"], 0.0)
        self.assertEqual(away["raw_probability"], away["conservative_probability"])

    def test_a_recorded_handicap_without_a_recorded_haircut_is_not_completed_with_zero(self):
        """Zero is the fallback's value; borrowing it relabels the population."""
        sides = replay_sides(
            {"values": {"away": 0.60, "home": 0.40}},
            {"values": {"away": 0.50, "home": 0.50}},
            {"side": "away", "value": 0.66}, {"value": None}, 0.05,
        )
        self.assertFalse(sides["recorded_handicap"]["evaluable"])
        self.assertIn("haircut", sides["recorded_handicap"]["reason"])

    def test_a_recorded_handicap_with_a_haircut_evaluates_on_its_own_side(self):
        sides = replay_sides(
            {"values": {"away": 0.60, "home": 0.40}},
            {"values": {"away": 0.50, "home": 0.50}},
            {"side": "away", "value": 0.66}, {"value": 0.02}, 0.05,
        )
        handicap = sides["recorded_handicap"]
        self.assertEqual(handicap["side"], "away")
        self.assertAlmostEqual(handicap["conservative_probability"], 0.64)
        self.assertAlmostEqual(handicap["conservative_edge"], 0.14)

    def test_a_missing_price_makes_the_market_population_unevaluable_not_zero(self):
        sides = replay_sides(
            {"values": None}, {"values": {"away": 0.5, "home": 0.5}},
            {"side": None}, {"value": None}, 0.05,
        )
        self.assertFalse(sides["market_only"]["away"]["evaluable"])
        self.assertEqual(sides["market_only"]["away"]["reason"], "dk_fair")


class SelectionTests(unittest.TestCase):
    def side(self, edge, cleared=True):
        return {"evaluable": True, "cleared": cleared, "conservative_edge": edge,
                "conservative_probability": 0.6, "current_ask": 0.6 - edge}

    def test_the_larger_clearing_edge_wins(self):
        chosen = select_hypothetical({"away": self.side(0.06), "home": self.side(0.08)})
        self.assertEqual(chosen["side"], "home")

    def test_a_side_below_the_floor_is_never_selected(self):
        self.assertIsNone(select_hypothetical({"away": self.side(0.01, cleared=False)}))

    def test_a_tie_is_refused_rather_than_broken_by_key_order(self):
        chosen = select_hypothetical({"away": self.side(0.06), "home": self.side(0.06)})
        self.assertTrue(chosen["tie"])
        self.assertEqual(chosen["sides"], ["away", "home"])

    def test_the_daily_cap_keeps_the_best_edges_and_drops_the_rest(self):
        """Ranked by EDGE, not by the order a card happened to list them.

        The tags are deliberately out of edge order — a cap that kept the first
        two entries would pass a test whose fixture ranked them the same way.
        """
        selections = [
            {"conservative_probability": 0.60, "current_ask": 0.54, "tag": "a"},  # 0.06
            {"conservative_probability": 0.70, "current_ask": 0.61, "tag": "b"},  # 0.09
            {"conservative_probability": 0.80, "current_ask": 0.72, "tag": "c"},  # 0.08
        ]
        result = apply_daily_cap(selections, replay_policy(0.05, 2))
        self.assertEqual([s["tag"] for s in result["kept"]], ["b", "c"])
        self.assertEqual([s["tag"] for s in result["dropped"]], ["a"])

    def test_the_cap_is_the_gate_s_own_limiter(self):
        patched, result = rebound(
            "mlb_runtime_policy", "enforce_daily_candidate_limit",
            unittest.mock.Mock(return_value=([], [])),
            lambda module: module.apply_daily_cap(
                [{"conservative_probability": 0.6, "current_ask": 0.5}],
                module.replay_policy(0.05, 2),
            ),
        )
        self.assertTrue(patched.called)
        self.assertEqual(result["kept"], [])


class GradingTests(unittest.TestCase):
    SELECTION = {"tie": False, "side": "away", "current_ask": 0.4}

    def test_a_winning_side_returns_the_ask_s_payoff(self):
        result = grade(self.SELECTION, {"winner_side": "away"})
        self.assertTrue(result["won"])
        self.assertAlmostEqual(result["units"], 1.5)

    def test_a_losing_side_returns_one_unit(self):
        result = grade(self.SELECTION, {"winner_side": "home"})
        self.assertFalse(result["won"])
        self.assertEqual(result["units"], -1.0)

    def test_a_missing_final_is_ungraded_and_carries_no_units(self):
        """"No evidence" and "broke even" are different facts."""
        result = grade(self.SELECTION, None)
        self.assertFalse(result["graded"])
        self.assertNotIn("units", result)

    def test_an_undecided_game_is_ungraded(self):
        result = grade(self.SELECTION, {"winner_side": None, "status": "Postponed"})
        self.assertFalse(result["graded"])

    def test_no_selection_is_ungraded(self):
        self.assertFalse(grade(None, {"winner_side": "away"})["graded"])
        self.assertFalse(grade({"tie": True}, {"winner_side": "away"})["graded"])


class OutcomeJoinTests(unittest.TestCase):
    def record(self, away=AWAY, home=HOME):
        return {"parsed": True, "away_team": away, "home_team": home}

    def test_a_unique_final_joins_and_carries_the_winning_side(self):
        index = outcome_index(schedule_payload([(AWAY, HOME, "Final", 5, 3)]))
        joined = replay._join_outcome(self.record(), index)
        self.assertTrue(joined["joined"])
        self.assertEqual(joined["record"]["winner_side"], "away")

    def test_a_doubleheader_refuses_the_join_rather_than_grading_the_wrong_game(self):
        """A matchup-keyed dict keeps only the last game; the first would be
        graded against the second's result, silently and anti-correlated."""
        index = outcome_index(schedule_payload([
            (AWAY, HOME, "Final", 5, 3),
            (AWAY, HOME, "Final", 1, 8),
        ]))
        joined = replay._join_outcome(self.record(), index)
        self.assertFalse(joined["joined"])
        self.assertIn("doubleheader", joined["reason"])

    def test_a_transposed_match_is_detected_and_refused_not_corrected(self):
        """Flipping it would assume the prose is wrong about which club is home."""
        index = outcome_index(schedule_payload([(HOME, AWAY, "Final", 5, 3)]))
        joined = replay._join_outcome(self.record(), index)
        self.assertFalse(joined["joined"])
        self.assertIn("swapped", joined["reason"])

    def test_an_unplayed_game_is_absent_rather_than_final(self):
        index = outcome_index(schedule_payload([(AWAY, HOME, "In Progress", 2, 1)]))
        self.assertFalse(replay._join_outcome(self.record(), index)["joined"])

    def test_an_absent_payload_yields_an_empty_index(self):
        self.assertEqual(outcome_index(None), {})


class SectionExtractionTests(unittest.TestCase):
    DOC = """# Slate

## Official card right now

- nothing

## Full-slate game-by-game read

### Seattle Mariners at New York Yankees — 6:05 PM CT
**Price:** DK SEA +137 / NYY -147 -> fair SEA 0.415 / NYY 0.585. PM asks SEA 0.420 / NYY 0.585.
**Pass:** no edge.

### Boston Red Sox at Miami Marlins — 5:40 PM CT
**Price:** DK BOS -147 / MIA +137 -> fair BOS 0.585 / MIA 0.415.

## Clean read

- done
"""

    def test_only_the_read_sections_yield_blocks(self):
        extracted = extract_blocks(self.DOC)
        self.assertEqual(len(extracted["blocks"]), 2)
        self.assertEqual(extracted["sections"], ["Full-slate game-by-game read"])
        self.assertEqual(extracted["unknown_sections"], [])

    def test_a_block_body_stops_at_the_next_block(self):
        first = extract_blocks(self.DOC)["blocks"][0]
        self.assertIn("PM asks", first["body"])
        self.assertNotIn("MIA +137", first["body"])

    def test_the_bullet_shape_is_read_as_well_as_the_subsection_shape(self):
        doc = (
            "## Full-slate pass notes\n\n"
            "- **Seattle Mariners at New York Yankees (6:05 PM CT):** "
            "DK fair NYY 0.5446; PM asks NYY 0.550/SEA 0.455.\n"
        )
        blocks = extract_blocks(doc)["blocks"]
        self.assertEqual(len(blocks), 1)
        self.assertIn("PM asks", blocks[0]["body"])

    def test_an_unresolvable_title_costs_one_block_and_not_two(self):
        """The coupling defect, measured where it actually did damage.

        When extraction called the resolver, a title naming a club the table
        cannot resolve stopped being a block boundary, so its prose — including
        its prices — was appended to the PREVIOUS game's body. The previous game
        then carried a second game's numbers and was still reported as parsed.
        """
        doc = (
            "## Full-slate pass notes\n\n"
            "- **Seattle Mariners at New York Yankees:** PM asks SEA 0.455 / NYY 0.550.\n"
            "- **Montreal Expos at Brooklyn Dodgers:** PM asks 0.900 / 0.105.\n"
            "- **Boston Red Sox at Miami Marlins:** PM asks BOS 0.585 / MIA 0.420.\n"
        )
        blocks = extract_blocks(doc)["blocks"]
        self.assertEqual(len(blocks), 3)
        self.assertNotIn("0.900", blocks[0]["body"])
        first = replay.replay_block(blocks[0], 0.05)
        self.assertAlmostEqual(first["inputs"]["polymarket_ask"]["values"]["away"], 0.455)
        self.assertFalse(replay.replay_block(blocks[1], 0.05)["parsed"])

    def test_an_unrecognised_section_heading_is_reported_not_ignored(self):
        """A seventh spelling would otherwise cost a whole document in silence."""
        doc = "## Tonight's game-by-game rundown\n\n### A at B\ntext\n"
        self.assertEqual(
            extract_blocks(doc)["unknown_sections"], ["Tonight's game-by-game rundown"]
        )

    def test_a_known_non_read_heading_is_not_reported_as_unknown(self):
        doc = "## Sticks out, but pass\n\n### A at B\ntext\n"
        self.assertEqual(extract_blocks(doc)["unknown_sections"], [])

    def test_a_heading_with_a_dash_suffix_still_matches_the_closed_set(self):
        doc = (
            "## Lineup watchlist — recheck ~75 minutes before first pitch\n\n"
            "- **A at B:** text\n"
        )
        self.assertEqual(extract_blocks(doc)["unknown_sections"], [])


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "runtime" / ".picks"
        (self.root / "slate").mkdir(parents=True)
        (self.root / "execute").mkdir()
        (self.root / "audit-results").mkdir()

    def write_day(self, date, doc, games):
        (self.root / "slate" / f"{date}.md").write_text(doc, encoding="utf-8")
        (self.root / "audit-results" / f"{date}.json").write_text(
            json.dumps(schedule_payload(games)), encoding="utf-8"
        )

    def report(self, **kwargs):
        params = {
            "picks_dir": self.root, "extra_picks_dirs": [],
            "since": replay.parse_date("2026-08-11"),
            "until": replay.parse_date("2026-08-11"),
            "results_dir": None, "edge_floor": 0.05,
            "max_bets_per_day": 2, "repo_revision": "abc123",
        }
        params.update(kwargs)
        return replay.build_report(**params)

    def test_a_clearing_game_is_selected_graded_and_traceable(self):
        self.write_day(
            "2026-08-11",
            "## Full-slate read\n\n### Seattle Mariners at New York Yankees — 6:05 PM CT\n"
            "**Price:** DK fair SEA 0.415 / NYY 0.585. PM asks SEA 0.350 / NYY 0.660.\n",
            [(AWAY, HOME, "Final", 7, 2)],
        )
        report = self.report()
        stats = report["populations"]["market_only"]
        self.assertEqual(stats["games_clearing_floor"], 1)
        self.assertEqual(stats["games_within_daily_cap"], 1)
        pick = stats["selections"][0]
        self.assertEqual(pick["side"], "away")
        self.assertAlmostEqual(pick["conservative_edge"], 0.065)
        self.assertTrue(pick["grade"]["won"])
        self.assertTrue(pick["faithful_inputs"])

    def test_the_daily_cap_bounds_what_a_card_could_have_bet(self):
        blocks = "\n".join(
            f"### {away} at {home} — 6:05 PM CT\n"
            f"**Price:** DK fair {away} 0.600 / {home} 0.400. "
            f"PM asks {away} {ask:.3f} / {home} 0.600.\n"
            for (away, home), ask in zip(
                [("Seattle Mariners", "New York Yankees"),
                 ("Boston Red Sox", "Miami Marlins"),
                 ("Atlanta Braves", "Milwaukee Brewers")],
                (0.50, 0.51, 0.52),
            )
        )
        self.write_day("2026-08-11", f"## Full-slate read\n\n{blocks}", [])
        stats = self.report()["populations"]["market_only"]
        self.assertEqual(stats["games_clearing_floor"], 3)
        self.assertEqual(stats["games_within_daily_cap"], 2)
        dropped = [s for s in stats["selections"] if not s["within_daily_cap"]]
        self.assertEqual(len(dropped), 1)
        self.assertFalse(dropped[0]["grade"]["graded"])

    def test_the_two_populations_are_reported_separately_and_never_summed(self):
        self.write_day(
            "2026-08-11",
            "## Full-slate read\n\n### Seattle Mariners at New York Yankees — 6:05 PM CT\n"
            "**Price:** DK fair SEA 0.415 / NYY 0.585. PM asks SEA 0.350 / NYY 0.660. "
            "win probability SEA 0.500 with a 0.020 uncertainty haircut.\n",
            [(AWAY, HOME, "Final", 7, 2)],
        )
        populations = self.report()["populations"]
        self.assertEqual(set(POPULATIONS), set(populations) - {"why_empty"})
        self.assertEqual(populations["market_only"]["games_clearing_floor"], 1)
        self.assertEqual(populations["recorded_handicap"]["games_clearing_floor"], 1)
        self.assertAlmostEqual(
            populations["recorded_handicap"]["selections"][0]["conservative_edge"], 0.13
        )

    def test_a_day_with_no_artifact_in_any_root_is_named_not_omitted(self):
        report = self.report(until=replay.parse_date("2026-08-12"))
        empty = [d for d in report["days"] if d["empty_in_every_root"]]
        self.assertEqual([d["date"] for d in empty], ["2026-08-11", "2026-08-12"])
        self.assertEqual(report["coverage"]["days_empty_in_every_root"], 2)

    def test_a_day_with_no_cached_schedule_reports_a_null_denominator(self):
        (self.root / "slate" / "2026-08-11.md").write_text(
            "## Full-slate read\n\n### Seattle Mariners at New York Yankees\n"
            "**Price:** DK fair SEA 0.415 / NYY 0.585.\n", encoding="utf-8"
        )
        row = self.report()["coverage"]["per_day"][0]
        self.assertIsNone(row["scheduled_games"])
        self.assertIsNone(row["coverage_of_schedule"])
        self.assertEqual(self.report()["coverage"]["days_without_cached_schedule"], 1)

    def test_two_roots_holding_different_documents_are_both_carried(self):
        """2026-08-22 is exactly this: different sections, different coverage."""
        other = Path(self.tmp.name) / "skill" / ".picks"
        (other / "slate").mkdir(parents=True)
        self.write_day(
            "2026-08-11",
            "## Full-slate read\n\n### Seattle Mariners at New York Yankees\n"
            "**Price:** DK fair SEA 0.415 / NYY 0.585.\n",
            [(AWAY, HOME, "Final", 7, 2)],
        )
        (other / "slate" / "2026-08-11.md").write_text(
            "## Full-slate read\n\n### Boston Red Sox at Miami Marlins\n"
            "**Price:** DK fair BOS 0.585 / MIA 0.415.\n", encoding="utf-8"
        )
        report = self.report(extra_picks_dirs=[other])
        day = report["days"][0]
        self.assertEqual(day["document_count"], 2)
        self.assertEqual(sorted(day["distinct_matchups"]), [
            "Boston Red Sox at Miami Marlins", "Seattle Mariners at New York Yankees"
        ])
        self.assertTrue(all(d["conflicting_copies"] for d in day["documents"]))

    def test_two_roots_holding_the_same_document_are_marked_duplicate(self):
        other = Path(self.tmp.name) / "skill" / ".picks"
        (other / "slate").mkdir(parents=True)
        text = ("## Full-slate read\n\n### Seattle Mariners at New York Yankees\n"
                "**Price:** DK fair SEA 0.415 / NYY 0.585.\n")
        self.write_day("2026-08-11", text, [])
        (other / "slate" / "2026-08-11.md").write_text(text, encoding="utf-8")
        day = self.report(extra_picks_dirs=[other])["days"][0]
        self.assertTrue(all(d["duplicate_of_other_root"] for d in day["documents"]))
        self.assertFalse(any(d["conflicting_copies"] for d in day["documents"]))

    def test_an_unreadable_field_keeps_its_reason_and_its_raw_prose(self):
        self.write_day(
            "2026-08-11",
            "## Full-slate read\n\n### Seattle Mariners at New York Yankees\n"
            "**Price:** DraftKings unavailable because the game was already in progress.\n",
            [],
        )
        game = self.report()["days"][0]["documents"][0]["games"][0]
        self.assertEqual(game["inputs"]["dk_fair_prob"]["provenance"], "unavailable")
        self.assertTrue(game["inputs"]["dk_fair_prob"]["reason"])
        self.assertIn("DraftKings unavailable", game["raw"])

    def test_the_empty_handicap_population_says_which_step_emptied_it(self):
        self.write_day(
            "2026-08-11",
            "## Full-slate read\n\n### Seattle Mariners at New York Yankees\n"
            "**Price:** DK fair SEA 0.415 / NYY 0.585. PM asks SEA 0.420 / NYY 0.585. "
            "win probability SEA 0.500.\n",
            [],
        )
        gap = self.report()["populations"]["recorded_handicap"]["why_empty"]
        self.assertEqual(gap["blocks_with_a_recorded_handicap"], 1)
        self.assertEqual(gap["blocks_with_a_recorded_haircut"], 0)
        self.assertEqual(gap["blocks_with_both"], 0)

    def test_an_inferred_order_price_is_counted_separately_from_a_labelled_one(self):
        """A single evaluable-games headline hides how much rests on convention."""
        self.write_day(
            "2026-08-11",
            "## Full-slate read\n\n"
            "### Seattle Mariners at New York Yankees\n"
            "**Price:** DK fair SEA 0.415 / NYY 0.585. PM asks SEA 0.420 / NYY 0.585.\n\n"
            "### Boston Red Sox at Miami Marlins\n"
            "**Price:** DK fair 0.585 / 0.415. Polymarket ask 0.590 / 0.420.\n",
            [],
        )
        stats = self.report()["populations"]["market_only"]
        self.assertEqual(stats["games_evaluable"], 2)
        self.assertEqual(stats["games_evaluable_faithful"], 1)
        self.assertEqual(stats["games_evaluable_inferred_order"], 1)

    def test_two_documents_pricing_one_game_differently_are_reported(self):
        """The 2026-08-22 finding: same slate, two captures, up to nine points apart.

        Every hypothetical selection in the real window comes from one of the two
        documents for that date. A report that preferred a root would have said
        either "five games cleared" or "none did" with no way to tell which.
        """
        other = Path(self.tmp.name) / "skill" / ".picks"
        (other / "slate").mkdir(parents=True)
        self.write_day(
            "2026-08-11",
            "## Full-slate read\n\n### Seattle Mariners at New York Yankees\n"
            "**Price:** DK fair SEA 0.415 / NYY 0.585. PM asks SEA 0.330 / NYY 0.675.\n",
            [],
        )
        (other / "slate" / "2026-08-11.md").write_text(
            "## Full-slate read\n\n### Seattle Mariners at New York Yankees\n"
            "**Price:** DK fair SEA 0.415 / NYY 0.585. PM asks SEA 0.425 / NYY 0.580.\n",
            encoding="utf-8",
        )
        cross = self.report(extra_picks_dirs=[other])["cross_document_disagreement"]
        self.assertEqual(cross["games_priced_by_more_than_one_document"], 1)
        self.assertEqual(cross["games_where_the_documents_disagree"], 1)
        self.assertAlmostEqual(cross["max_spread"], 0.095)
        self.assertEqual(len(cross["disagreements"][0]["sources"]), 2)

    def test_two_documents_agreeing_within_rounding_are_not_reported(self):
        """Morning and evening captures drift by half a point all window; that is
        the book moving, not two readings of one moment disagreeing."""
        other = Path(self.tmp.name) / "skill" / ".picks"
        (other / "slate").mkdir(parents=True)
        self.write_day(
            "2026-08-11",
            "## Full-slate read\n\n### Seattle Mariners at New York Yankees\n"
            "**Price:** DK fair SEA 0.415 / NYY 0.585. PM asks SEA 0.420 / NYY 0.585.\n",
            [],
        )
        (other / "slate" / "2026-08-11.md").write_text(
            "## Full-slate read\n\n### Seattle Mariners at New York Yankees\n"
            "**Price:** DK fair SEA 0.415 / NYY 0.585. PM asks SEA 0.423 / NYY 0.582.\n",
            encoding="utf-8",
        )
        cross = self.report(extra_picks_dirs=[other])["cross_document_disagreement"]
        self.assertEqual(cross["games_priced_by_more_than_one_document"], 1)
        self.assertEqual(cross["games_where_the_documents_disagree"], 0)

    def test_the_report_records_the_rails_it_ran_against(self):
        self.write_day("2026-08-11", "## Clean read\n", [])
        report = self.report(edge_floor=0.03, max_bets_per_day=1)
        self.assertEqual(report["edge_floor"], 0.03)
        self.assertEqual(report["max_bets_per_day"], 1)
        self.assertEqual(report["repo_revision"], "abc123")

    def test_the_live_risk_limits_file_is_never_read(self):
        """A report whose meaning changes when a rail is edited is not reproducible."""
        self.write_day("2026-08-11", "## Clean read\n", [])
        policy_module = sys.modules["mlb_runtime_policy"]
        with unittest.mock.patch.object(
            policy_module, "load_mlb_selection_policy"
        ) as patched:
            self.report()
        self.assertFalse(patched.called)

    def test_the_report_renders_without_raising(self):
        self.write_day(
            "2026-08-11",
            "## Full-slate read\n\n### Seattle Mariners at New York Yankees\n"
            "**Price:** DK fair SEA 0.415 / NYY 0.585. PM asks SEA 0.350 / NYY 0.660.\n",
            [(AWAY, HOME, "Final", 7, 2)],
        )
        text = replay.render(self.report())
        self.assertIn("MLB gate replay", text)
        self.assertIn("Populations (never summed)", text)
        self.assertIn("traded price", text)

    def test_a_bad_window_or_rail_is_a_caller_error(self):
        # Read off the module, not this file's top-level import: a consultation
        # pin elsewhere reloads the module and rebuilds this class.
        error = replay.ReplayError
        with self.assertRaises(error):
            self.report(until=replay.parse_date("2026-08-10"))
        with self.assertRaises(error):
            self.report(edge_floor=0.0)
        with self.assertRaises(error):
            self.report(max_bets_per_day=0)
        with self.assertRaises(error):
            self.report(picks_dir=self.root / "nope")

    def test_a_corrupt_schedule_cache_is_reported_not_raised(self):
        self.write_day("2026-08-11", "## Clean read\n", [])
        (self.root / "audit-results" / "2026-08-11.json").write_text("{", encoding="utf-8")
        self.assertTrue(self.report()["days"][0]["finals"].startswith("corrupt"))


class ReadOnlyTests(unittest.TestCase):
    def test_the_module_never_fetches(self):
        """The replay is entirely offline; the only score source is the cache."""
        source = Path(replay.__file__).read_text(encoding="utf-8")
        for forbidden in ("fetch_json", "urlopen", "requests.", "SCHEDULE_URL"):
            self.assertNotIn(forbidden, source)

    def test_the_module_never_writes(self):
        source = Path(replay.__file__).read_text(encoding="utf-8")
        for forbidden in ("write_text", "write_bytes", "mkdir", "os.remove", "shutil"):
            self.assertNotIn(forbidden, source)

    def test_the_epsilon_matches_the_gate_s_own_slack(self):
        self.assertEqual(EDGE_COMPARISON_EPSILON, 1e-9)


if __name__ == "__main__":
    unittest.main()
