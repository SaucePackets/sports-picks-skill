from __future__ import annotations

from pathlib import Path
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


def test_evening_contract_requires_stage2_preflight_before_writer_calls():
    before = legacy_prompt(evening=True)
    transformed = contract.transform_prompt(contract.EVENING_JOB_ID, before)

    preflight = contract.EVENING_PREFLIGHT_CONTRACT
    assert preflight in transformed
    assert transformed.index(preflight) < transformed.index("mlb_slate_writer.py")
    assert '--run-nonce "$run_nonce"' in transformed


def test_evening_contract_rejects_prompt_without_stage2_preflight():
    prompt = legacy_prompt(evening=True) + contract.writer_contract(
        contract.PROMPT_SPECS[contract.EVENING_JOB_ID]
    )

    errors = contract.writer_contract_errors(contract.EVENING_JOB_ID, prompt)

    assert any("Stage 2 preflight" in error for error in errors)


def test_evening_contract_rejects_nonce_less_writer_commands():
    prompt = contract.transform_prompt(contract.EVENING_JOB_ID, legacy_prompt(evening=True))
    prompt = prompt.replace(
        ' --run-nonce "$run_nonce" --out .picks/tmp/YYYY-MM-DD-evening-slate-draft.json',
        ' --out .picks/tmp/YYYY-MM-DD-evening-slate-draft.json',
    )
    prompt = prompt.replace(
        ' --day YYYY-MM-DD --run-nonce "$run_nonce"`',
        ' --day YYYY-MM-DD`',
    )

    errors = contract.writer_contract_errors(contract.EVENING_JOB_ID, prompt)

    assert any("nonce-bound skeleton" in error for error in errors)
    assert any("nonce-bound land" in error for error in errors)


def test_evening_contract_rejects_preflight_after_writer_invocation():
    transformed = contract.transform_prompt(contract.EVENING_JOB_ID, legacy_prompt(evening=True))
    preflight = contract.EVENING_PREFLIGHT_CONTRACT
    bad = transformed.replace(preflight, "", 1)
    first_writer = bad.index("mlb_slate_writer.py")
    first_writer_end = bad.index("`", first_writer) + 1
    bad = bad[:first_writer_end] + "\n" + preflight + bad[first_writer_end:]

    errors = contract.writer_contract_errors(contract.EVENING_JOB_ID, bad)

    assert any("precede every writer" in error for error in errors)


def test_evening_preflight_uses_the_writer_scan_artifact_convention():
    from scripts.mlb_stage2_scan import denominator_output_path

    day = "2026-09-09"
    expected = denominator_output_path(day, Path("/runtime"))
    assert str(expected) == "/runtime/.picks/tmp/stage2-2026-09-09.json"
    assert '--run-nonce "$run_nonce"' in contract.EVENING_PREFLIGHT_CONTRACT


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
