# Pinned-source offline verification

Run this command to verify the retained bulk starter evidence against the
repository-owned [provenance checkpoint](mlb-provenance-checkpoint.json):

```sh
python3 scripts/mlb_provenance_checkpoint.py \
  --bundle "$BUZZ_WORKSPACE/RESEARCH/MLB_CHRONOLOGICAL_ADMISSION_2026_09_07/ACQUISITION" \
  --snapshot-dir "$BUZZ_WORKSPACE/RESEARCH/MLB_BULK_STARTER_SNAPSHOTS_2026_09_07/ACQUISITION"
```

The command automatically compares the source manifest, snapshot manifest,
plan, contract, acquisition implementation identity and replay adapter hashes.
It then revalidates all retained bodies and receipts through offline replay and
compares the full encoded report byte count and SHA-256. Success exits 0 with
`pinned_replay_verified: true`; missing inputs, corrupt bodies, replaced sources,
adapter drift and output mismatch exit 2 with verification false. There is no
acquisition or caller-selected checkpoint option. The trust anchor is the reviewed
repository checkout, not a checkpoint supplied alongside the input bundle.

This successor pins a fresh bulk replay at merged main `bbee9b7` (which includes
census PR #96). Source, snapshot manifest, plan and acquisition pins are copied
unchanged from `mlb-bulk-starter-snapshots.json`. The bulk admission adapter changed
in the census lane, so the full replay's adapter field and consequently its hash
are refreshed explicitly here. The original checkpoint and closed census files
are untouched; summary counts match the predecessor. No source data was acquired.

The existing `mlb_bulk_starter_snapshots.py` remains the general integrity-only
acquisition/replay tool. Its successful exit does not certify these repository
pins. Use this verification command for checkpoint acceptance. The appearance
census is a separate report with its existing checkpoint and baseline check; this
command does not claim to verify the census output or recover additional rows.

The adversarial test replaces every source body, all source/snapshot receipts,
both manifests and the plan coherently. General replay succeeds with identical
summary counts; pinned verification rejects the replacement. A supplied alternate
checkpoint cannot override the repository anchor. Separate tests cover snapshot
resealing, body corruption under unchanged manifest pins, each pin, altered replay
output, missing/malformed checkpoints and CLI refusal. Synthetic tests forbid
network access.

Hashes establish equality to the committed retained evidence, not provider
authenticity or independently proven historical availability. This adds no feature
rows, fitting, scoring, eligibility, historical performance, runtime or deployment.
Pre-manifest malformed-receipt call-site coverage remains a separately tracked
follow-up and is not claimed by this lane.
