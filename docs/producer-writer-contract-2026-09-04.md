# Route both MLB slate producers through the writer

## Scope and boundary

The repository already had a writer that derives `slate_denominator` from the
Stage 2 scan, requires one validated `game_reads` row per scan game, and refuses
an incomplete draft before replacing the schedule. The live morning and evening
cron prompts still instructed the agent to write
`.picks/execute/YYYY-MM-DD-schedule.json` directly, and
`mlb_slate_writer.py` was not in `PROFILE_MANIFEST`. The 2026-09-04 morning rerun
therefore reproduced the original bare schedule even though the downstream gate
and receipt correctly called it `recorder_failed`.

This change closes the supported producer route without changing a selection
threshold, review rule, execution rule, or betting decision:

1. `scripts/deploy-runtime.sh` installs `mlb_slate_writer.py` into the Vig
   profile alongside all of its sibling imports.
2. `scripts/mlb_producer_prompt_contract.py` replaces the direct-write
   instructions in both stored producer prompts with a strict Stage 2 ->
   `--skeleton` -> fill draft -> `--land` sequence.
3. A refused landing is a terminal producer failure. The prompt may not fall
   back to a direct schedule write.
4. The evening producer may carry forward producer-owned morning entries, but
   it may not strip reviewer or executor state to make a landing pass. The
   writer refuses a schedule that has moved beyond production.

This is still a prompt-level producer contract. An agent can disobey a prompt;
the scheduled gate and receipt remain the independent downstream detection.
The narrower claim is that both supported stored producers now instruct the
only path that cannot land an incomplete record.

## Reviewed live inputs

Read-only snapshot from the Vig cron store on 2026-09-04. Counts distinguish
Unicode characters from encoded bytes; the sha256 is over UTF-8 bytes.

| job | state | characters | bytes | sha256 |
|---|---|---:|---:|---|
| `c9452052719c` morning | before | 15625 | 15689 | `c7adb97cf12a17080e3a83e5da537ab36c68a04a7ab56bea477a64b36c9f37e4` |
| `c9452052719c` morning | after | 16482 | 16544 | `5b477b8cc57eeb136748b4004d885d5780540ae9fefbba1f72e280496fa59b97` |
| `27087cc00dfa` evening | before | 17693 | 17765 | `decdf0e1e4b7f27ff34bc897f162b5ccfe75c8baac98754abb8efff64e2fcf4e` |
| `27087cc00dfa` evening | after | 18635 | 18703 | `9a5c2783a764e55b06cd64da44188552e17898443d79b69cb67b1d7a0c568671` |

The transform requires every legacy line exactly once and then verifies both
positive and negative contract text. A marker alone does not pass while a
direct-write instruction remains.

## Post-merge migration

Do not apply this before the implementing tip is reviewed, merged, and deployed
to the runtime checkout. Jerry owns the merge; Rebecca owns the live migration
and proof run.

