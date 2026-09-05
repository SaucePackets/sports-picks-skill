#!/usr/bin/env python3
"""Migrate and verify the stored MLB slate-producer prompts.

The morning and evening jobs are agent prompts stored outside this repository.
Repository code can deploy the writer without making either producer call it,
so this transformer is the reviewed, fail-closed migration for those two prompt
fields. It changes only the schedule-writing instructions; handicapping,
selection, review, and execution language are left byte-for-byte unchanged.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path


MORNING_JOB_ID = "c9452052719c"
EVENING_JOB_ID = "27087cc00dfa"
WRITER = "~/.hermes/profiles/vig/scripts/mlb_slate_writer.py"
RECEIPT = "~/.hermes/profiles/vig/scripts/mlb_slate_receipt.py"
SCHEDULE = ".picks/execute/YYYY-MM-DD-schedule.json"

SCAN_PREFIX = "2. The mcp-sports-data MCP tools"
WRITE_PREFIX = f"8. Write `{SCHEDULE}`"
VALIDATE_PREFIX = "9. Validate the finished schedule"
POSTFLIGHT_WRITE_PREFIX = "- Use `write_file` for the slate and latest-action"
POSTFLIGHT_DIRECT_WRITE = (
    "- Use `write_file` for the slate and latest-action and a valid JSON write "
    "for the schedule"
)
POSTFLIGHT_RUN_PREFIX = "- Immediately run: `test -s"
EVENING_MERGE_PREFIX = f"- Schedule file: MERGE into the existing `{SCHEDULE}`"


class ProducerPromptError(ValueError):
    pass


@dataclass(frozen=True)
class PromptSpec:
    job_id: str
    draft: str
    report: str
    evening: bool = False


PROMPT_SPECS = {
    MORNING_JOB_ID: PromptSpec(
        MORNING_JOB_ID,
        ".picks/tmp/YYYY-MM-DD-morning-slate-draft.json",
        ".picks/slate/YYYY-MM-DD.md",
    ),
    EVENING_JOB_ID: PromptSpec(
        EVENING_JOB_ID,
        ".picks/tmp/YYYY-MM-DD-evening-slate-draft.json",
        ".picks/slate/YYYY-MM-DD-evening.md",
        evening=True,
    ),
}


def _replace_one_line(prompt: str, prefix: str, replacement: str) -> str:
    lines = prompt.splitlines(keepends=True)
    matches = [index for index, line in enumerate(lines) if line.startswith(prefix)]
    if len(matches) != 1:
        raise ProducerPromptError(
            f"expected exactly one line starting {prefix!r}, found {len(matches)}"
        )
    index = matches[0]
    ending = "\n" if lines[index].endswith("\n") else ""
    lines[index] = replacement.rstrip("\n") + ending
    return "".join(lines)


def writer_contract(spec: PromptSpec) -> str:
    return "\n".join(
        [
            "   PRODUCER WRITER CONTRACT v1 (mandatory, not advisory):",
            f"   - Immediately create the producer draft with `python3 {WRITER} "
            f"--skeleton --day YYYY-MM-DD --out {spec.draft}`.",
            "   - Fill that draft only: keep its header fields, fill every existing "
            "game_reads stub, and add only producer-owned candidates and watchlist entries.",
            "   - Never add slate_denominator or scan_sha256 to the draft; the writer "
            "derives both from the exact Stage 2 scan bytes.",
            f"   - NEVER create, overwrite, merge, or edit `{SCHEDULE}` directly.",
            f"   - Land only with `python3 {WRITER} --land {spec.draft} "
            "--day YYYY-MM-DD` after every read is complete.",
            "   - A nonzero exit or a response with landed=false is a terminal run "
            "failure. Return the exact writer errors and stop; never report slate success.",
        ]
    )


def _task_write_line(spec: PromptSpec) -> str:
    return (
        f"8. Fill the writer-generated `{spec.draft}`. Do not create, overwrite, merge, "
        f"or edit `{SCHEDULE}` directly. Keep the skeleton header unchanged; fill every "
        "game_reads stub and add candidates or lineup_watchlist entries using the fields below:"
    )


def _task_land_line(spec: PromptSpec) -> str:
    return (
        f"9. Land the filled draft with `python3 {WRITER} --land {spec.draft} "
        "--day YYYY-MM-DD`. This is the only supported schedule write. If it exits "
        "nonzero or reports landed=false, return the exact writer errors and stop without "
        "reporting slate success."
    )


def _postflight_run_line(spec: PromptSpec) -> str:
    return (
        f"- Immediately run: `test -s {spec.report} && test -s {SCHEDULE} && "
        f"test -s .picks/latest-action.md && python3 {RECEIPT} --write "
        "--day YYYY-MM-DD`."
    )


def transform_prompt(job_id: str, prompt: str) -> str:
    try:
        spec = PROMPT_SPECS[job_id]
    except KeyError as exc:
        raise ProducerPromptError(f"unsupported producer job id: {job_id}") from exc

    transformed = _replace_one_line(
        prompt,
        SCAN_PREFIX,
        next(
            line.rstrip("\r\n")
            for line in prompt.splitlines(keepends=True)
            if line.startswith(SCAN_PREFIX)
        )
        + "\n"
        + writer_contract(spec),
    )
    transformed = _replace_one_line(transformed, WRITE_PREFIX, _task_write_line(spec))
    transformed = _replace_one_line(transformed, VALIDATE_PREFIX, _task_land_line(spec))
    transformed = _replace_one_line(
        transformed,
        POSTFLIGHT_WRITE_PREFIX,
        "- Use `write_file` for the slate and latest-action only. The schedule must "
        "already have been created by the writer's --land command; never write schedule "
        "JSON directly.",
    )
    transformed = _replace_one_line(
        transformed, POSTFLIGHT_RUN_PREFIX, _postflight_run_line(spec)
    )
    if spec.evening:
        transformed = _replace_one_line(
            transformed,
            EVENING_MERGE_PREFIX,
            f"- Schedule file: NEVER edit `{SCHEDULE}` directly. Build `{spec.draft}` "
            "from the fresh skeleton and carry forward producer-owned morning candidates "
            "and watchlist entries unchanged. Never strip or rewrite reviewer/executor "
            "state to make a landing pass; if the writer refuses the existing schedule, "
            "the evening run fails.",
        )

    errors = writer_contract_errors(job_id, transformed)
    if errors:
        raise ProducerPromptError(
            "transformed prompt failed its contract: " + "; ".join(errors)
        )
    return transformed


def writer_contract_errors(job_id: str, prompt: str) -> list[str]:
    try:
        spec = PROMPT_SPECS[job_id]
    except KeyError:
        return [f"unsupported producer job id: {job_id}"]
    required = (
        "PRODUCER WRITER CONTRACT v1",
        f"{WRITER} --skeleton --day YYYY-MM-DD --out {spec.draft}",
        f"{WRITER} --land {spec.draft} --day YYYY-MM-DD",
        f"NEVER create, overwrite, merge, or edit `{SCHEDULE}` directly",
        f"{RECEIPT} --write --day YYYY-MM-DD",
    )
    errors = [
        f"missing required producer contract text: {item}"
        for item in required
        if item not in prompt
    ]
    forbidden = [WRITE_PREFIX]
    if spec.evening:
        forbidden.append(EVENING_MERGE_PREFIX)
    for prefix in forbidden:
        if any(line.startswith(prefix) for line in prompt.splitlines()):
            errors.append(f"legacy direct-write instruction remains: {prefix}")
    if POSTFLIGHT_DIRECT_WRITE in prompt:
        errors.append(
            "legacy direct-write instruction remains: valid JSON write for the schedule"
        )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--job-id", required=True, choices=tuple(PROMPT_SPECS))
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify input without transforming or emitting it",
    )
    args = parser.parse_args(argv)

    try:
        prompt = args.input.read_text(encoding="utf-8")
        if args.check:
            errors = writer_contract_errors(args.job_id, prompt)
            if errors:
                raise ProducerPromptError("; ".join(errors))
            return 0
        else:
            output = transform_prompt(args.job_id, prompt)
    except (OSError, ProducerPromptError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        if args.output is None:
            print(output, end="")
        else:
            args.output.write_text(output, encoding="utf-8")
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
