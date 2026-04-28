# Dev Notes

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
