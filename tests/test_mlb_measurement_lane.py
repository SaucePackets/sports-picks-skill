import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

from scripts import mlb_game_reads
from scripts import mlb_measurement_lane as lane
from scripts import mlb_model_eval_dataset as dataset
from scripts import mlb_probability_model

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


def game(game_pk=823509, away="Atlanta Braves", home="Milwaukee Brewers"):
    return {"game_pk": game_pk, "event_id": f"4018{game_pk}", "away": away, "home": home}


def schedule(games=None, reads=None, **overrides):
    games = [game()] if games is None else games
    payload = {
        "date": "2026-09-01",
        "sport": "mlb",
        "market_type": "moneyline",
        "candidates": [],
        "lineup_watchlist": [],
        "slate_denominator": {
            "source": "mlb_stage2_scan",
            "fetched_at_utc": "2026-09-01T15:30:00+00:00",
            "games": games,
        },
        "game_reads": [read()] if reads is None else reads,
    }
    payload.update(overrides)
    return payload


def statsapi(game_pk=823509, away="Atlanta Braves", home="Milwaukee Brewers",
             away_score=5, home_score=2, status="Final"):
    return {
        "dates": [
            {
                "games": [
                    {
                        "gamePk": game_pk,
                        "status": {"detailedState": status},
                        "teams": {
                            "away": {"team": {"name": away}, "score": away_score},
                            "home": {"team": {"name": home}, "score": home_score},
                        },
                    }
                ]
            }
        ]
    }


def rows_for(sched, finals_payload=None):
    finals = (
        {"2026-09-01": dataset._final_by_game_pk(finals_payload)}
        if finals_payload is not None
        else {}
    )
    return lane.build_rows([("2026-09-01", sched)], finals)[0]


def audit_for(sched):
    return lane.build_rows([("2026-09-01", sched)], {})[1]


DEDUP_STUB = {
    "policy": "",
    "dates_requested": 1,
    "dates_used": 1,
    "excluded": [],
    "dates_with_no_schedule": [],
    "duplicate_copies_collapsed": 0,
}

EMPTY_AUDIT = {
    "policy": "",
    "accounting": "",
    "dates_used": 1,
    "dates_with_no_usable_denominator": [],
    "orphaned_reads": [],
    "scheduled_games": 0,
    "unusable_denominator_entries": [],
    "per_date": [],
    "rows": 0,
}


