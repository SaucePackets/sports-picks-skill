import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import mlb_game_reads
from scripts import vig_refusal_hypothesis_scan as scan


SECTION = """### Atlanta Braves at Milwaukee Brewers — pass
**Starter:** Martin Perez 3.15 vs Logan Henderson 2.70.
**Bullpen:** ATL 4.22 / 1.31, the side-specific liability; MIL 0.69 / 1.00.
**Price:** DK ATL +147 / MIL -158 -> de-vigged fair ATL 39.8% / MIL 60.2%; Polymarket ask ATL 46.0% / MIL 54.5%. **Pass:** the ask is already below the DK fair prior.
"""


def slate(*sections, header="# MLB Slate\n\n"):
    return header + "\n".join(sections)


class SelectionTests(unittest.TestCase):
    def enumerate_in(self, files):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, text in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            return scan.eligible_files(root, dt.date(2026, 8, 11), dt.date(2026, 8, 31))

    def test_a_narrated_day_is_excluded_with_its_reason(self):
        result = self.enumerate_in({"2026-08-18.md": "# MLB Slate\n\nNothing cleared today.\n"})
        entry = result["files"][0]
        self.assertFalse(entry["eligible"])
        self.assertIn("narrated, not read out per game", entry["reason"])
        self.assertEqual(entry["price_lines"], 0)

    def test_a_nested_lane_file_is_listed_and_excluded_not_invisible(self):
        # The mirror of the drought report's own blocker: recursing makes an
        # NFL writeup visible, and treating it as an MLB read would silently
        # un-drought a drought day. It is listed, and excluded, and says why.
        result = self.enumerate_in({"nfl/2026-08-26.md": slate(SECTION)})
        entry = result["files"][0]
        self.assertTrue(entry["nested"])
        self.assertFalse(entry["eligible"])
        self.assertIn("not the MLB slate lane", entry["reason"])

    def test_a_day_outside_the_window_is_not_counted_at_all(self):
        result = self.enumerate_in({"2026-07-04.md": slate(SECTION)})
        self.assertEqual(result["files"], [])

    def test_the_scope_of_the_walk_is_stated(self):
        result = self.enumerate_in({"2026-08-22.md": slate(SECTION)})
        self.assertIn("walked recursively", result["scope"])
        self.assertIn("top level", result["scope"])


class ClauseScopeTests(unittest.TestCase):
    def test_a_statistics_line_does_not_name_a_rail(self):
        # The measured defect: classifying over the whole section made the
        # bullpen rail fire on 109 of 109 reads, because every section carries
        # a **Bullpen:** statistics line. A rail at 100% is measuring the
        # writeup's template, not the gate.
        section = """### Atlanta Braves at Milwaukee Brewers — pass
**Bullpen:** ATL 4.22 / 1.31; MIL 0.69 / 1.00.
**Price:** DK ATL +147 / MIL -158 -> de-vigged fair ATL 39.8% / MIL 60.2%; Polymarket ask ATL 46.0% / MIL 54.5%. **Pass:** the ask is already below the DK fair prior.
"""
        record = scan.parse_slate(slate(section))[0]
        self.assertEqual(record["refusing_rails"], ["price_discipline"])
        self.assertNotIn("bullpen_close_game_survival", record["refusing_rails"])

    def test_the_bullpen_rail_still_fires_when_the_verdict_names_it(self):
        section = SECTION.replace(
            "**Pass:** the ask is already below the DK fair prior.",
            "**Pass:** Milwaukee's bullpen is the decisive liability.",
        )
        record = scan.parse_slate(slate(section))[0]
        self.assertIn("bullpen_close_game_survival", record["refusing_rails"])

    def test_the_clause_source_travels_with_the_answer(self):
        record = scan.parse_slate(slate(SECTION))[0]
        self.assertEqual(record["refusal_clause_source"], "verdict_field+price_line")


