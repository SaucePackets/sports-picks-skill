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

from scripts import mlb_game_reads
from scripts import mlb_lineup_watchlist
from scripts import mlb_slate_receipt
from scripts import mlb_slate_writer

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
        "net_edge": {"away": -0.080, "home": 0.035},
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
        self.assertEqual(mlb_game_reads.validate_with_denominator(path, written), [])

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

    def test_a_mode_is_required(self):
        with self.assertRaises(SystemExit):
            mlb_slate_writer.main(["--day", DAY, "--root", str(self.root)])


if __name__ == "__main__":
    unittest.main()