class DenominatorTests(unittest.TestCase):
    def test_a_scheduled_game_with_no_read_still_produces_a_row(self):
        # The whole reason this module exists next to the dataset builder. The
        # builder SKIPS an unusable read, which is right for an evaluator and
        # useless for a measurement: a game nobody recorded is exactly the kind
        # of failure the lane is looking for, and it cannot be counted if it
        # vanishes.
        rows = rows_for(schedule(games=[game(1), game(2)], reads=[read(1)]))
        self.assertEqual([row["game_pk"] for row in rows], [1, 2])
        missing = rows[1]
        self.assertEqual(missing["fidelity"], "no_read")
        self.assertEqual(missing["refusal_attribution"]["label"], "process_missing_input")
        self.assertEqual(
            missing["dk_fair_prob"],
            {
                "value": None,
                "provenance": "unavailable",
                "reason": "the slate recorded no read for this scheduled game",
            },
        )

    def test_a_read_for_a_game_not_in_the_denominator_contributes_no_row(self):
        # The denominator is the roster the recorder cross-checks against a
        # fresh scan. A read outside it would let a run add rows to its own
        # population, which is the failure `--denominator` exists to stop one
        # level down.
        rows = rows_for(schedule(games=[game(1)], reads=[read(1), read(2)]))
        self.assertEqual([row["game_pk"] for row in rows], [1])

    def test_an_orphaned_read_is_dropped_and_named_not_dropped_silently(self):
        # Dropping it is right. Dropping it silently is not: the recorder's own
        # validator calls a read outside the denominator an error, so one
        # reaching this lane is evidence the slate was written unvalidated.
        audit = audit_for(schedule(games=[game(1)], reads=[read(1), read(2)]))
        self.assertEqual(audit["orphaned_reads"], [{"date": "2026-09-01", "game_pk": 2}])

    def test_a_schedule_with_no_denominator_contributes_no_rows(self):
        sched = schedule()
        del sched["slate_denominator"]
        self.assertEqual(rows_for(sched), [])

    def test_a_date_read_but_never_opened_is_named_with_which_way_it_failed(self):
        # `denominator_games` returns [] for a missing OR malformed denominator
        # with no raise and no log, and on every schedule file that exists today
        # that is the path taken. Counted as a used date with zero rows, the
        # page reads "we opened nine slates and found nothing" when the truth is
        # "we never opened them".
        cases = {
            "the schedule carries no slate_denominator object": lambda s: s.pop(
                "slate_denominator"
            ),
            "the slate_denominator carries no games list": lambda s: s[
                "slate_denominator"
            ].pop("games"),
            "the slate_denominator lists zero games": lambda s: s["slate_denominator"].update(
                {"games": []}
            ),
        }
        for reason, break_it in cases.items():
            with self.subTest(reason=reason):
                sched = schedule()
                break_it(sched)
                audit = audit_for(sched)
                self.assertEqual(audit["dates_used"], 1)
                self.assertEqual(audit["rows"], 0)
                self.assertEqual(
                    audit["dates_with_no_usable_denominator"],
                    [{"date": "2026-09-01", "reason": reason}],
                )

    def test_a_used_date_can_never_contribute_zero_rows_in_silence(self):
        # The invariant behind the case above, stated once so a fourth way of
        # having no roster cannot slip through the three named cases.
        for sched in (schedule(), schedule(games=[], reads=[])):
            with self.subTest(sched=sched["slate_denominator"].get("games")):
                rows, audit = lane.build_rows([("2026-09-01", sched)], {})
                named = {
                    item["date"] for item in audit["dates_with_no_usable_denominator"]
                }
                dates_with_rows = {row["date"] for row in rows}
                self.assertEqual(
                    audit["dates_used"], len(dates_with_rows | named), audit
                )

    def test_every_scheduled_game_either_builds_a_row_or_is_named(self):
        """The lane's headline invariant, checked against the denominator's own length.

        Round-2 blocker: the three named no-roster reasons are a
        classification, not a proof. A denominator whose ``games`` list is
        non-empty but holds non-dict entries is truthy, so it was never
        classified, and each such entry was dropped with a bare ``continue`` —
        a used date with two scheduled games and two rows missing, named
        nowhere, and the partial case (2 of 3 built) survived every counter in
        the report while falsifying the one claim this lane is built on.

        So this asserts over the shape space rather than on hand-picked
        fixtures: for every date, ``rows + named_shortfall == scheduled_games``.
        That closes a fourth reason, a fifth, and any filter added to the loop
        later — none of them can both skip a game and stay silent.
        """
        junk = ["not-a-dict", 7, None, ["nested"]]
        shapes = {
            "two good": [game(1), game(2)],
            "two junk": [junk[0], junk[1]],
            "partial, junk last": [game(1), game(2), junk[2]],
            "partial, junk first": [junk[3], game(1)],
            "junk between": [game(1), junk[0], game(2)],
            "empty": [],
        }
        for name, games in shapes.items():
            with self.subTest(shape=name):
                sched = schedule(games=games, reads=[])
                sched["slate_denominator"]["games"] = games
                rows, audit = lane.build_rows([("2026-09-01", sched)], {})
                scheduled = len(lane.denominator_games(sched))
                entry = audit["per_date"][0]
                self.assertEqual(entry["date"], "2026-09-01")
                self.assertEqual(entry["scheduled_games"], scheduled)
                self.assertEqual(entry["rows"], len(rows))
                self.assertEqual(entry["rows"] + entry["named_shortfall"], scheduled)
                # Counted is not named. Every unit of the shortfall is an entry
                # a reader can point at, with the date and its position.
                named = [
                    item
                    for item in audit["unusable_denominator_entries"]
                    if item["date"] == "2026-09-01"
                ]
                self.assertEqual(len(named), entry["named_shortfall"])
                for item in named:
                    self.assertIn("reason", item)
                    self.assertIsInstance(item["index"], int)
                self.assertEqual(audit["scheduled_games"], scheduled)

    def test_a_partly_unusable_denominator_is_not_reported_as_a_clean_slate(self):
        # The reader-facing half: 2 rows out of 3 scheduled games must not
        # render as three games measured. The shortfall reaches the page.
        sched = schedule(games=[game(1), game(2)], reads=[])
        sched["slate_denominator"]["games"] = [game(1), game(2), "not-a-dict"]
        rows, audit = lane.build_rows([("2026-09-01", sched)], {})
        text = lane.markdown(lane.report(rows, dict(DEDUP_STUB), audit))
        self.assertIn("denominator entry 2", text)
        self.assertIn("Scheduled games", text)