class ExtractionTests(unittest.TestCase):
    def test_a_labelled_percentage_pair_is_read_onto_the_right_sides(self):
        record = scan.parse_slate(slate(SECTION))[0]
        self.assertAlmostEqual(record["dk_fair_prob"]["away"], 0.398)
        self.assertAlmostEqual(record["dk_fair_prob"]["home"], 0.602)
        self.assertAlmostEqual(record["polymarket_ask"]["away"], 0.460)
        self.assertAlmostEqual(record["polymarket_ask"]["home"], 0.545)
        self.assertEqual(record["dk_fair_prob_pattern"], "fair_labelled_pct")

    def test_a_positional_pair_is_away_first(self):
        section = """### Washington Nationals at Miami Marlins — pass
**Price:** DK WSH +168 / MIA -181 -> fair 36.7% / 63.3%; Polymarket asks 42.5% / 58.0%. **Pass:** no edge.
"""
        record = scan.parse_slate(slate(section))[0]
        self.assertAlmostEqual(record["dk_fair_prob"]["away"], 0.367)
        self.assertAlmostEqual(record["dk_fair_prob"]["home"], 0.633)
        self.assertEqual(record["dk_fair_prob_pattern"], "fair_positional_pct")

    def test_a_single_sided_fair_is_completed_by_its_complement_and_says_so(self):
        # A de-vigged pair sums to 1 by construction, so one side determines
        # the other. The pattern name records that the second number was
        # derived rather than stated.
        section = """### Arizona Diamondbacks at Atlanta Braves — pass
**Price:** DK 115/-124 -> de-vig fair away 0.457; PM ask 0.465/0.540. **Pass:** no edge.
"""
        record = scan.parse_slate(slate(section))[0]
        self.assertAlmostEqual(record["dk_fair_prob"]["away"], 0.457)
        self.assertAlmostEqual(record["dk_fair_prob"]["home"], 0.543)
        self.assertEqual(record["dk_fair_prob_pattern"], "fair_single_side_dec")

    def test_an_unresolvable_label_refuses_rather_than_guesses(self):
        # Getting a side backwards inverts every edge downstream, so a label
        # that matches neither team is an extraction failure, not a coin flip.
        section = """### Atlanta Braves at Milwaukee Brewers — pass
**Price:** de-vigged fair XYZ 39.8% / QRS 60.2%; Polymarket ask ATL 46.0% / MIL 54.5%. **Pass:** no edge.
"""
        record = scan.parse_slate(slate(section))[0]
        self.assertNotIn("dk_fair_prob", record)
        self.assertTrue(
            any("sides could not be assigned" in note for note in record["unreadable"]),
            record["unreadable"],
        )

    def test_an_unreadable_number_is_counted_never_imputed(self):
        section = """### Atlanta Braves at Milwaukee Brewers — pass
**Price:** the board was unquotable this morning. **Pass:** no edge.
"""
        record = scan.parse_slate(slate(section))[0]
        self.assertNotIn("dk_fair_prob", record)
        self.assertNotIn("polymarket_ask", record)
        self.assertEqual(
            sum(1 for note in record["unreadable"] if note.startswith(("dk_fair_prob", "polymarket_ask"))),
            2,
        )

    def test_a_section_with_no_price_line_is_recorded_as_such(self):
        section = "### Atlanta Braves at Milwaukee Brewers — pass\n**Form:** ATL 2-5.\n"
        record = scan.parse_slate(slate(section))[0]
        self.assertIn("price: section carries no **Price:** line", record["unreadable"])


