from __future__ import annotations

from unittest import mock

import pytest

from scripts import mlb_producer_prompt_contract as contract


def legacy_prompt(*, evening: bool) -> str:
    lines = ["unchanged prefix"]
    if evening:
        lines.append(contract.EVENING_MERGE_PREFIX + " and append entries")
    lines.extend(
        [
            contract.SCAN_PREFIX + " and then run Stage 2.",
            contract.WRITE_PREFIX + " as an object.",
            contract.VALIDATE_PREFIX + " with the lineup validator.",
            contract.POSTFLIGHT_WRITE_PREFIX
            + " and a valid JSON write for the schedule.",
            contract.POSTFLIGHT_RUN_PREFIX + " legacy checks`.",
            "unchanged suffix",
        ]
    )
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize(
    ("job_id", "evening"),
    [
        (contract.MORNING_JOB_ID, False),
        (contract.EVENING_JOB_ID, True),
    ],
)
def test_transform_routes_each_live_producer_through_skeleton_and_land(job_id, evening):
    before = legacy_prompt(evening=evening)

    after = contract.transform_prompt(job_id, before)

    assert contract.writer_contract_errors(job_id, after) == []
    assert "unchanged prefix" in after and "unchanged suffix" in after
    assert after.endswith("\n")
    assert before != after


def test_transform_fails_closed_when_the_live_prompt_shape_moved():
    before = legacy_prompt(evening=False).replace(contract.VALIDATE_PREFIX, "9. Moved")

    with pytest.raises(contract.ProducerPromptError, match="expected exactly one"):
        contract.transform_prompt(contract.MORNING_JOB_ID, before)


def test_a_marker_does_not_hide_a_remaining_direct_write_instruction():
    prompt = legacy_prompt(evening=False) + contract.writer_contract(
        contract.PROMPT_SPECS[contract.MORNING_JOB_ID]
    )

    errors = contract.writer_contract_errors(contract.MORNING_JOB_ID, prompt)

    assert any("legacy direct-write instruction remains" in error for error in errors)


def test_cli_reports_an_output_write_failure(tmp_path, capsys):
    source = tmp_path / "morning.before.txt"
    source.write_text(legacy_prompt(evening=False))

    with mock.patch.object(contract.Path, "write_text", side_effect=OSError("disk full")):
        status = contract.main(
            [
                "--job-id",
                contract.MORNING_JOB_ID,
                "--input",
                str(source),
                "--output",
                str(tmp_path / "morning.after.txt"),
            ]
        )

    assert status == 1
    assert "disk full" in capsys.readouterr().err


def test_cli_check_verifies_without_echoing_the_prompt(tmp_path, capsys):
    source = tmp_path / "morning.after.txt"
    source.write_text(
        contract.transform_prompt(
            contract.MORNING_JOB_ID, legacy_prompt(evening=False)
        )
    )

    status = contract.main(
        ["--job-id", contract.MORNING_JOB_ID, "--input", str(source), "--check"]
    )

    assert status == 0
    assert capsys.readouterr().out == ""