class FidelityTests(unittest.TestCase):
    def test_a_full_trail_is_a_recorded_handicap(self):
        row = rows_for(schedule())[0]
        self.assertEqual(row["fidelity"], "recorded_handicap")
        self.assertEqual(row["source_quality"], "market_only_fallback")

    def test_a_non_market_model_version_is_its_own_source_quality(self):
        # The split has to survive into every aggregate: under the market-only
        # fallback our raw probability IS DK's fair, so a model-versus-DK number
        # computed across both would be partly DK measured against itself.
        row = rows_for(schedule(reads=[read(model_version="vig-mlb-elo-v2")]))[0]
        self.assertEqual(row["source_quality"], "non_market_model")

    def test_a_read_with_the_trail_excused_is_no_handicap_not_a_defect(self):
        entry = read()
        for field in lane.MODEL_TRAIL_FIELDS:
            del entry[field]
        entry["unavailable"] = {field: "never handicapped" for field in lane.MODEL_TRAIL_FIELDS}
        # The edge goes with the handicap. `net_edge` is
        # `conservative_probability - polymarket_ask`, so a read excusing the
        # conservative probability and keeping the edge is claiming a
        # subtraction it did not do — which the recorder now refuses. Before the
        # coherence rail this fixture could hold both, and it did.
        del entry["net_edge"]
        entry["unavailable"]["net_edge"] = "never handicapped; nothing to subtract"
        entry["refusing_rails"] = ["no_dk_price"]
        row = rows_for(schedule(reads=[entry]))[0]
        self.assertEqual(row["fidelity"], "no_handicap")
        self.assertEqual(row["source_quality"], "not_applicable")

    def test_a_read_the_recorder_would_refuse_is_reported_as_unusable_not_folded_in(self):
        # A read that never passed the slate-time validator has reached this
        # report by some path that skipped it. That is a finding in itself, so
        # it gets its own bucket rather than being counted as a handicap.
        row = rows_for(schedule(reads=[read(refusing_rails=["not_a_real_rail"])]))[0]
        self.assertEqual(row["fidelity"], "unusable_read")
        self.assertTrue(row["read_errors"])

    def test_every_fidelity_bucket_is_zero_filled(self):
        # A bucket that never occurred must print 0. Without it a reader cannot
        # tell a constant axis from an impossible one, and the tuple stops being
        # load-bearing.
        payload = lane.report(rows_for(schedule()), {"policy": "", "dates_requested": 1,
                                                     "dates_used": 1, "excluded": [],
                                                     "dates_with_no_schedule": [],
                                                     "duplicate_copies_collapsed": 0},
                              dict(EMPTY_AUDIT))
        self.assertEqual(
            set(payload["aggregates"]["coverage_by_fidelity"]), set(lane.FIDELITY_BUCKETS)
        )
        self.assertEqual(
            set(payload["aggregates"]["refusal_attribution"]), set(lane.REFUSAL_ATTRIBUTIONS)
        )
        self.assertEqual(
            set(payload["aggregates"]["outcome_attribution"]), set(lane.OUTCOME_ATTRIBUTIONS)
        )
        self.assertEqual(
            payload["aggregates"]["refusal_attribution"]["gate_candidate_from_inferred_input"], 0
        )


class AvailabilityTests(unittest.TestCase):
    def test_an_explained_absence_carries_the_runs_own_reason(self):
        entry = read()
        del entry["polymarket_ask"]
        entry["unavailable"] = {"polymarket_ask": "the exact slug returned no market data"}
        row = rows_for(schedule(reads=[entry]))[0]
        self.assertEqual(row["polymarket_ask"]["provenance"], "unavailable")
        self.assertEqual(
            row["polymarket_ask"]["reason"], "the exact slug returned no market data"
        )

    def test_an_unexplained_absence_is_not_the_same_state_as_an_explained_one(self):
        # "No price" and "a price nobody recorded" are different facts and only
        # one of them is a finding. Collapsing them here would undo the whole
        # reason the recorder demands a reason.
        entry = read()
        del entry["polymarket_ask"]
        row = rows_for(schedule(reads=[entry]))[0]
        self.assertEqual(row["polymarket_ask"]["provenance"], "unexplained_absence")

    def test_starter_and_lineup_availability_are_never_reported_as_confirmed(self):
        # Nothing in a read states that a starter was announced or a lineup
        # posted; what exists is the rail the run named when one was NOT. The
        # absence of a complaint is not a confirmation, and reporting one as the
        # other would manufacture exactly the input provenance this lane exists
        # to stop inventing.
        quiet = rows_for(schedule())[0]
        self.assertEqual(quiet["starter_availability"], "not_stated")
        self.assertEqual(quiet["lineup_availability"], "not_stated")
        self.assertNotIn(
            "confirmed",
            {quiet["starter_availability"], quiet["lineup_availability"]},
        )
        named = rows_for(
            schedule(reads=[read(refusing_rails=["starter_unannounced"])])
        )[0]
        self.assertEqual(named["starter_availability"], "pending")
        self.assertEqual(named["lineup_availability"], "not_stated")

    def test_capture_time_says_it_is_the_rosters_and_not_the_games(self):
        row = rows_for(schedule())[0]
        self.assertEqual(row["captured_at_utc"]["value"], "2026-09-01T15:30:00+00:00")
        self.assertIn("schedule-level", row["captured_at_utc"]["provenance"])

    def test_bbo_mid_and_traded_are_never_captured_on_every_row(self):
        # D1: order-book capture is a runtime change held out of this lane. The
        # fields stay on the row so the gap is visible rather than absent, and
        # they are never approximated from the ask.
        row = rows_for(schedule())[0]
        for field in lane.UNCAPTURED_PRICE_FIELDS:
            self.assertEqual(row[field]["provenance"], "never_captured")
            self.assertIsNone(row[field]["value"])