class ValueSideTests(unittest.TestCase):
    def test_the_value_side_is_the_larger_fair_minus_ask(self):
        record = scan.parse_slate(slate(SECTION))[0]
        side = scan.value_side(record)
        self.assertEqual(side["side"], "home")
        self.assertAlmostEqual(side["edge"], 0.602 - 0.545)

    def test_a_tie_has_no_value_side(self):
        record = {
            "dk_fair_prob": {"away": 0.5, "home": 0.5},
            "polymarket_ask": {"away": 0.52, "home": 0.52},
        }
        self.assertIsNone(scan.value_side(record))

    def test_a_missing_price_has_no_value_side(self):
        self.assertIsNone(scan.value_side({"dk_fair_prob": {"away": 0.5, "home": 0.5}}))


class OutcomeTests(unittest.TestCase):
    FINALS = [
        {
            "game_pk": 1,
            "away": "Atlanta Braves",
            "home": "Milwaukee Brewers",
            "away_score": 2,
            "home_score": 5,
            "winner": "home",
        }
    ]

    def test_an_outcome_is_attached_with_a_readable_score(self):
        record = {"away": "Atlanta Braves", "home": "Milwaukee Brewers"}
        scan.attach_outcome(record, self.FINALS)
        self.assertTrue(record["outcome_known"])
        self.assertEqual(record["outcome_winner"], "home")
        # Each number carries the team it belongs to; a bare "2-5" was read
        # backwards off exactly this kind of field in the drought lane.
        self.assertEqual(record["outcome_score"], "Atlanta Braves 2 at Milwaukee Brewers 5")

    def test_two_matching_games_refuse_rather_than_pick_one(self):
        finals = self.FINALS + [dict(self.FINALS[0], game_pk=2, winner="away")]
        record = {"away": "Atlanta Braves", "home": "Milwaukee Brewers"}
        scan.attach_outcome(record, finals)
        self.assertFalse(record["outcome_known"])
        self.assertIn("doubleheader", record["outcome_reason"])

    def test_no_matching_game_says_so(self):
        record = {"away": "Chicago Cubs", "home": "Seattle Mariners"}
        scan.attach_outcome(record, self.FINALS)
        self.assertFalse(record["outcome_known"])
        self.assertIn("0 final games match", record["outcome_reason"])

    def test_a_tied_or_unfinished_game_is_not_an_outcome(self):
        payload = {
            "dates": [
                {
                    "games": [
                        {
                            "gamePk": 1,
                            "status": {"detailedState": "In Progress"},
                            "teams": {
                                "away": {"team": {"name": "A"}, "score": 1},
                                "home": {"team": {"name": "B"}, "score": 0},
                            },
                        },
                        {
                            "gamePk": 2,
                            "status": {"detailedState": "Final"},
                            "teams": {
                                "away": {"team": {"name": "C"}, "score": 3},
                                "home": {"team": {"name": "D"}, "score": 3},
                            },
                        },
                    ]
                }
            ]
        }
        self.assertEqual(scan.outcomes_for(payload), [])


class VocabularyTests(unittest.TestCase):
    def test_every_rail_the_classifier_emits_is_in_the_shared_vocabulary(self):
        # Imported, not restated. If the recorder and the passenger spoke
        # different languages the passenger could not inform the recorder,
        # which is its only job.
        emitted = {rail for _, rail in scan.RAIL_PHRASES}
        self.assertTrue(emitted <= set(mlb_game_reads.REFUSAL_RAILS), emitted - set(mlb_game_reads.REFUSAL_RAILS))

    def test_the_vocabulary_is_consulted_not_copied(self):
        original = scan.REFUSAL_RAILS
        try:
            scan.REFUSAL_RAILS = frozenset()
            with self.assertRaises(ValueError):
                scan.classify_rails("the ask is efficient")
        finally:
            scan.REFUSAL_RAILS = original

    def test_incomplete_input_data_exists_because_the_prose_needed_it(self):
        rails, recognised = scan.classify_rails("missing offense input for Arizona")
        self.assertTrue(recognised)
        self.assertIn("incomplete_input_data", rails)
        self.assertIn("incomplete_input_data", mlb_game_reads.REFUSAL_RAILS)


