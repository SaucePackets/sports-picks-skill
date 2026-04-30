# Dev Notes

## 2026-04-30 — Optional database-backed picks section

- Added a separate `Optional Database-Backed Pick Storage` section to `references/process.md`.
- Flat-file `.picks/INDEX.md` remains the portable reconciliation source unless a runtime explicitly names a database as canonical.
- Database users should store full cards/results/reflections in a picks table/object store, not generic memory.
- Added provenance expectations: `source_agent`/`persona_id` on creation and `updated_by_agent` on settlement.
- Added record-integrity rule: list filters and record filters must match, especially for `source_agent`, or the `/picks` cockpit lies.

## 2026-04-29 — Runtime extraction

- Kept the Runtime Lock Gate top-level in `SKILL.md`; it is identity, not detail.
- Slimmed `SKILL.md` from the full monolith into the front door: when to use, hard gate, verification rules, required reference load order, and default output rules.
- Added `skills/sports-picks/references/runtime.md` as the working clipboard: runtime order, final pass/fail gate, gate checklist, output templates, pass rules, and props rule.
- Added `skills/sports-picks/references/process.md` for settlement, postgame reflections, result updates, and recurring pattern maintenance.
- Medium confidence still means all hard gates passed but edge is not elite; it cannot salvage a failed starter, bullpen, cold-fade, price, or winner-conviction gate.
- Shared repo, Hermes installed runtime copy, and OpenClaw live runtime copy were updated together so current-agent behavior matches repo truth.

## 2026-04-28 — Official gate cleanup

- Winner conviction remains first; price is a veto/filter, not the reason to create a pick.
- MLB official-pick gate now explicitly includes opposing-starter shutdown risk.
- Two-way starter enforcement is required before locks: answer what happens if their starter is good and what happens if our starter struggles.
- Pregame scratches are auditable: if already logged, mark `Result` as `Scratched`; otherwise do not add the row.
- Props are secondary to the main game-picks workflow and require verified line/price before any official recommendation.

## 2026-04-28 — Runtime data quirks folded back

- ESPN summary `boxscore.players[].statistics[]` may have baseball pitching rows with `name: None`; identify pitching rows by `labels[0] == "IP"`.
- ESPN summary `header` may not expose a top-level `name`; use scoreboard event name or `header.competitions[0].competitors` for matchup labels.
- If `wttr.in` hangs or gets blocked during MLB weather checks, switch to Open-Meteo JSON with venue/city coordinates instead of retrying the same curl path.
- Runtime skill copy was synced after merging repo gate rules with the runtime-only MLB data notes.

## Runtime sync note

Hermes installed copy was updated alongside the shared repo copy so current-agent behavior matches repo truth.