class RefusalAttributionTests(unittest.TestCase):
    def test_a_missing_input_outranks_a_handicapping_rail_named_beside_it(self):
        # Precedence is the finding, not an implementation detail: a gate cannot
        # be said to have refused a game it was never able to price. A read
        # naming both is a process failure that happens to have written down a
        # gate as well.
        entry = read(refusing_rails=["no_dk_price", "price_discipline"])
        attribution = rows_for(schedule(reads=[entry]))[0]["refusal_attribution"]
        self.assertEqual(attribution["label"], "process_missing_input")
        self.assertIn("rail no_dk_price", attribution["evidence"])

    def test_a_handicapping_rail_alone_is_a_gate_refusal(self):
        attribution = rows_for(schedule())[0]["refusal_attribution"]
        self.assertEqual(attribution["label"], "gate_handicapping_rail")

    def test_the_volume_cap_is_not_counted_as_a_handicapping_decision(self):
        entry = read(refusing_rails=["daily_volume_cap"])
        attribution = rows_for(schedule(reads=[entry]))[0]["refusal_attribution"]
        self.assertEqual(attribution["label"], "gate_volume_cap")

    def test_an_unavailable_field_is_a_process_failure_even_with_no_process_rail(self):
        entry = read(refusing_rails=["price_discipline"])
        del entry["net_edge"]
        entry["unavailable"] = {"net_edge": "the edge was never computed"}
        attribution = rows_for(schedule(reads=[entry]))[0]["refusal_attribution"]
        self.assertEqual(attribution["label"], "process_missing_input")
        self.assertIn("net_edge: the edge was never computed", attribution["evidence"])

    def test_a_game_we_took_is_not_attributed_a_refusal(self):
        entry = read(disposition="candidate", refusing_rails=[])
        sched = schedule(reads=[entry], candidates=[{"game_pk": 823509}])
        attribution = rows_for(sched)[0]["refusal_attribution"]
        self.assertEqual(attribution["label"], "not_refused")

    def test_process_fixes_are_ranked_by_how_many_games_they_cost(self):
        entry = read(1, refusing_rails=["no_dk_price"])
        other = read(2, refusing_rails=["no_dk_price"])
        third = read(3, refusing_rails=["no_polymarket_market"])
        rows = rows_for(
            schedule(games=[game(1), game(2), game(3)], reads=[entry, other, third])
        )
        fixes = lane.ranked_process_fixes(rows)
        self.assertEqual([fix["cause"] for fix in fixes],
                         ["rail no_dk_price", "rail no_polymarket_market"])
        self.assertEqual([fix["games"] for fix in fixes], [2, 1])
        self.assertTrue(fixes[0]["example"])


class OutcomeTests(unittest.TestCase):
    def test_a_result_with_a_handicap_is_left_unattributed_between_read_and_variance(self):
        # Rebecca's classes 1 and 2 are not separable from a scoreline. The lane
        # records which way the read leaned and what happened, and refuses to
        # sort them.
        row = rows_for(schedule(), statsapi())[0]
        self.assertEqual(row["result"]["outcome"], 1)
        attribution = row["outcome_attribution"]
        self.assertEqual(attribution["label"], "unattributed_no_game_script")
        self.assertIs(attribution["away_won"], True)
        self.assertIs(attribution["read_favoured_away"], False)
        self.assertNotIn("bad_read", attribution.values())

    def test_a_transposed_read_is_refused_rather_than_scored(self):
        # Probabilities descend from ESPN and the outcome from StatsAPI. A
        # transposed read otherwise produces a perfectly clean row scoring one
        # club's handicap against the other club's result, with no trace.
        payload = statsapi(away="Milwaukee Brewers", home="Atlanta Braves",
                           away_score=5, home_score=2)
        row = rows_for(schedule(), payload)[0]
        self.assertEqual(row["result"]["provenance"], "refused")
        self.assertIsNone(row["result"]["outcome"])
        self.assertEqual(row["outcome_attribution"]["label"], "refused_transposed_read")

    def test_swap_detection_is_the_dataset_builders_and_not_a_second_copy(self):
        # Two halves, and neither alone is the pin.
        #
        # The call-site half: rebind the name the lane holds and the answer must
        # follow. An inline re-derivation at the call site would leave this
        # green while the import sat there looking consulted.
        #
        # The identity half: that name must BE the builder's object, pinned
        # against sys.modules under the BARE key. The lane imports
        # `mlb_model_eval_dataset` and this suite imports
        # `scripts.mlb_model_eval_dataset`; those are two different module
        # objects, and pinning against the wrong one passes while proving
        # nothing.
        self.assertIs(
            lane._transposition, sys.modules["mlb_model_eval_dataset"]._transposition
        )
        original = lane._transposition
        try:
            lane._transposition = lambda entry, away, home: "probe refusal"
            row = rows_for(schedule(), statsapi())[0]
            self.assertEqual(row["result"]["reason"], "probe refusal")
            self.assertEqual(row["result"]["provenance"], "refused")
        finally:
            lane._transposition = original
        self.assertIsNone(rows_for(schedule(), statsapi())[0]["result"].get("reason"))

    def test_a_row_with_no_final_is_pending_not_absent(self):
        row = rows_for(schedule())[0]
        self.assertEqual(row["outcome_attribution"]["label"], "pending_no_final")

    def test_a_final_with_no_handicap_says_so_rather_than_being_scored(self):
        entry = read()
        for field in lane.MODEL_TRAIL_FIELDS:
            del entry[field]
        entry["unavailable"] = {field: "never handicapped" for field in lane.MODEL_TRAIL_FIELDS}
        entry["refusing_rails"] = ["no_dk_price"]
        row = rows_for(schedule(reads=[entry]), statsapi())[0]
        self.assertEqual(row["outcome_attribution"]["label"], "no_probability_recorded")