class ReportTests(unittest.TestCase):
    def build(self, files, fetch=False, finals=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, text in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            with mock.patch.object(scan, "refetch_schedule", return_value={}), \
                    mock.patch.object(scan, "outcomes_for", return_value=finals or []):
                return scan.scan(root, dt.date(2026, 8, 11), dt.date(2026, 8, 31), fetch_outcomes=fetch)

    def test_the_base_rate_is_rendered_next_to_the_rail_table(self):
        # A rail bucket is only interesting relative to how often the value
        # side won at all. Shipping the numerator without the denominator is
        # the base-rate fallacy this lane already committed once.
        report = self.build(
            {"2026-08-22.md": slate(SECTION)},
            fetch=True,
            finals=OutcomeTests.FINALS,
        )
        self.assertEqual(report["overall"]["reads_with_value_side_and_outcome"], 1)
        self.assertEqual(report["overall"]["value_side_won"], 1)
        text = scan.render(report)
        self.assertIn("Base rate over the same population", text)
        self.assertIn("**1 of 1**", text)

    def test_the_rail_counts_are_declared_not_to_be_a_partition(self):
        report = self.build({"2026-08-22.md": slate(SECTION)})
        self.assertIn("not a partition", scan.render(report))

    def test_an_unclassified_refusal_is_quoted_verbatim(self):
        section = """### Atlanta Braves at Milwaukee Brewers — pass
**Price:** DK ATL +147 / MIL -158 -> de-vigged fair ATL 39.8% / MIL 60.2%; Polymarket ask ATL 46.0% / MIL 54.5%. **Pass:** it simply did not feel right.
"""
        report = self.build({"2026-08-22.md": slate(section)})
        self.assertEqual(report["counts"]["reads_with_no_classified_rail"], 1)
        self.assertIn("it simply did not feel right", scan.render(report))

    def test_the_selection_bias_carries_its_denominator(self):
        report = self.build(
            {
                "2026-08-22.md": slate(SECTION),
                "2026-08-18.md": "# MLB Slate\n\nNothing cleared.\n",
            }
        )
        self.assertIn("1 of 2 slate files", report["selection_bias"])
        self.assertEqual(report["counts"]["files_in_window"], 2)
        self.assertEqual(report["counts"]["files_eligible"], 1)

    def test_the_corpus_is_named_or_the_report_says_it_is_not(self):
        report = self.build({"2026-08-22.md": slate(SECTION)})
        self.assertIn("not stated on the command line", report["corpus"])

    def test_no_absolute_home_path_reaches_the_artifact(self):
        # The first version of this test built the report in a system temp
        # directory, which is not under the home directory — so the assertion
        # held whether or not the substitution happened, and baking the raw
        # path back in SURVIVED the mutation sweep. A fixture that cannot
        # exhibit the defect is not coverage.
        #
        # The corpus now sits under a directory the scan is told is home, so
        # the substitution is exercised rather than assumed.
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            root = fake_home / "corpus" / "slate"
            root.mkdir(parents=True)
            (root / "2026-08-22.md").write_text(slate(SECTION), encoding="utf-8")
            with mock.patch.object(scan.Path, "home", staticmethod(lambda: fake_home)):
                report = scan.scan(root, dt.date(2026, 8, 11), dt.date(2026, 8, 31))
                scope = report["enumeration"]["scope"]
                text = scan.render(report)
        self.assertTrue(scope.startswith("~/corpus/slate"), scope)
        self.assertNotIn(str(fake_home), scope)
        self.assertNotIn(str(fake_home), text)

    def test_home_relative_leaves_a_path_outside_home_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            outside = Path(tmp) / "elsewhere" / "slate"
            with mock.patch.object(scan.Path, "home", staticmethod(lambda: fake_home)):
                self.assertEqual(scan.home_relative(outside), str(outside))
                self.assertEqual(scan.home_relative(fake_home / "x"), "~/x")


if __name__ == "__main__":
    unittest.main()
