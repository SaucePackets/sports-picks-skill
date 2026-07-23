#!/usr/bin/env python3
from __future__ import annotations
import json, subprocess, sys
from pathlib import Path
HERMES='/home/clawdbot/.local/bin/hermes'
PICKS=Path('/home/clawdbot/notes/Sports/picks/picks.json')
ROOT=Path('/home/clawdbot/projects/sports-picks-skill')

def main():
    # Trigger off the picks ledger itself, not the denormalized record.json
    # counters (which have gone stale and silently disabled settlement).
    try:
        data=json.loads(PICKS.read_text())
        picks=data.get('picks',[])
    except Exception as exc:
        print(f'Postgame gate ERROR: invalid picks.json: {exc}'); return 1
    open_picks=[p for p in picks if isinstance(p,dict) and p.get('status') in ('active','pending')]
    if not open_picks: return 0
    ids=', '.join(str(p.get('pick_id') or '?') for p in open_picks)
    prompt=f'''You are Vig running settlement and reflection only because the canonical picks ledger has {len(open_picks)} active or pending official wagers: {ids}.

Read /home/clawdbot/notes/Sports/picks/picks.json (canonical ledger) and /home/clawdbot/notes/Sports/picks/record.json. Settle only receipt-backed supported-venue or historically documented official wagers whose events are final. Verify official result and score. Never create or submit any order, and never restore Polymarket CLOB execution.

Update canonical records atomically — including recomputing record.json counters from picks.json statuses so they match — then return a concise result plus one process lesson only when evidence supports it. If no event is final, return [SILENT].'''
    cmd=[HERMES,'--profile','vig','--skills','betting-operations,sports-data-apis','chat','-q',prompt,'-t','terminal,file,web,skills','--quiet']
    try:
        proc=subprocess.run(cmd,cwd=ROOT,text=True,capture_output=True,timeout=1800)
    except subprocess.TimeoutExpired:
        print('Postgame review failed: child settlement agent timed out after 1800s'); return 1
    if proc.returncode:
        print(f'Postgame review failed (exit {proc.returncode}):\n{(proc.stderr or proc.stdout).strip()[:3000]}'); return proc.returncode
    out=proc.stdout.strip()
    if out and out!='[SILENT]': print(out)
    return 0
if __name__=='__main__': sys.exit(main())