METRIC_KEYS = frozenset(
    {
        "brier",
        "log_loss",
        "record",
        "calibration",
        "model_brier",
        "dk_brier",
        "median_model_minus_dk",
        "model_below_dk",
        "mean_conservative_probability",
        "away_wins",
        "away_losses",
    }
)

# A bucket is a dict that names which population it is. Anything under one may
# carry a metric; anything above one may not.
BUCKET_KEYS = frozenset({"fidelity", "source_quality", "rows"})

METRIC_WORDS = re.compile(r"\b(brier|log[ _]loss|calibration|record|win rate)\b", re.I)


def blended_metric_paths(node, inside_bucket=False, path="payload"):
    """Every metric key in the payload that does not sit inside a named bucket."""
    found = []
    if isinstance(node, dict):
        is_bucket = BUCKET_KEYS <= set(node)
        for key, value in node.items():
            if key in METRIC_KEYS and not inside_bucket:
                found.append(f"{path}.{key}")
            found += blended_metric_paths(value, inside_bucket or is_bucket, f"{path}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found += blended_metric_paths(item, inside_bucket, f"{path}[{index}]")
    return found


def metric_lines_outside_a_bucket(text):
    """Every rendered line naming a metric that is not inside a bucket's own block.

    The latch closes at the end of the bullet block it opened, not merely at
    the next heading. Round 2's blocker: clearing only on ``#`` left it stuck
    on for the whole ``### Metrics, per bucket`` section after the first
    bucket, so "under a bucket header" degenerated to "anywhere after the first
    bucket" — and the tail of that section, immediately before the next
    heading, is exactly where a summary row goes.

    In-bucket means: inside the metrics section, after a bucket header, still
    in that header's bullet block, and itself a bullet. A blended headline is
    none of those wherever it is written.
    """
    offenders = []
    in_metrics_section = False
    under_bucket_header = False
    seen_bullet = False
    for line in text.splitlines():
        is_bullet = line.startswith("- ")
        if line.startswith("#"):
            in_metrics_section = line.strip() == "### Metrics, per bucket"
            under_bucket_header = False
            seen_bullet = False
        elif line.startswith("**") and "rows" in line:
            under_bucket_header = True
            seen_bullet = False
        elif not line.strip():
            # The blank between a bucket header and its bullets does not end
            # the block; the blank after the bullets does.
            if seen_bullet:
                under_bucket_header = False
                seen_bullet = False
        elif is_bullet:
            seen_bullet = True
        else:
            under_bucket_header = False
            seen_bullet = False
        in_bucket = in_metrics_section and under_bucket_header and is_bullet
        if METRIC_WORDS.search(line) and not in_bucket:
            offenders.append(line)
    return offenders


class BlendedHeadlineTests(unittest.TestCase):
    """The no-combined-headline property, pinned where a headline would land.

    Blocker 2 on this PR: the property was asserted on `aggregate()` alone,
    which is one level below the two things a reader actually sees. A blended
    Brier added to `report()` — mixing `unusable_read` rows into a headline,
    a strictly worse version of the replay's own blocker 1 — left the whole
    suite green.
    """

    def payload(self):
        return lane.report(
            rows_for(schedule(), statsapi()),
            {"policy": "", "dates_requested": 1, "dates_used": 1, "excluded": [],
             "dates_with_no_schedule": [], "duplicate_copies_collapsed": 0},
            dict(EMPTY_AUDIT),
        )

    def test_the_json_report_carries_no_metric_above_a_bucket(self):
        self.assertEqual(blended_metric_paths(self.payload()), [])

    def test_the_rendered_markdown_carries_no_metric_above_a_bucket(self):
        self.assertEqual(metric_lines_outside_a_bucket(lane.markdown(self.payload())), [])

    def test_both_guards_catch_the_headline_they_exist_to_catch(self):
        # A guard nobody has seen fail is not a guard. This is the exact
        # mutation the reviewer landed green: an unfiltered blended headline in
        # the report dict, and the same number rendered.
        payload = self.payload()
        payload["brier"] = 0.2374
        payload["record"] = "10-12"
        self.assertEqual(
            sorted(blended_metric_paths(payload)), ["payload.brier", "payload.record"]
        )
        self.assertTrue(
            metric_lines_outside_a_bucket(
                "# MLB measurement lane\n\nBrier 0.2374, record 10-12.\n"
                + lane.markdown(self.payload())
            )
        )

    def test_the_markdown_guard_catches_a_headline_in_every_position_it_could_be_written(self):
        # Round-2 blocker: the guard was proven only ABOVE the metrics section,
        # which is the one position its stuck latch could not hide. The tail of
        # the metrics section — after the last bucket's bullets, immediately
        # before the next heading — is where a totals row naturally goes, and
        # the guard walked straight past one there.
        rendered = lane.markdown(self.payload()).splitlines()
        headline = "Overall Brier 0.2374 across all buckets, record 10-12."
        anchor = rendered.index("### Ranked process fixes")
        first_bucket = next(
            i for i, line in enumerate(rendered) if line.startswith("**") and "rows" in line
        )
        # Both forms — plain and bulleted, because "starts with a dash" must
        # not be a way in — in every position outside a bucket's own block.
        positions = {
            "above the metrics section": 0,
            "inside the section, before the first bucket": first_bucket,
            "at the tail, after the last bucket's bullets": anchor,
        }
        for where, index in positions.items():
            for text in (headline, f"- {headline}"):
                with self.subTest(position=where, bulleted=text.startswith("- ")):
                    mutated = rendered[:index] + [text, ""] + rendered[index:]
                    self.assertIn(
                        text, metric_lines_outside_a_bucket("\n".join(mutated)), where
                    )
        # The one position a text scan cannot reach is inside a bucket's own
        # bullet block, where the line is indistinguishable from the bucket's
        # own metrics. That is not a gap in this guard: nothing writes there
        # except the per-bucket render, and the JSON walk covers the payload
        # those bullets are drawn from. Said out loud rather than left implied.

    def test_a_bucket_metric_block_is_reachable_and_still_names_its_n(self):
        # Without this the two guards above pass vacuously on a report that
        # stopped emitting metrics at all.
        buckets = self.payload()["aggregates"]["buckets"]
        scored = [b for b in buckets if b["metrics"] is not None]
        self.assertTrue(scored)
        for bucket in scored:
            self.assertIn("n", bucket["metrics"])
            self.assertIn("brier", bucket["metrics"])


class AggregateTests(unittest.TestCase):
    def test_no_combined_metric_key_exists_anywhere_above_a_bucket(self):
        # Blocker 1 on PR #75 was a headline drawn from one fidelity of input
        # without saying so. The fix there was to make the combined key
        # impossible to write rather than to remember not to write it.
        aggregates = lane.aggregate(rows_for(schedule(), statsapi()))
        for key in ("brier", "log_loss", "record", "calibration", "metrics"):
            self.assertNotIn(key, aggregates)
        for bucket in aggregates["buckets"]:
            self.assertIn("fidelity", bucket)
            self.assertIn("source_quality", bucket)

    def test_every_metric_block_carries_its_own_n(self):
        aggregates = lane.aggregate(rows_for(schedule(), statsapi()))
        scored = [b for b in aggregates["buckets"] if b["metrics"] is not None]
        self.assertTrue(scored)
        for bucket in scored:
            self.assertIn("n", bucket["metrics"])

    def test_the_market_comparison_reports_its_own_n_and_not_the_buckets(self):
        # The two populations differ whenever a read carries a handicap and no
        # DK line. A comparison quietly computed over a smaller set than the
        # record printed beside it is the shape that made the replay's headline
        # wrong.
        with_dk = read(1)
        without_dk = read(2)
        del without_dk["dk_fair_prob"]
        without_dk["unavailable"] = {"dk_fair_prob": "DK line unavailable"}
        payload = {"dates": [{"games": statsapi(1)["dates"][0]["games"]
                              + statsapi(2)["dates"][0]["games"]}]}
        rows = rows_for(
            schedule(games=[game(1), game(2)], reads=[with_dk, without_dk]), payload
        )
        bucket = next(
            b for b in lane.aggregate(rows)["buckets"] if b["fidelity"] == "recorded_handicap"
        )
        self.assertEqual(bucket["metrics"]["n"], 2)
        self.assertEqual(bucket["market_comparison"]["n"], 1)

    def test_a_bucket_with_no_scoreable_row_reports_none_rather_than_zero(self):
        # A Brier of 0 over no rows is a number a reader will read. None is the
        # honest answer and forces the report to say there is nothing here.
        bucket = lane.aggregate(rows_for(schedule()))["buckets"][0]
        self.assertIsNone(bucket["metrics"])
        self.assertIsNone(bucket["market_comparison"])

    def test_buckets_split_by_model_version_within_one_fidelity(self):
        rows = rows_for(
            schedule(
                games=[game(1), game(2)],
                reads=[read(1), read(2, model_version="vig-mlb-elo-v2")],
            ),
            {"dates": [{"games": statsapi(1)["dates"][0]["games"]
                        + statsapi(2)["dates"][0]["games"]}]},
        )
        versions = sorted(
            b["model_version"] for b in lane.aggregate(rows)["buckets"] if b["model_version"]
        )
        self.assertEqual(versions, ["vig-mlb-elo-v2", "vig-mlb-market-v1"])


class DedupTests(unittest.TestCase):
    def test_byte_identical_copies_across_roots_collapse_to_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root_a, root_b = Path(tmp) / "a", Path(tmp) / "b"
            root_a.mkdir()
            root_b.mkdir()
            raw = json.dumps(schedule())
            for root in (root_a, root_b):
                (root / "2026-09-01-schedule.json").write_text(raw, encoding="utf-8")
            schedules, dedup = lane.load_schedules([root_a, root_b], ["2026-09-01"])
            self.assertEqual(len(schedules), 1)
            self.assertEqual(dedup["duplicate_copies_collapsed"], 1)
            self.assertEqual(dedup["excluded"], [])

    def test_roots_that_disagree_exclude_the_date_and_name_it(self):
        # Refusal, not repair. Choosing between two disagreeing captures of the
        # same slate is exactly the decision the replay could not make on
        # 2026-08-22, where the two cards' asks differed by up to nine points
        # and preferring either root produced a different answer with equal
        # confidence.
        with tempfile.TemporaryDirectory() as tmp:
            root_a, root_b = Path(tmp) / "a", Path(tmp) / "b"
            root_a.mkdir()
            root_b.mkdir()
            (root_a / "2026-09-01-schedule.json").write_text(
                json.dumps(schedule()), encoding="utf-8"
            )
            (root_b / "2026-09-01-schedule.json").write_text(
                json.dumps(schedule(reads=[read(refusing_rails=["cold_fade_reset"])])),
                encoding="utf-8",
            )
            schedules, dedup = lane.load_schedules([root_a, root_b], ["2026-09-01"])
            self.assertEqual(schedules, [])
            self.assertEqual(len(dedup["excluded"]), 1)
            self.assertEqual(dedup["excluded"][0]["date"], "2026-09-01")
            self.assertEqual(len(dedup["excluded"][0]["digests"]), 2)

    def test_the_dedup_policy_is_stated_in_the_output_and_not_only_in_code(self):
        # Open finding #4 on the replay was two dedup policies in one report,
        # neither of them printed. One policy, and it is on the page.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "2026-09-01-schedule.json").write_text(
                json.dumps(schedule()), encoding="utf-8"
            )
            _, dedup = lane.load_schedules([root], ["2026-09-01"])
        payload = lane.report([], dedup, dict(EMPTY_AUDIT))
        self.assertIn("byte-identical", payload["dedup"]["policy"])
        self.assertIn(payload["dedup"]["policy"], lane.markdown(payload))


