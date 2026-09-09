import copy
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import pytest
from scripts import mlb_game_reads as reads
from scripts import mlb_probability_model as model
from scripts import mlb_producer_prompt_contract as prompts
from scripts import mlb_slate_writer as writer
from test_mlb_producer_prompt_contract import legacy_prompt
from test_mlb_slate_writer import WriterTestCase, DAY, draft_for, scan_row

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/mlb_producer_schema_mismatch.json").read_text()
)


def test_schema_is_deterministic_and_uses_validator_vocabulary():
    contract = reads.producer_schema()
    assert contract == reads.producer_schema()
    assert contract["refusing_rails"] == sorted(reads.REFUSAL_RAILS)
    assert contract["unavailable_fields"] == list(reads.EXPLAINABLE_FIELDS)
    with mock.patch.object(
        reads, "REFUSAL_RAILS", reads.REFUSAL_RAILS | {"new_schema_rail"}
    ):
        assert reads.producer_schema()["sha256"] != contract["sha256"]


@pytest.mark.parametrize("job_id", [prompts.MORNING_JOB_ID, prompts.EVENING_JOB_ID])
def test_bound_prompt_matches_exact_schema_and_preserves_original_text(job_id):
    original = prompts.transform_prompt(
        job_id, legacy_prompt(evening=job_id == prompts.EVENING_JOB_ID)
    )
    bound = prompts.bind_schema_contract(job_id, original)
    assert bound.startswith(original)
    assert prompts.schema_contract_errors(job_id, bound) == []
    assert prompts.bind_schema_contract(job_id, bound) == bound
    assert reads.producer_schema()["sha256"] in bound
    assert "incomplete_input_data" in bound and "park_environment_cap" in bound
    assert (
        "unknown_park_environment belongs only to the probability-model haircut vocabulary"
        in bound
    )
    assert "unavailable.away_offense=true is invalid" in bound
    assert "Keep every skeleton game row" in bound
    assert prompts.schema_contract_errors(job_id, original)
    with mock.patch.object(
        reads, "REFUSAL_RAILS", reads.REFUSAL_RAILS | {"future_rail"}
    ):
        assert prompts.schema_contract_errors(job_id, bound)
        with pytest.raises(prompts.ProducerPromptError, match="differs"):
            prompts.bind_schema_contract(job_id, bound)
    assert prompts.schema_contract_errors(
        job_id, bound.replace("never silently map", "silently map")
    )


def test_unknown_park_has_no_refusal_alias():
    assert "unknown_park_environment" in model.HAIRCUT_COMPONENTS
    assert "unknown_park_environment" not in reads.REFUSAL_RAILS
    assert "away_offense" not in reads.EXPLAINABLE_FIELDS
    for case in FIXTURE["cases"]:
        if case.get("recorded_reason"):
            assert case["recorded_reason"] in reads.REFUSAL_RAILS
        if "refusing_rail" in case:
            assert case["refusing_rail"] not in reads.REFUSAL_RAILS


class ProducerSchemaLandingTests(WriterTestCase):
    def test_fifteen_row_payload_rejects_all_incident_tokens_without_rewriting(self):
        rows = [scan_row(820000 + i) for i in range(FIXTURE["row_count"])]
        self.write_scan(rows)
        valid = draft_for(rows)
        writer.land(self.root, DAY, valid)
        before = self.schedule_path().read_bytes()
        draft = copy.deepcopy(valid)
        for case in FIXTURE["cases"]:
            if "refusing_rail" in case:
                draft["game_reads"][case["row"]]["refusing_rails"].append(
                    case["refusing_rail"]
                )
            if "unavailable" in case:
                draft["game_reads"][case["row"]]["unavailable"] = case["unavailable"]
        draft_before = copy.deepcopy(draft)
        with self.assertRaises(writer.SlateWriteError) as caught:
            writer.land(self.root, DAY, draft)
        errors = "\n".join(caught.exception.errors)
        for token in (
            "extreme_park_confidence_cap",
            "missing_offense_data",
            "unknown_park_environment",
            "away_offense",
        ):
            self.assertIn(token, errors)
        self.assertEqual(draft, draft_before)
        self.assertEqual(len(draft["game_reads"]), 15)
        self.assertEqual(self.schedule_path().read_bytes(), before)
        missing = copy.deepcopy(valid)
        missing["game_reads"].pop(11)
        with self.assertRaises(writer.SlateWriteError):
            writer.land(self.root, DAY, missing)
        self.assertEqual(self.schedule_path().read_bytes(), before)

    def test_schema_mismatch_stops_both_modes_before_reading_or_writing(self):
        for mode in (
            ["--skeleton"],
            ["--land", str(self.root / "not-even-readable.json")],
        ):
            with (
                redirect_stdout(io.StringIO()),
                mock.patch.object(writer, "skeleton") as skeleton,
                mock.patch.object(writer, "land") as land,
            ):
                self.assertEqual(
                    writer.main(
                        mode + ["--schema-sha256", "stale", "--root", str(self.root)]
                    ),
                    1,
                )
                skeleton.assert_not_called()
                land.assert_not_called()
        self.assertFalse(self.schedule_path().exists())

    def test_matching_schema_retains_fifteen_rows_and_accepts_valid_source_reasons(
        self,
    ):
        rows = [scan_row(820000 + i) for i in range(15)]
        self.write_scan(rows)
        draft = draft_for(rows)
        draft["game_reads"][8]["refusing_rails"].append("park_environment_cap")
        draft["game_reads"][11]["refusing_rails"].append("incomplete_input_data")
        path = self.root / "separate-valid-draft.json"
        path.write_text(json.dumps(draft))
        digest = writer.mlb_game_reads.producer_schema()["sha256"]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(
                writer.main(
                    [
                        "--land",
                        str(path),
                        "--day",
                        DAY,
                        "--root",
                        str(self.root),
                        "--schema-sha256",
                        digest,
                    ]
                ),
                0,
            )
        self.assertEqual(
            len(json.loads(self.schedule_path().read_text())["game_reads"]), 15
        )
