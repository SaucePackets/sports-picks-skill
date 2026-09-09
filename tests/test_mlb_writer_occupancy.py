"""Immutable occupancy, including the September 8 evening-draft shape."""

import copy
import io
import json
from contextlib import redirect_stdout
from unittest import mock

from test_mlb_slate_writer import WriterTestCase, DAY, draft_for, scan_row, read_for
from scripts import mlb_slate_writer as writer


def candidate(row, **extra):
    return {
        "game_pk": row["game_pk"],
        "event_id": row["event_id"],
        "sport": "MLB",
        "market_type": "moneyline",
        **extra,
    }


def watchlist(row):
    return {
        "id": f"LW-{row['game_pk']}",
        **candidate(row),
        "game": row["event"],
        "side": row["away"],
        "first_pitch_utc": f"{DAY}T23:10Z",
        "recheck_due_utc": f"{DAY}T22:00Z",
        "blocked_only_by": ["lineups_unconfirmed"],
        "original_gate_results": {
            **{
                gate: True
                for gate in writer.mlb_lineup_watchlist.REQUIRED_ORIGINAL_GATES
            },
            "lineups_confirmed": False,
        },
        "original_price": 110,
        "bettable_to_price": 100,
        "status": "pending_lineup_recheck",
    }


class OccupancyTests(WriterTestCase):
    def existing(self, promoted=False, **state):
        rows = [scan_row(823509 + i) for i in range(3)]
        self.write_scan(rows)
        draft = draft_for(rows, candidates=[candidate(rows[0])])
        draft["game_reads"][0] = read_for(
            rows[0], disposition="candidate", refusing_rails=[]
        )
        _, schedule = writer.land(self.root, DAY, draft)
        schedule = copy.deepcopy(schedule)
        schedule["candidates"][0].update(
            vig_approved=True, vig_notes="review retained", **state
        )
        if promoted:
            entry = watchlist(rows[0])
            entry.update(
                status="promoted",
                rechecked_at_utc=f"{DAY}T22:00Z",
                recheck={
                    name: True
                    for name in (
                        "lineups_confirmed",
                        "key_injuries_refreshed",
                        "price_refreshed",
                        "all_original_gates_hold",
                    )
                },
                promoted_candidate={
                    "watchlist_id": entry["id"],
                    "execution_mode": "manual",
                    "manual_bet_status": "awaiting_jerry",
                    "executed": False,
                },
            )
            schedule["lineup_watchlist"] = [entry]
        # Noncanonical whitespace, Unicode escapes and numeric spelling must survive.
        raw = (
            json.dumps(schedule, indent=3)
            .replace('"market_type": "moneyline"', '"market_type" : "money\\u006cine"')
            .replace("0.398", "3.98e-1")
            + "\r\n"
        )
        self.schedule_path().write_bytes(raw.encode())
        return rows, draft, schedule, raw.encode()

    def test_reviewed_and_executed_candidate_is_immutable(self):
        for state in ({}, {"execution_status": "filled", "executed": True}):
            with self.subTest(state=state):
                self.schedule_path().unlink(missing_ok=True)
                rows, draft, schedule, before = self.existing(**state)
                draft["candidates"][0]["thesis"] = "stale producer overwrite"
                counts = {}
                _, actual = writer.land(self.root, DAY, draft, counts=counts)
                self.assertEqual(actual["candidates"], schedule["candidates"])
                self.assertEqual(self.schedule_path().read_bytes(), before)
                self.assertEqual(counts["skipped_occupied_entries"], 1)

    def test_promoted_watchlist_retry_cannot_downgrade_read_or_readd(self):
        rows, draft, schedule, before = self.existing(promoted=True)
        draft["candidates"] = []
        draft["lineup_watchlist"] = [watchlist(rows[0])]
        draft["game_reads"][0] = read_for(
            rows[0], disposition="lineup_watchlist", refusing_rails=[]
        )
        _, actual = writer.land(self.root, DAY, draft)
        self.assertEqual(actual, schedule)
        self.assertEqual(self.schedule_path().read_bytes(), before)

    def test_september_8_shape_omits_occupied_cards_and_retries_as_cli_noop(self):
        rows, draft, schedule, before = self.existing(promoted=True)
        draft["candidates"] = []  # read still says candidate, as in retained incident
        draft_path = self.root / "evening.json"
        draft_path.write_text(json.dumps(draft))
        for _ in range(2):
            output = io.StringIO()
            with (
                redirect_stdout(output),
                mock.patch.object(writer, "atomic_write") as write,
            ):
                self.assertEqual(
                    writer.main(
                        [
                            "--land",
                            str(draft_path),
                            "--day",
                            DAY,
                            "--root",
                            str(self.root),
                        ]
                    ),
                    0,
                    output.getvalue(),
                )
            receipt = json.loads(output.getvalue())
            self.assertTrue(receipt["landed"])
            self.assertEqual(receipt["new_candidates"], 0)
            self.assertEqual(receipt["new_watchlist_entries"], 0)
            self.assertEqual(receipt["retained_occupied_entries"], 2)
            write.assert_not_called()
            self.assertEqual(self.schedule_path().read_bytes(), before)

    def test_mixed_append_preserves_header_and_old_record_bytes_then_noop(self):
        rows, draft, schedule, before = self.existing(promoted=True)
        draft["market_type"] = "producer header must not replace existing"
        draft["candidates"].append(candidate(rows[1]))
        draft["game_reads"][1] = read_for(
            rows[1], disposition="candidate", refusing_rails=[]
        )
        draft["lineup_watchlist"] = [watchlist(rows[2])]
        draft["game_reads"][2] = read_for(
            rows[2], disposition="lineup_watchlist", refusing_rails=[]
        )
        counts = {}
        _, actual = writer.land(self.root, DAY, draft, counts=counts)
        self.assertEqual(counts["new_candidates"], 1)
        self.assertEqual(counts["new_watchlist_entries"], 1)
        self.assertEqual(counts["skipped_occupied_entries"], 1)
        after = self.schedule_path().read_bytes()
        self.assertIn(b'"market_type" : "money\\u006cine"', after)
        for key in ("candidates", "lineup_watchlist"):
            # Match exact original object lexemes, including indentation/escapes.
            value = schedule[key][0]
            decoder = json.JSONDecoder()
            text = before.decode()
            start = text.index("[", text.index('"' + key + '"')) + 1
            while text[start].isspace():
                start += 1
            _, end = decoder.raw_decode(text, start)
            self.assertIn(text[start:end].encode(), after)
            self.assertEqual(actual[key][0], value)
        retry_counts = {}
        writer.land(self.root, DAY, draft, counts=retry_counts)
        self.assertEqual(self.schedule_path().read_bytes(), after)
        self.assertEqual(retry_counts["new_candidates"], 0)
        self.assertEqual(retry_counts["new_watchlist_entries"], 0)
        self.assertEqual(retry_counts["skipped_occupied_entries"], 3)

    def test_event_only_identity_matches_but_conflicting_ids_fail_closed(self):
        rows, draft, _, before = self.existing()
        draft["candidates"][0].pop("game_pk")
        draft["candidates"][0]["event_id"] = int(rows[0]["event_id"])
        writer.land(self.root, DAY, draft)
        self.assertEqual(self.schedule_path().read_bytes(), before)
        draft["candidates"][0]["game_pk"] = rows[1]["game_pk"]
        with self.assertRaisesRegex(writer.SlateWriteError, "consistent stable"):
            writer.land(self.root, DAY, draft)
        self.assertEqual(self.schedule_path().read_bytes(), before)

    def test_doubleheader_games_are_distinct_and_ambiguous_event_fails(self):
        rows, draft, _, _ = self.existing()
        draft["candidates"].append(candidate(rows[1]))  # same clubs, different ids
        draft["game_reads"][1] = read_for(
            rows[1], disposition="candidate", refusing_rails=[]
        )
        counts = {}
        writer.land(self.root, DAY, draft, counts=counts)
        self.assertEqual(counts["new_candidates"], 1)
        with self.assertRaisesRegex(writer.SlateWriteError, "ambiguous"):
            writer.game_identity(
                {"event_id": "shared"},
                [
                    {"game_pk": 1, "event_id": "shared"},
                    {"game_pk": 2, "event_id": "shared"},
                ],
            )

    def test_malformed_drafts_are_not_hidden_by_occupancy(self):
        rows, draft, _, before = self.existing()
        variants = []
        bad = copy.deepcopy(draft)
        bad["candidates"][0]["vig_approved"] = False
        variants.append(bad)
        bad = copy.deepcopy(draft)
        bad["game_reads"][0]["raw_probability"] = None
        variants.append(bad)
        bad = copy.deepcopy(draft)
        bad["candidates"] = [None]
        variants.append(bad)
        bad = copy.deepcopy(draft)
        bad["candidates"][0].pop("game_pk")
        bad["candidates"][0].pop("event_id")
        variants.append(bad)
        bad = copy.deepcopy(draft)
        bad["game_reads"] = []
        variants.append(bad)
        for bad in variants:
            with self.subTest(bad=bad), self.assertRaises(writer.SlateWriteError):
                writer.land(self.root, DAY, bad)
            self.assertEqual(self.schedule_path().read_bytes(), before)

    def test_duplicate_json_key_and_changed_schedule_fail_closed(self):
        _, draft, _, before = self.existing()
        self.schedule_path().write_bytes(
            before.replace(b'"date":', b'"date":"wrong", "date":', 1)
        )
        with self.assertRaisesRegex(writer.SlateWriteError, "duplicate JSON key"):
            writer.land(self.root, DAY, draft)
        self.schedule_path().write_bytes(before)
        original = writer.preserving_payload

        def concurrent(*args):
            self.schedule_path().write_bytes(before + b" ")
            return original(*args)

        with mock.patch.object(writer, "preserving_payload", side_effect=concurrent):
            with self.assertRaisesRegex(
                writer.SlateWriteError, "changed during landing"
            ):
                writer.land(self.root, DAY, draft)
        self.assertEqual(self.schedule_path().read_bytes(), before + b" ")

    def test_new_candidate_cannot_borrow_another_games_disposition(self):
        rows, draft, _, before = self.existing()
        draft["candidates"].append(candidate(rows[1]))
        # Counts agree, identities do not: the third game's read claims the card.
        draft["game_reads"][2] = read_for(
            rows[2], disposition="candidate", refusing_rails=[]
        )
        with self.assertRaisesRegex(writer.SlateWriteError, "recorded disposition"):
            writer.land(self.root, DAY, draft)
        self.assertEqual(self.schedule_path().read_bytes(), before)

    def test_zero_cards_cannot_erase_an_occupied_schedule(self):
        rows, draft, schedule, before = self.existing(promoted=True)
        empty_card = draft_for(rows)  # all pass, but valid read coverage
        _, actual = writer.land(self.root, DAY, empty_card)
        self.assertEqual(actual, schedule)
        self.assertEqual(self.schedule_path().read_bytes(), before)