class SharedRuleTests(unittest.TestCase):
    def test_the_market_only_version_string_matches_the_model_modules_own(self):
        # Defined locally so a read-only reporting module does not drag the
        # execution-path model into its import closure, and pinned here because
        # two copies of one name agree only until one of them changes.
        self.assertEqual(
            lane.MARKET_ONLY_MODEL_VERSION, mlb_probability_model.MARKET_MODEL_VERSION
        )

    def test_the_probability_rule_is_the_recorders_own_object(self):
        # Identity against sys.modules under the BARE name — the lane imports
        # `mlb_game_reads` while the suite imports `scripts.mlb_game_reads`, and
        # those are two different module objects — plus a call-site rebind, so
        # a copy re-derived inline while the import sat untouched would red.
        self.assertIs(lane._is_probability, sys.modules["mlb_game_reads"]._is_probability)
        original = lane._is_probability
        try:
            lane._is_probability = lambda value: False
            self.assertEqual(rows_for(schedule())[0]["fidelity"], "no_handicap")
        finally:
            lane._is_probability = original
        self.assertEqual(rows_for(schedule())[0]["fidelity"], "recorded_handicap")

    def test_the_scored_side_is_the_dataset_builders_and_not_a_second_decision(self):
        # Same two halves. The rebind proves the row actually reads the bound
        # name rather than a literal "away" written out at each call site: the
        # away and home probabilities in the fixture differ, so flipping the
        # constant has to move the recorded value.
        self.assertIs(
            lane.EVALUATED_SIDE, sys.modules["mlb_model_eval_dataset"].EVALUATED_SIDE
        )
        self.assertEqual(rows_for(schedule())[0]["dk_fair_prob"]["value"], 0.398)
        original = lane.EVALUATED_SIDE
        try:
            lane.EVALUATED_SIDE = "home"
            self.assertEqual(rows_for(schedule())[0]["dk_fair_prob"]["value"], 0.602)
        finally:
            lane.EVALUATED_SIDE = original

    def test_the_rail_vocabulary_is_a_partition_of_the_recorders_own(self):
        # Every rail the recorder accepts must land in exactly one attribution
        # class. A rail in none of them would fall through to
        # `unclassified_rail` — which is a real bucket on purpose, but it must
        # be empty by construction rather than by luck.
        classified = lane.PROCESS_RAILS | lane.HANDICAPPING_RAILS | lane.VOLUME_RAILS
        self.assertEqual(classified, mlb_game_reads.REFUSAL_RAILS)
        self.assertFalse(lane.PROCESS_RAILS & lane.HANDICAPPING_RAILS)
        self.assertFalse(lane.PROCESS_RAILS & lane.VOLUME_RAILS)
        self.assertFalse(lane.HANDICAPPING_RAILS & lane.VOLUME_RAILS)

    def test_an_integer_too_large_for_a_float_is_refused_and_never_raises(self):
        # math.isfinite is not total on ints — it raises OverflowError on one
        # too large to convert — and json.loads parses an arbitrarily long
        # integer literal straight off a card. A predicate that can itself crash
        # is not a guard.
        #
        # Both guards are exercised DIRECTLY, and the reason is a survivor this
        # sweep produced. Going through `build_rows` proves nothing about
        # `_has_full_trail`: the recorder's validator refuses a huge haircut
        # first, so the row is already `unusable_read` and the trail check never
        # runs. A fixture that cannot exhibit the defect is not coverage,
        # however plausible the assertion on the far side looks.
        entry = read(uncertainty_haircut=10 ** 400)
        self.assertFalse(lane._has_full_trail(entry))
        self.assertEqual(
            lane._scalar_availability(
                entry, "uncertainty_haircut", lane.is_finite_number(10 ** 400)
            )["provenance"],
            "unexplained_absence",
        )
        row = rows_for(schedule(reads=[entry]))[0]
        self.assertEqual(row["fidelity"], "unusable_read")
        self.assertEqual(row["uncertainty_haircut"]["provenance"], "unexplained_absence")