```bash
set -euo pipefail

RUNTIME="$HOME/projects/sports-picks-runtime"
JOBS="$HOME/.hermes/profiles/vig/cron/jobs.json"
PROFILE_SCRIPTS="$HOME/.hermes/profiles/vig/scripts"
EXPECTED_SHA="<full-merged-main-sha>"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$HOME/backups/mlb-producer-writer-contract.$TS"
mkdir -p "$BACKUP"

test "$(git -C "$RUNTIME" rev-parse HEAD)" = "$EXPECTED_SHA"
test -z "$(git -C "$RUNTIME" status --porcelain --untracked-files=all)"
test "$(shasum -a 256 "$RUNTIME/scripts/mlb_slate_writer.py" | cut -d' ' -f1)" = \
     "$(shasum -a 256 "$PROFILE_SCRIPTS/mlb_slate_writer.py" | cut -d' ' -f1)"
cp -a "$JOBS" "$BACKUP/jobs.before.json"

python3 - "$JOBS" "$BACKUP" <<'PY'
import hashlib, json, pathlib, sys

jobs_path, backup = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
data = json.loads(jobs_path.read_text())
jobs = data["jobs"] if isinstance(data, dict) else data
expected = {
    "c9452052719c": (15625, 15689, "c7adb97cf12a17080e3a83e5da537ab36c68a04a7ab56bea477a64b36c9f37e4", "morning"),
    "27087cc00dfa": (17693, 17765, "decdf0e1e4b7f27ff34bc897f162b5ccfe75c8baac98754abb8efff64e2fcf4e", "evening"),
}
for job_id, (chars, size, digest, label) in expected.items():
    matches = [job for job in jobs if job.get("id") == job_id]
    assert len(matches) == 1, (job_id, len(matches))
    prompt = matches[0].get("prompt")
    assert isinstance(prompt, str), job_id
    raw = prompt.encode()
    assert (len(prompt), len(raw), hashlib.sha256(raw).hexdigest()) == (chars, size, digest), \
        f"{job_id} prompt moved since review - STOP"
    (backup / f"{label}.before.txt").write_bytes(raw)
PY

python3 "$RUNTIME/scripts/mlb_producer_prompt_contract.py" \
  --job-id c9452052719c \
  --input "$BACKUP/morning.before.txt" \
  --output "$BACKUP/morning.after.txt"
python3 "$RUNTIME/scripts/mlb_producer_prompt_contract.py" \
  --job-id 27087cc00dfa \
  --input "$BACKUP/evening.before.txt" \
  --output "$BACKUP/evening.after.txt"

python3 - "$BACKUP" <<'PY'
import hashlib, pathlib, sys

root = pathlib.Path(sys.argv[1])
expected = {
    "morning.after.txt": (16482, 16544, "5b477b8cc57eeb136748b4004d885d5780540ae9fefbba1f72e280496fa59b97"),
    "evening.after.txt": (18635, 18703, "9a5c2783a764e55b06cd64da44188552e17898443d79b69cb67b1d7a0c568671"),
}
for name, want in expected.items():
    raw = (root / name).read_bytes()
    text = raw.decode()
    got = (len(text), len(raw), hashlib.sha256(raw).hexdigest())
    assert got == want, (name, got, want)
PY

# Preserve trailing newlines. Plain "$(cat file)" would strip them and make
# the reviewed after-hash unreachable.
MORNING=$(cat "$BACKUP/morning.after.txt"; printf X); MORNING=${MORNING%X}
EVENING=$(cat "$BACKUP/evening.after.txt"; printf X); EVENING=${EVENING%X}
PATH="$HOME/.local/bin:$PATH" hermes -p vig cron edit c9452052719c --prompt "$MORNING"
PATH="$HOME/.local/bin:$PATH" hermes -p vig cron edit 27087cc00dfa --prompt "$EVENING"

python3 - "$JOBS" <<'PY'
import hashlib, json, sys

data = json.load(open(sys.argv[1]))
jobs = data["jobs"] if isinstance(data, dict) else data
expected = {
    "c9452052719c": (16482, 16544, "5b477b8cc57eeb136748b4004d885d5780540ae9fefbba1f72e280496fa59b97"),
    "27087cc00dfa": (18635, 18703, "9a5c2783a764e55b06cd64da44188552e17898443d79b69cb67b1d7a0c568671"),
}
for job_id, want in expected.items():
    prompt = next(job["prompt"] for job in jobs if job.get("id") == job_id)
    raw = prompt.encode()
    got = (len(prompt), len(raw), hashlib.sha256(raw).hexdigest())
    assert got == want, (job_id, got, want)
print("both producer prompts match the reviewed after-hashes")
PY
```

If either edit or read-back fails, restore both prompt fields from the backup,
using the same newline-preserving assignment pattern, and verify the two
before-hashes before doing anything else. Do not run a producer from a store
where only one of the two prompts migrated.

```bash
MORNING_BEFORE=$(cat "$BACKUP/morning.before.txt"; printf X)
MORNING_BEFORE=${MORNING_BEFORE%X}
EVENING_BEFORE=$(cat "$BACKUP/evening.before.txt"; printf X)
EVENING_BEFORE=${EVENING_BEFORE%X}
set +e
PATH="$HOME/.local/bin:$PATH" hermes -p vig cron edit c9452052719c \
  --prompt "$MORNING_BEFORE"
MORNING_RESTORE=$?
PATH="$HOME/.local/bin:$PATH" hermes -p vig cron edit 27087cc00dfa \
  --prompt "$EVENING_BEFORE"
EVENING_RESTORE=$?
set -e
test "$MORNING_RESTORE" -eq 0
test "$EVENING_RESTORE" -eq 0
python3 - "$JOBS" <<'PY'
import hashlib, json, sys

data = json.load(open(sys.argv[1]))
jobs = data["jobs"] if isinstance(data, dict) else data
expected = {
    "c9452052719c": "c7adb97cf12a17080e3a83e5da537ab36c68a04a7ab56bea477a64b36c9f37e4",
    "27087cc00dfa": "decdf0e1e4b7f27ff34bc897f162b5ccfe75c8baac98754abb8efff64e2fcf4e",
}
for job_id, want in expected.items():
    prompt = next(job["prompt"] for job in jobs if job.get("id") == job_id)
    assert hashlib.sha256(prompt.encode()).hexdigest() == want, job_id
print("both producer prompts match the reviewed before-hashes")
PY
```

The final read-back is the rollback receipt: both stored prompt fields must
match the reviewed before-hashes.

## Proof run after migration

1. Run `scripts/vig_runtime_verify.py --expect-sha <full-merged-main-sha>` and
   record all findings. The known cron-owner failures are separate; the
   `profile-scripts` check must show the complete manifest including the writer.
2. Run the morning producer, never the execution poller.
3. Read the landed schedule back and require `game_reads`,
   `slate_denominator.games`, and `slate_denominator.scan_sha256`. The read and
   denominator counts must equal the Stage 2 scan count.
4. Run `mlb_slate_receipt.py --write`; require `complete` or `honest_zero`, an
   empty `recorder_errors`, and an explicit `policy_status`.
5. Run the 15-minute review gate and confirm it rewrites the same receipt. Do
   not invoke the execution poller until the recording receipt is healthy.
