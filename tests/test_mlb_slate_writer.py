"""Tests for the producer-side half: a schedule lands through code, or not at all.

PR #77 made every downstream check reachable. It could not change the fact that
the schedule itself was hand-authored from a prompt, which is why the 2026-09-01
run could handicap fifteen games and write a record covering none of them. These
cover the two properties that close that: the denominator is derived from the
scan and refused if transcribed, and an incomplete record is rejected *before*
anything is written — with the previous schedule left byte-identical.

They also pin the two ways this could be quietly wrong: a preflight check that
is a second opinion rather than the same function the gate calls, and a skeleton
that turns out to be a bypass because it lands as-is.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import vig_policy_state
from scripts import mlb_game_reads
from scripts import mlb_lineup_watchlist
from scripts import mlb_slate_receipt
from scripts import mlb_slate_writer
from scripts import vig_review_gate_common

DAY = "2026-09-01"


def scan_row(game_pk, away="Atlanta Braves", home="Milwaukee Brewers", **overrides):
    row = {
        "game_pk": game_pk,
        "event_id": f"4018{game_pk}",
        "event": f"{away} at {home}",
        "time": f"{DAY}T23:10Z",
        "away": away,
        "home": home,
        "away_fair": 0.398,
        "home_fair": 0.602,
    }
    row.update(overrides)
    return row


def read_for(row, **overrides):
    entry = {
        "game_pk": row["game_pk"],
        "event_id": row["event_id"],
        "away": row["away"],
        "home": row["home"],
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


def draft_for(rows, **overrides):
    payload = {
        "date": DAY,
        "sport": "MLB",
        "market_type": "moneyline",
        "candidates": [],
        "lineup_watchlist": [],
        "game_reads": [read_for(row) for row in rows],
    }
    payload.update(overrides)
    return payload


class WriterTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".picks" / "tmp").mkdir(parents=True)
        (self.root / ".picks" / "execute").mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)
        # Landing loads the deployed selection policy — the same floor the gate
        # will apply — so the fixture supplies one. Leaving it to the machine
        # would mean a read naming `price_discipline` validated on a developer
        # box with a live Vig state dir and failed on one without.
        policy = vig_policy_state.deployed_policy(self.root / "state")
        policy.__enter__()
        self.addCleanup(policy.__exit__, None, None, None)

    def policy(self):
        """The floor the gate would load, from this test's own state dir."""
        return vig_policy_state.loaded_policy(self.root / "state")

    def write_scan(self, rows):
        path = mlb_slate_writer.denominator_output_path(DAY, self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        return path

    def schedule_path(self):
        return mlb_slate_receipt.schedule_path_for(self.root, DAY)


class LandingTests(WriterTestCase):
    def test_a_complete_draft_lands_and_the_record_validates(self):
        rows = [scan_row(823509), scan_row(823510, away="New York Mets", home="Chicago Cubs")]
        self.write_scan(rows)

        path, schedule = mlb_slate_writer.land(self.root, DAY, draft_for(rows))

        self.assertEqual(path, self.schedule_path())
        self.assertTrue(path.exists())
        written = json.loads(path.read_text())
        self.assertEqual(written, schedule)
        # The record it wrote passes the rail the scheduled gate runs. A landing
        # check that accepts what the gate rejects is worse than none.
        self.assertEqual(mlb_game_reads.validate_with_denominator(path, written, self.policy()), [])

    def test_the_denominator_is_built_from_the_scan_not_from_the_draft(self):
        rows = [scan_row(823509), scan_row(823510, away="New York Mets", home="Chicago Cubs")]
        scan_path = self.write_scan(rows)

        _, schedule = mlb_slate_writer.land(self.root, DAY, draft_for(rows))

        denominator = schedule["slate_denominator"]
        self.assertEqual(denominator["source"], "mlb_stage2_scan")
        self.assertEqual(
            [game["game_pk"] for game in denominator["games"]], [823509, 823510]
        )
        self.assertEqual(
            [game["event_id"] for game in denominator["games"]],
            [row["event_id"] for row in rows],
        )
        # The timestamp is a fact about the artifact the roster came from, not a
        # claim the writeup made about a scan it could not see.
        self.assertEqual(
            denominator["fetched_at_utc"], mlb_slate_writer.scan_fetched_at(scan_path)
        )

    def test_a_draft_that_transcribes_the_denominator_is_refused(self):
        rows = [scan_row(823509)]
        self.write_scan(rows)
        draft = draft_for(
            rows,
            slate_denominator={
                "source": "mlb_stage2_scan",
                "fetched_at_utc": f"{DAY}T15:30:00+00:00",
                "games": [
                    {
                        "game_pk": 823509,
                        "event_id": "4018823509",
                        "away": "Atlanta Braves",
                        "home": "Milwaukee Brewers",
                    }
                ],
            },
        )

        with self.assertRaises(mlb_slate_writer.SlateWriteError) as caught:
            mlb_slate_writer.land(self.root, DAY, draft)

        self.assertTrue(
            any("must never be transcribed" in error for error in caught.exception.errors),
            caught.exception.errors,
        )
        self.assertFalse(self.schedule_path().exists())

    def test_a_draft_that_transcribes_a_correct_denominator_is_still_refused(self):
        """Refused for being transcribed, not for being wrong.

        A draft whose roster happens to match the scan exactly is the case that
        would make an "accept it if it agrees" rule look harmless — and that
        rule is how transcription survives, because the day it disagrees is the
        day nobody is checking.
        """
        rows = [scan_row(823509)]
        scan_path = self.write_scan(rows)
        exact = mlb_slate_writer.denominator_from_scan(
            rows, mlb_slate_writer.scan_fetched_at(scan_path)
        )

        with self.assertRaises(mlb_slate_writer.SlateWriteError) as caught:
            mlb_slate_writer.land(self.root, DAY, draft_for(rows, slate_denominator=exact))

        self.assertTrue(
            any("must never be transcribed" in error for error in caught.exception.errors),
            caught.exception.errors,
        )

    def test_a_read_set_short_of_the_scan_cannot_land(self):
        rows = [scan_row(823509), scan_row(823510, away="New York Mets", home="Chicago Cubs")]
        self.write_scan(rows)
        draft = draft_for(rows)
        draft["game_reads"] = draft["game_reads"][:1]

        with self.assertRaises(mlb_slate_writer.SlateWriteError) as caught:
            mlb_slate_writer.land(self.root, DAY, draft)

        self.assertIn(
            "scheduled game 823510 has no game_reads entry", caught.exception.errors
        )
        self.assertFalse(self.schedule_path().exists())

    def test_the_bare_2026_09_01_record_is_exactly_what_cannot_land(self):
        """The regression, in the shape it actually shipped.

        Fifteen games scanned, a schedule carrying candidates and no reads at
        all. It validated as 'Slate complete' because nothing looked before the
        write.
        """
        rows = [scan_row(823509 + offset) for offset in range(15)]
        self.write_scan(rows)
        bare = {
            "date": DAY,
            "sport": "MLB",
            "market_type": "moneyline",
            "candidates": [],
            "lineup_watchlist": [],
            "game_reads": [],
        }

        with self.assertRaises(mlb_slate_writer.SlateWriteError) as caught:
            mlb_slate_writer.land(self.root, DAY, bare)

        missing = [e for e in caught.exception.errors if "has no game_reads entry" in e]
        self.assertEqual(len(missing), 15, caught.exception.errors)
        self.assertFalse(self.schedule_path().exists())

    def test_an_invalid_read_cannot_land(self):
        rows = [scan_row(823509)]
        self.write_scan(rows)
        draft = draft_for(rows)
        # A partial model trail: the exact hole the PR #74 review found, here
        # reaching the writer rather than the gate.
        draft["game_reads"][0].pop("uncertainty_haircut")

        with self.assertRaises(mlb_slate_writer.SlateWriteError) as caught:
            mlb_slate_writer.land(self.root, DAY, draft)

        self.assertTrue(
            any("model trail" in error for error in caught.exception.errors),
            caught.exception.errors,
        )
        self.assertFalse(self.schedule_path().exists())

    def test_an_invalid_watchlist_entry_cannot_land(self):
        """The other validator the docs name. Both or the landing means less."""
        rows = [scan_row(823509)]
        self.write_scan(rows)
        draft = draft_for(rows)
        draft["game_reads"][0]["disposition"] = "lineup_watchlist"
        draft["game_reads"][0]["refusing_rails"] = []
        draft["lineup_watchlist"] = [{"id": "LW-20260901-ATL-001"}]

        with self.assertRaises(mlb_slate_writer.SlateWriteError) as caught:
            mlb_slate_writer.land(self.root, DAY, draft)

        self.assertTrue(
            any(error.startswith("lineup_watchlist[") for error in caught.exception.errors),
            caught.exception.errors,
        )
        self.assertFalse(self.schedule_path().exists())

    def test_a_missing_scan_refuses_the_landing_and_says_so(self):
        with self.assertRaises(mlb_slate_writer.SlateWriteError) as caught:
            mlb_slate_writer.land(self.root, DAY, draft_for([scan_row(823509)]))

        self.assertTrue(
            any("not readable" in error for error in caught.exception.errors),
            caught.exception.errors,
        )
        self.assertFalse(self.schedule_path().exists())

    def test_a_missing_scan_still_reports_the_drafts_own_defects(self):
        draft = draft_for([scan_row(823509)], sport="NFL")

        with self.assertRaises(mlb_slate_writer.SlateWriteError) as caught:
            mlb_slate_writer.land(self.root, DAY, draft)

        self.assertTrue(any("not readable" in e for e in caught.exception.errors))
        self.assertTrue(any("draft.sport" in e for e in caught.exception.errors))

    def test_a_scan_row_that_identifies_no_game_refuses_the_landing(self):
        rows = [
            scan_row(823509),
            {
                "game_pk": None,
                "event_id": "401816999",
                "event": "Seattle Mariners at Texas Rangers",
                "error": "unmatched: no MLB StatsAPI game for this ESPN event",
            },
        ]
        self.write_scan(rows)

        with self.assertRaises(mlb_slate_writer.SlateWriteError) as caught:
            mlb_slate_writer.land(self.root, DAY, draft_for([rows[0]]))

        self.assertTrue(
            any("unmatched" in error for error in caught.exception.errors),
            caught.exception.errors,
        )
        self.assertFalse(self.schedule_path().exists())

    def test_a_draft_with_no_read_list_is_refused_by_that_name(self):
        """A draft carrying no ``game_reads`` at all is the 09-01 shape exactly.

        It has to fail as "no read list", not as a hundred field errors over a
        composed record, or the one fact worth reading is buried.
        """
        rows = [scan_row(823509)]
        self.write_scan(rows)
        draft = draft_for(rows)
        draft.pop("game_reads")

        with self.assertRaises(mlb_slate_writer.SlateWriteError) as caught:
            mlb_slate_writer.land(self.root, DAY, draft)

        self.assertIn(
            "draft.game_reads must be a list, one entry per scheduled game",
            caught.exception.errors,
        )
        self.assertFalse(self.schedule_path().exists())

    def test_a_draft_that_is_not_an_object_is_refused(self):
        self.write_scan([scan_row(823509)])
        with self.assertRaises(mlb_slate_writer.SlateWriteError) as caught:
            mlb_slate_writer.land(self.root, DAY, ["not", "a", "draft"])
        self.assertIn("draft must be a JSON object", caught.exception.errors)
        self.assertFalse(self.schedule_path().exists())

    def test_the_day_a_draft_is_filed_under_must_be_the_day_it_names(self):
        rows = [scan_row(823509)]
        self.write_scan(rows)

        with self.assertRaises(mlb_slate_writer.SlateWriteError) as caught:
            mlb_slate_writer.land(self.root, DAY, draft_for(rows, date="2026-08-31"))

        self.assertTrue(
            any("the day being landed" in error for error in caught.exception.errors),
            caught.exception.errors,
        )


class NonDestructiveTests(WriterTestCase):
    def test_a_refused_landing_leaves_the_previous_schedule_byte_identical(self):
        rows = [scan_row(823509), scan_row(823510, away="New York Mets", home="Chicago Cubs")]
        self.write_scan(rows)
        mlb_slate_writer.land(self.root, DAY, draft_for(rows))
        before = self.schedule_path().read_bytes()

        short = draft_for(rows)
        short["game_reads"] = short["game_reads"][:1]
        with self.assertRaises(mlb_slate_writer.SlateWriteError):
            mlb_slate_writer.land(self.root, DAY, short)

        self.assertEqual(self.schedule_path().read_bytes(), before)

    def test_a_landing_leaves_no_temporary_file_behind(self):
        rows = [scan_row(823509)]
        self.write_scan(rows)
        mlb_slate_writer.land(self.root, DAY, draft_for(rows))

        leftovers = [
            p.name
            for p in (self.root / ".picks" / "execute").iterdir()
            if p.name != f"{DAY}-schedule.json"
        ]
        self.assertEqual(leftovers, [])

    def test_a_reviewed_card_is_never_overwritten(self):
        rows = [scan_row(823509)]
        self.write_scan(rows)
        reviewed = draft_for(rows)
        reviewed["game_reads"][0]["disposition"] = "candidate"
        reviewed["game_reads"][0]["refusing_rails"] = []
        reviewed["candidates"] = [{"sport": "MLB", "vig_approved": True}]
        self.schedule_path().write_text(json.dumps(reviewed), encoding="utf-8")
        before = self.schedule_path().read_bytes()

        with self.assertRaises(mlb_slate_writer.SlateWriteError) as caught:
            mlb_slate_writer.land(self.root, DAY, draft_for(rows))

        self.assertTrue(
            any("landing would erase it" in error for error in caught.exception.errors),
            caught.exception.errors,
        )
        self.assertEqual(self.schedule_path().read_bytes(), before)

    def test_a_rechecked_watchlist_entry_is_never_overwritten(self):
        rows = [scan_row(823509)]
        self.write_scan(rows)
        self.schedule_path().write_text(
            json.dumps({"lineup_watchlist": [{"id": "LW-1", "status": "expired"}]}),
            encoding="utf-8",
        )

        with self.assertRaises(mlb_slate_writer.SlateWriteError) as caught:
            mlb_slate_writer.land(self.root, DAY, draft_for(rows))

        self.assertTrue(
            any("has been rechecked" in error for error in caught.exception.errors),
            caught.exception.errors,
        )

    def test_an_unparseable_existing_schedule_is_never_overwritten(self):
        rows = [scan_row(823509)]
        self.write_scan(rows)
        self.schedule_path().write_text("{ not json", encoding="utf-8")

        with self.assertRaises(mlb_slate_writer.SlateWriteError) as caught:
            mlb_slate_writer.land(self.root, DAY, draft_for(rows))

        self.assertTrue(
            any("contents are unknown" in error for error in caught.exception.errors),
            caught.exception.errors,
        )
        self.assertEqual(self.schedule_path().read_text(), "{ not json")

    def test_a_still_pending_watchlist_entry_does_not_block_a_relanding(self):
        """The producer's own output must not read as somebody else's decision.

        A watchlist entry this run just wrote carries ``pending_lineup_recheck``.
        Treating any status but the literal ``"pending"`` as "rechecked" would
        block every ordinary re-run — the rail keys on the module's constant.
        """
        rows = [scan_row(823509)]
        self.write_scan(rows)
        self.schedule_path().write_text(
            json.dumps(
                {
                    "lineup_watchlist": [
                        {"id": "LW-1", "status": mlb_lineup_watchlist.PENDING_STATUS}
                    ]
                }
            ),
            encoding="utf-8",
        )

        self.assertEqual(mlb_slate_writer.occupancy_errors(self.schedule_path()), [])
        self.assertTrue(mlb_slate_writer.land(self.root, DAY, draft_for(rows))[0].exists())

    def test_an_unreviewed_schedule_may_be_relanded(self):
        """Re-running the slate before anyone has ruled on it is ordinary."""
        rows = [scan_row(823509)]
        self.write_scan(rows)
        mlb_slate_writer.land(self.root, DAY, draft_for(rows))

        relanded = draft_for(rows)
        relanded["game_reads"][0]["refusing_rails"] = ["starter_floor"]
        _, schedule = mlb_slate_writer.land(self.root, DAY, relanded)

        self.assertEqual(schedule["game_reads"][0]["refusing_rails"], ["starter_floor"])


class AuthoredDecisionTests(WriterTestCase):
    """The producer may not author the decisions the reviewer and executor own.

    The occupancy check already refuses to *erase* a decision. These cover the
    other direction — a landing that *invents* one — because the executor reads
    ``vig_approved`` straight off the schedule and the review queue only holds
    candidates whose value is not yet a bool, so a producer-authored ``true``
    reaches the executor never having been reviewed.

    The positive control is the load-bearing one. The producer's canonical
    candidate spells all four fields out as ``null``/``false``, so a guard
    written as a key-presence test would refuse every ordinary slate — and
    without a test that a real candidate still lands, that guard is
    indistinguishable from one that refuses everything.
    """

    # scripts/../skills/sports-picks/SKILL.md, verbatim: what the producer is
    # told to write. If this stops landing, the guard is broken, not the slate.
    CANONICAL_CANDIDATE = {
        "sport": "MLB",
        "market_type": "moneyline",
        "vig_review_needed": True,
        "vig_approved": None,
        "vig_notes": None,
        "execution_mode": "standing_authorized",
        "execution_status": None,
        "max_polymarket_price": 0.51,
        "executed": False,
    }

    def draft_with_candidate(self, rows, **overrides):
        candidate = dict(self.CANONICAL_CANDIDATE)
        candidate.update(overrides)
        draft = draft_for(rows)
        draft["candidates"] = [candidate]
        draft["game_reads"][0]["disposition"] = "candidate"
        draft["game_reads"][0]["refusing_rails"] = []
        return draft

    def test_the_canonical_producer_candidate_still_lands(self):
        rows = [scan_row(823509)]
        self.write_scan(rows)

        path, schedule = mlb_slate_writer.land(
            self.root, DAY, self.draft_with_candidate(rows)
        )

        self.assertTrue(path.exists())
        self.assertEqual(schedule["candidates"], [self.CANONICAL_CANDIDATE])
        # And it is still the thing the review queue is supposed to pick up.
        self.assertEqual(
            vig_review_gate_common.pending_candidates(schedule["candidates"]),
            schedule["candidates"],
        )

    def test_a_draft_that_approves_its_own_candidate_is_refused(self):
        rows = [scan_row(823509)]
        self.write_scan(rows)
        draft = self.draft_with_candidate(rows, vig_approved=True)

        with self.assertRaises(mlb_slate_writer.SlateWriteError) as caught:
            mlb_slate_writer.land(self.root, DAY, draft)

        self.assertTrue(
            any(
                "draft.candidates[0] already carries vig_approved" in error
                for error in caught.exception.errors
            ),
            caught.exception.errors,
        )
        self.assertFalse(self.schedule_path().exists())

    def test_the_refused_shape_is_the_one_that_would_reach_the_executor(self):
        """Why the guard exists, stated as the two facts that make it necessary.

        A producer-authored card in this exact shape is not queued for review —
        ``pending_candidates`` holds only candidates whose ``vig_approved`` is
        not yet a bool — and it satisfies the executor's own state predicate.
        The writer is the only thing between it and the executor.
        """
        rows = [scan_row(823509)]
        self.write_scan(rows)
        approved = dict(
            self.CANONICAL_CANDIDATE,
            vig_approved=True,
            execution_status="pending",
        )

        self.assertEqual(vig_review_gate_common.pending_candidates([approved]), [])

        with self.assertRaises(mlb_slate_writer.SlateWriteError):
            mlb_slate_writer.land(
                self.root, DAY, self.draft_with_candidate(rows, **approved)
            )

    def test_a_draft_that_rejects_its_own_candidate_is_also_refused(self):
        """``false`` is a ruling too — the rail is about authorship, not polarity."""
        rows = [scan_row(823509)]
        self.write_scan(rows)

        with self.assertRaises(mlb_slate_writer.SlateWriteError) as caught:
            mlb_slate_writer.land(
                self.root, DAY, self.draft_with_candidate(rows, vig_approved=False)
            )

        self.assertTrue(
            any("vig_approved" in error for error in caught.exception.errors),
            caught.exception.errors,
        )

    def test_every_decision_field_is_refused_and_named(self):
        """One case per field, so growing the tuple cannot outrun the coverage."""
        stamps = {
            "vig_approved": True,
            "vig_notes": "cleared on the 08-30 read",
            "execution_status": "pending",
            "executed": True,
        }
        self.assertEqual(
            sorted(stamps), sorted(mlb_slate_writer.CANDIDATE_STATE_FIELDS + ("executed",))
        )
        for field, value in stamps.items():
            with self.subTest(field=field):
                rows = [scan_row(823509)]
                self.write_scan(rows)
                draft = self.draft_with_candidate(rows, **{field: value})

                with self.assertRaises(mlb_slate_writer.SlateWriteError) as caught:
                    mlb_slate_writer.land(self.root, DAY, draft)

                self.assertTrue(
                    any(
                        f"draft.candidates[0] already carries {field}" in error
                        for error in caught.exception.errors
                    ),
                    caught.exception.errors,
                )
                self.assertFalse(self.schedule_path().exists())

    def test_execution_mode_is_not_a_decision(self):
        """``standing_authorized`` says what may happen later, not that it did.

        It is in the producer's template on every card. Treating it as decision
        state would refuse every ordinary slate.
        """
        self.assertNotIn("execution_mode", mlb_slate_writer.CANDIDATE_STATE_FIELDS)
        self.assertEqual(
            mlb_slate_writer.decision_fields(
                {"execution_mode": "standing_authorized", "vig_review_needed": True}
            ),
            [],
        )

    def test_the_offending_card_is_named_by_index(self):
        rows = [scan_row(823509)]
        self.write_scan(rows)
        draft = self.draft_with_candidate(rows)
        draft["candidates"] = [
            dict(self.CANONICAL_CANDIDATE),
            dict(self.CANONICAL_CANDIDATE, executed=True),
        ]

        errors = mlb_slate_writer.draft_errors(draft, DAY)

        self.assertEqual(
            [error for error in errors if "already carries" in error],
            [
                "draft.candidates[1] already carries executed; review and execution "
                "state is written by the reviewer and the executor, never by the "
                "producer — leave those fields null (and executed false) in the draft"
            ],
        )

    def test_a_refused_authorship_leaves_the_previous_schedule_byte_identical(self):
        rows = [scan_row(823509)]
        self.write_scan(rows)
        mlb_slate_writer.land(self.root, DAY, self.draft_with_candidate(rows))
        before = self.schedule_path().read_bytes()

        with self.assertRaises(mlb_slate_writer.SlateWriteError):
            mlb_slate_writer.land(
                self.root, DAY, self.draft_with_candidate(rows, vig_approved=True)
            )

        self.assertEqual(self.schedule_path().read_bytes(), before)

    def test_a_candidate_list_that_is_not_a_list_is_refused(self):
        rows = [scan_row(823509)]
        self.write_scan(rows)
        draft = draft_for(rows)
        draft["candidates"] = {"sport": "MLB", "vig_approved": True}

        errors = mlb_slate_writer.draft_errors(draft, DAY)

        self.assertIn("draft.candidates must be a list of candidate objects", errors)

    def test_a_draft_with_no_candidates_key_is_ordinary(self):
        """A pass-everything slate names no cards at all; ``compose`` fills it in."""
        rows = [scan_row(823509)]
        self.write_scan(rows)
        draft = draft_for(rows)
        draft.pop("candidates")

        _, schedule = mlb_slate_writer.land(self.root, DAY, draft)

        self.assertEqual(schedule["candidates"], [])

    def test_a_non_object_candidate_carries_no_decision(self):
        self.assertEqual(mlb_slate_writer.decision_fields("vig_approved"), [])
        self.assertEqual(mlb_slate_writer.decision_fields(None), [])

    def test_both_rails_ask_one_predicate_rather_than_two_copies(self):
        """The consultation, proven by rebinding the source and both answers moving.

        The occupancy check asks it of the file being replaced and the draft
        check asks it of the record replacing it. Asserting the two agree on
        some card would pass just as well against two identical copies; forcing
        the shared name to lie is what makes this a consultation pin.
        """
        rows = [scan_row(823509)]
        self.write_scan(rows)
        clean = self.draft_with_candidate(rows)
        mlb_slate_writer.land(self.root, DAY, clean)
        approved = self.draft_with_candidate(rows, vig_approved=True)

        # Baseline in both directions with the real predicate: the existing
        # schedule is landable and the approved draft is not.
        self.assertEqual(mlb_slate_writer.occupancy_errors(self.schedule_path()), [])
        self.assertTrue(
            [error for error in mlb_slate_writer.draft_errors(approved, DAY)]
        )

        with mock.patch.object(
            mlb_slate_writer, "decision_fields", return_value=["sentinel"]
        ):
            self.assertTrue(
                any(
                    "sentinel" in error
                    for error in mlb_slate_writer.occupancy_errors(self.schedule_path())
                )
            )
            self.assertTrue(
                any(
                    "sentinel" in error
                    for error in mlb_slate_writer.draft_errors(clean, DAY)
                )
            )

        with mock.patch.object(mlb_slate_writer, "decision_fields", return_value=[]):
            reviewed = json.loads(self.schedule_path().read_text())
            reviewed["candidates"][0]["vig_approved"] = True
            self.schedule_path().write_text(json.dumps(reviewed), encoding="utf-8")
            self.assertEqual(mlb_slate_writer.occupancy_errors(self.schedule_path()), [])
            self.assertEqual(
                [
                    error
                    for error in mlb_slate_writer.draft_errors(approved, DAY)
                    if "already carries" in error
                ],
                [],
            )


class SkeletonTests(WriterTestCase):
    def test_the_skeleton_carries_one_stub_per_scanned_game_with_both_id_spaces(self):
        rows = [scan_row(823509), scan_row(823510, away="New York Mets", home="Chicago Cubs")]
        self.write_scan(rows)

        draft = mlb_slate_writer.skeleton(self.root, DAY)

        self.assertEqual(len(draft["game_reads"]), 2)
        self.assertEqual(
            [(r["game_pk"], r["event_id"]) for r in draft["game_reads"]],
            [(row["game_pk"], row["event_id"]) for row in rows],
        )
        self.assertNotIn("slate_denominator", draft)

    def test_the_skeleton_prefills_dk_fair_only_when_the_scan_has_both_sides(self):
        rows = [
            scan_row(823509),
            scan_row(823510, away="New York Mets", home="Chicago Cubs", home_fair=None),
        ]
        self.write_scan(rows)

        reads = mlb_slate_writer.skeleton(self.root, DAY)["game_reads"]

        self.assertEqual(reads[0]["dk_fair_prob"], {"away": 0.398, "home": 0.602})
        self.assertNotIn("dk_fair_prob", reads[1])

    def test_an_unfilled_skeleton_cannot_land(self):
        """The skeleton is a head start, not a bypass.

        If it landed as written we would have replaced one silent empty record
        with another, and this time the tooling would have produced it.
        """
        rows = [scan_row(823509)]
        self.write_scan(rows)

        with self.assertRaises(mlb_slate_writer.SlateWriteError) as caught:
            mlb_slate_writer.land(self.root, DAY, mlb_slate_writer.skeleton(self.root, DAY))

        self.assertTrue(
            any("disposition" in error for error in caught.exception.errors),
            caught.exception.errors,
        )
        self.assertFalse(self.schedule_path().exists())

    def test_the_skeleton_refuses_a_missing_scan(self):
        with self.assertRaises(mlb_slate_writer.SlateWriteError) as caught:
            mlb_slate_writer.skeleton(self.root, DAY)
        self.assertTrue(any("not readable" in e for e in caught.exception.errors))


class SameRuleAsTheGateTests(WriterTestCase):
    def test_the_writer_holds_the_same_modules_the_gate_and_receipt_do(self):
        self.assertEqual(
            mlb_slate_writer.mlb_game_reads.__file__, mlb_game_reads.__file__
        )
        self.assertEqual(
            mlb_slate_writer.mlb_lineup_watchlist.__file__, mlb_lineup_watchlist.__file__
        )

    def test_the_landing_check_consults_the_validator_rather_than_re_deriving_it(self):
        """Rebind the source and require the answer to follow.

        Asserting that a valid draft lands and an invalid one does not would be
        satisfied by a hand-rolled copy of the coverage rule inside this module,
        which is precisely the drift that would let the writer accept a record
        the gate rejects.
        """
        rows = [scan_row(823509)]
        self.write_scan(rows)
        draft = draft_for(rows)

        with mock.patch.object(
            mlb_slate_writer.mlb_game_reads,
            "validate_with_denominator",
            return_value=["injected: the validator was consulted"],
        ):
            with self.assertRaises(mlb_slate_writer.SlateWriteError) as caught:
                mlb_slate_writer.land(self.root, DAY, draft)
        self.assertIn("injected: the validator was consulted", caught.exception.errors)
        self.assertFalse(self.schedule_path().exists())

        # And the other direction: with the real validator the same draft lands,
        # so the assertion above cannot pass for the wrong reason.
        self.assertTrue(mlb_slate_writer.land(self.root, DAY, draft)[0].exists())

    def test_the_landing_check_consults_the_watchlist_validator(self):
        rows = [scan_row(823509)]
        self.write_scan(rows)
        draft = draft_for(rows)

        with mock.patch.object(
            mlb_slate_writer.mlb_lineup_watchlist,
            "validate_watchlist",
            return_value={"LW-1": ["injected"]},
        ):
            with self.assertRaises(mlb_slate_writer.SlateWriteError) as caught:
                mlb_slate_writer.land(self.root, DAY, draft)
        self.assertIn("lineup_watchlist[LW-1]: injected", caught.exception.errors)

    def test_the_skeletons_probability_rule_is_the_validators(self):
        """A pre-filled dk_fair_prob the validator would reject is worse than none."""
        self.assertIs(
            mlb_slate_writer.mlb_game_reads._is_probability,
            sys.modules["mlb_game_reads"]._is_probability,
        )


class PostflightUnchangedTests(WriterTestCase):
    def test_a_landed_schedule_reads_complete_to_the_receipt(self):
        rows = [scan_row(823509), scan_row(823510, away="New York Mets", home="Chicago Cubs")]
        self.write_scan(rows)
        mlb_slate_writer.land(self.root, DAY, draft_for(rows))

        receipt = mlb_slate_receipt.build_receipt(self.root, DAY)

        self.assertEqual(receipt["verdict"], mlb_slate_receipt.VERDICT_COMPLETE)
        self.assertEqual(receipt["recorder_errors"], [])
        self.assertEqual(receipt["scheduled_games"], 2)
        self.assertEqual(receipt["reads_recorded"], 2)

    def test_a_hand_written_bare_schedule_still_reads_recorder_failed(self):
        """The producer-side rail does not retire the postflight one.

        Nothing here stops a run writing the schedule by hand. That path is
        still caught, and by the same verdict as before this change.
        """
        rows = [scan_row(823509 + offset) for offset in range(15)]
        self.write_scan(rows)
        self.schedule_path().write_text(
            json.dumps(
                {
                    "date": DAY,
                    "sport": "MLB",
                    "market_type": "moneyline",
                    "candidates": [],
                    "lineup_watchlist": [],
                }
            ),
            encoding="utf-8",
        )

        receipt = mlb_slate_receipt.build_receipt(self.root, DAY)

        self.assertEqual(receipt["verdict"], mlb_slate_receipt.VERDICT_RECORDER_FAILED)
        self.assertEqual(receipt["scheduled_games"], 15)


class IdentityCanonicalFormTests(WriterTestCase):
    """The date is not the only field that validates in one spelling.

    ``identity_agreement_errors`` compares the two ``event_id``s stripped, so a
    padded id agrees and then lands padded. The id is an address as well as a
    label — ``mlb_lineup_watchlist`` builds a URL out of it — so the padded
    form is a wrong address, the same failure item (2) exists to kill.
    """

    def test_a_padded_event_id_persists_canonical_on_both_halves(self):
        rows = [scan_row(823509, event_id="  4018823509  ")]
        self.write_scan(rows)
        draft = draft_for(rows)
        draft["game_reads"][0]["event_id"] = "\t4018823509\n"

        path, schedule = mlb_slate_writer.land(self.root, DAY, draft)

        written = json.loads(path.read_text())
        self.assertEqual(written["game_reads"][0]["event_id"], "4018823509")
        self.assertEqual(
            written["slate_denominator"]["games"][0]["event_id"], "4018823509"
        )
        self.assertEqual(written, schedule)
        self.assertEqual(mlb_game_reads.validate_with_denominator(path, written, self.policy()), [])

    def test_a_crossing_written_in_another_vocabulary_lands_by_design(self):
        """The declared limit, at the boundary an operator actually uses.

        The unit test pins the rule; this pins what the CLI does with it, which
        is what the doc's promise is about. A read that renames AND crosses the
        clubs lands, because crossing is decided by comparing names and these
        names match on neither side. Recorded here so the limit is a checked
        fact rather than something rediscovered by probing the landed record.
        """
        rows = [scan_row(823509)]
        self.write_scan(rows)

        draft = draft_for(rows)
        entry = draft["game_reads"][0]
        entry["away"], entry["home"] = "MIL", "ATL"

        _, schedule = mlb_slate_writer.land(self.root, DAY, draft)
        self.assertEqual(schedule["game_reads"][0]["away"], "MIL")

        # And the same crossing in the scan's own vocabulary — which is what
        # ``--skeleton`` prefills — is refused, so the landing above is the
        # stated limit and not a dead rail.
        entry["away"], entry["home"] = rows[0]["home"], rows[0]["away"]
        with self.assertRaises(mlb_slate_writer.SlateWriteError) as caught:
            mlb_slate_writer.land(self.root, DAY, draft)
        self.assertTrue(
            any("away/home transposed" in error for error in caught.exception.errors),
            caught.exception.errors,
        )


class SlateDateTests(WriterTestCase):
    """The value that validates must be the value that persists.

    The day is the record's address: the schedule filename, the scan
    artifact's conventional path and every later job's lookup are derived from
    it. ``draft_errors`` compared ``date.strip()`` against the day and
    ``compose`` copied the draft verbatim, so a padded date passed on its
    stripped form and was written with the padding intact — a schedule that
    validates and is then filed where nothing looks for it.
    """

    def test_a_padded_draft_date_persists_canonical(self):
        rows = [scan_row(823509)]
        self.write_scan(rows)
        draft = draft_for(rows, date=f"  {DAY}\n")

        path, schedule = mlb_slate_writer.land(self.root, DAY, draft)

        self.assertEqual(schedule["date"], DAY)
        # Read back off disk: the in-memory return value agreeing is not the
        # claim — the persisted record is.
        self.assertEqual(json.loads(path.read_text())["date"], DAY)
        # And the canonical record is the one the gate reads.
        self.assertEqual(
            mlb_game_reads.validate_with_denominator(
                path, json.loads(path.read_text()), self.policy()
            ),
            [],
        )

    def test_the_draft_is_not_mutated_by_landing_it(self):
        rows = [scan_row(823509)]
        self.write_scan(rows)
        draft = draft_for(rows, date=f" {DAY} ")

        mlb_slate_writer.land(self.root, DAY, draft)

        self.assertEqual(draft["date"], f" {DAY} ")

    def test_a_padded_day_still_addresses_the_canonical_path(self):
        """Normalising the draft alone would leave the FILENAME padded."""
        rows = [scan_row(823509)]
        self.write_scan(rows)

        path, schedule = mlb_slate_writer.land(self.root, f" {DAY} ", draft_for(rows))

        self.assertEqual(path, self.schedule_path())
        self.assertEqual(schedule["date"], DAY)

    def test_a_date_that_is_not_a_real_day_is_refused(self):
        """Shape is not enough: ``2026-02-30`` matches ``YYYY-MM-DD``.

        A schedule filed under a day that cannot occur is unreachable rather
        than merely odd — nothing will ever ask for it.
        """
        rows = [scan_row(823509)]
        self.write_scan(rows)
        draft = draft_for(rows, date="2026-02-30")

        errors = mlb_slate_writer.draft_errors(draft, "2026-02-30")

        self.assertTrue(
            any("not a real calendar date" in error for error in errors), errors
        )

    def test_an_unpadded_shape_variant_is_refused_rather_than_guessed(self):
        rows = [scan_row(823509)]
        self.write_scan(rows)

        for spelling in ("2026-9-1", "20260901", "09/01/2026", "2026-09-01T00:00:00Z"):
            with self.subTest(spelling=spelling):
                errors = mlb_slate_writer.draft_errors(
                    draft_for(rows, date=spelling), DAY
                )
                self.assertTrue(
                    any("is not a YYYY-MM-DD date" in error for error in errors), errors
                )

    def test_the_accepted_vocabulary_does_not_move_with_the_interpreter(self):
        """Spelled out rather than delegated to ``date.fromisoformat``.

        That function's accepted set has grown across Python versions (3.11
        began taking ``"20260901"``), and a normaliser whose input vocabulary
        depends on the interpreter is not a canonical form.
        """
        self.assertEqual(mlb_slate_writer.normalize_slate_date(f" {DAY} "), DAY)
        for rejected in ("20260901", "2026-9-01", "2026-09-1", "", "  ", None, 20260901):
            with self.subTest(rejected=rejected):
                with self.assertRaises(ValueError):
                    mlb_slate_writer.normalize_slate_date(rejected)

    def test_a_wrong_but_well_formed_date_is_still_refused(self):
        """Normalising is not accepting: the day being landed still decides."""
        rows = [scan_row(823509)]
        self.write_scan(rows)

        errors = mlb_slate_writer.draft_errors(draft_for(rows, date="2026-09-02"), DAY)

        self.assertIn(
            f"draft.date is '2026-09-02' but the day being landed is {DAY!r}; "
            "a schedule filed under the wrong date is invisible to every later job",
            errors,
        )

    def test_the_skeleton_carries_the_canonical_date(self):
        """The stub the producer fills in must pass the check it was made for."""
        rows = [scan_row(823509)]
        self.write_scan(rows)

        draft = mlb_slate_writer.skeleton(self.root, f"{DAY} ")

        self.assertEqual(draft["date"], DAY)

    def test_landing_an_unusable_day_writes_nothing(self):
        rows = [scan_row(823509)]
        self.write_scan(rows)

        with self.assertRaises(mlb_slate_writer.SlateWriteError):
            mlb_slate_writer.land(self.root, "2026-02-30", draft_for(rows))

        self.assertFalse(self.schedule_path().exists())
        self.assertEqual(list((self.root / ".picks" / "execute").iterdir()), [])


class CliTests(WriterTestCase):
    def test_skeleton_writes_the_draft_and_refuses_to_clobber_it(self):
        rows = [scan_row(823509)]
        self.write_scan(rows)
        argv = ["--skeleton", "--day", DAY, "--root", str(self.root)]

        self.assertEqual(mlb_slate_writer.main(argv), 0)
        destination = mlb_slate_writer.default_draft_path(self.root, DAY)
        self.assertTrue(destination.exists())
        filled = json.loads(destination.read_text())
        filled["game_reads"] = [read_for(rows[0])]
        destination.write_text(json.dumps(filled), encoding="utf-8")

        # A second --skeleton must not discard the reads just filled in.
        self.assertEqual(mlb_slate_writer.main(argv), 1)
        self.assertEqual(json.loads(destination.read_text()), filled)

        self.assertEqual(
            mlb_slate_writer.main(
                ["--land", str(destination), "--day", DAY, "--root", str(self.root)]
            ),
            0,
        )
        self.assertTrue(self.schedule_path().exists())

    def test_land_exits_nonzero_and_writes_nothing_on_a_refusal(self):
        rows = [scan_row(823509), scan_row(823510, away="New York Mets", home="Chicago Cubs")]
        self.write_scan(rows)
        draft_path = self.root / "draft.json"
        short = draft_for(rows)
        short["game_reads"] = short["game_reads"][:1]
        draft_path.write_text(json.dumps(short), encoding="utf-8")

        self.assertEqual(
            mlb_slate_writer.main(
                ["--land", str(draft_path), "--day", DAY, "--root", str(self.root)]
            ),
            1,
        )
        self.assertFalse(self.schedule_path().exists())

    def test_a_malformed_day_is_a_usage_error_not_a_finding(self):
        """Every path below ``--day`` is built from it.

        There is no day whose record could be reported on, so this exits as a
        usage error rather than printing a landing verdict about a slate that
        has no address.
        """
        rows = [scan_row(823509)]
        self.write_scan(rows)
        draft_path = self.root / "draft.json"
        draft_path.write_text(json.dumps(draft_for(rows)), encoding="utf-8")

        for spelling in (" ", "2026-02-30", "20260901"):
            with self.subTest(spelling=spelling):
                with self.assertRaises(SystemExit) as caught:
                    mlb_slate_writer.main(
                        [
                            "--land",
                            str(draft_path),
                            "--day",
                            spelling,
                            "--root",
                            str(self.root),
                        ]
                    )
                self.assertEqual(caught.exception.code, 2)

    def test_a_padded_day_lands_at_the_canonical_path(self):
        rows = [scan_row(823509)]
        self.write_scan(rows)
        draft_path = self.root / "draft.json"
        draft_path.write_text(json.dumps(draft_for(rows)), encoding="utf-8")

        self.assertEqual(
            mlb_slate_writer.main(
                ["--land", str(draft_path), "--day", f" {DAY} ", "--root", str(self.root)]
            ),
            0,
        )

        self.assertTrue(self.schedule_path().exists())
        self.assertEqual(json.loads(self.schedule_path().read_text())["date"], DAY)

    def test_a_mode_is_required(self):
        with self.assertRaises(SystemExit):
            mlb_slate_writer.main(["--day", DAY, "--root", str(self.root)])


if __name__ == "__main__":
    unittest.main()