class ReadOnlyTests(unittest.TestCase):
    def test_the_module_names_no_way_to_reach_the_network(self):
        source = (REPO_ROOT / "scripts" / "mlb_measurement_lane.py").read_text(encoding="utf-8")
        for token in ("fetch_json", "urlopen", "requests.", "http_util"):
            self.assertNotIn(token, source)

    def test_missing_finals_cache_yields_no_finals_rather_than_a_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(lane.load_finals(Path(tmp), ["2026-09-01"]), {})
        self.assertEqual(lane.load_finals(None, ["2026-09-01"]), {})


class CliTests(unittest.TestCase):
    def test_the_cli_writes_the_report_and_the_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "2026-09-01-schedule.json").write_text(
                json.dumps(schedule()), encoding="utf-8"
            )
            finals_dir = root / "finals"
            finals_dir.mkdir()
            (finals_dir / "2026-09-01.json").write_text(json.dumps(statsapi()), encoding="utf-8")
            out_json, out_md, out_rows = root / "r.json", root / "r.md", root / "r.jsonl"
            code = lane.main(
                [
                    "--schedules", str(root),
                    "--finals", str(finals_dir),
                    "--start", "2026-09-01",
                    "--until", "2026-09-01",
                    "--out-json", str(out_json),
                    "--out-markdown", str(out_md),
                    "--out-rows", str(out_rows),
                ]
            )
            self.assertEqual(code, 0)
            payload = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(payload["rows"], 1)
            self.assertEqual(len(out_rows.read_text(encoding="utf-8").strip().splitlines()), 1)
            report = out_md.read_text(encoding="utf-8")
            self.assertIn("recorded_handicap", report)
            self.assertIn("never captured", report)

    def test_an_empty_window_reports_zero_rows_with_the_dates_named(self):
        # The first real run of this lane has no recorder output at all. It must
        # say "nothing here, and these are the dates" rather than print an empty
        # table that reads like a clean bill of health.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_json = root / "r.json"
            lane.main(
                [
                    "--schedules", str(root),
                    "--start", "2026-09-01",
                    "--until", "2026-09-02",
                    "--out-json", str(out_json),
                ]
            )
            payload = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(payload["rows"], 0)
            self.assertEqual(
                payload["dedup"]["dates_with_no_schedule"], ["2026-09-01", "2026-09-02"]
            )
            self.assertIn("2026-09-01", lane.markdown(payload))

    def test_a_window_of_schedules_with_no_denominator_names_every_date(self):
        # The population that actually exists: 613 of the 617 schedule files on
        # this fleet carry no usable `slate_denominator`. Before this test the
        # zero-row path was only ever exercised with no file at all, so the
        # common case was the untested one.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for date in ("2026-09-01", "2026-09-02"):
                sched = schedule()
                del sched["slate_denominator"]
                sched["date"] = date
                (root / f"{date}-schedule.json").write_text(json.dumps(sched), encoding="utf-8")
            out_json = root / "r.json"
            out_md = root / "r.md"
            lane.main(
                [
                    "--schedules", str(root),
                    "--start", "2026-09-01",
                    "--until", "2026-09-02",
                    "--out-json", str(out_json),
                    "--out-markdown", str(out_md),
                ]
            )
            payload = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(payload["rows"], 0)
            self.assertEqual(payload["dedup"]["dates_with_no_schedule"], [])
            audit = payload["schedule_audit"]
            self.assertEqual(audit["dates_used"], 2)
            self.assertEqual(
                [item["date"] for item in audit["dates_with_no_usable_denominator"]],
                ["2026-09-01", "2026-09-02"],
            )
            report = out_md.read_text(encoding="utf-8")
            for date in ("2026-09-01", "2026-09-02"):
                self.assertIn(f"No roster `{date}`", report)


if __name__ == "__main__":
    unittest.main()
