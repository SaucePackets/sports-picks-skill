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
    recon=subprocess.run(['python3', str(ROOT/'scripts/receipts_ledger_reconcile.py')],
                         text=True, capture_output=True, timeout=120)
    recon_gap = recon.returncode != 0
    if not open_picks and not recon_gap: return 0
    ids=', '.join(str(p.get('pick_id') or '?') for p in open_picks) or 'none'
    recon_section = ''
    if recon_gap:
        recon_section = (
            '\n\nRECEIPT AUDIT DISCREPANCIES (fix these first — every filled receipt must have a ledger row; '
            'backfill missing rows from the execution schedule + receipt before settling):\n'
            + recon.stdout.strip()[:2000]
        )
    prompt=f'''You are Vig running settlement and reflection because the canonical picks ledger has {len(open_picks)} active or pending official wagers: {ids}.{recon_section}

Read /home/clawdbot/notes/Sports/picks/picks.json (canonical ledger) and /home/clawdbot/notes/Sports/picks/record.json. Settle only receipt-backed supported-venue or historically documented official wagers whose events are final. Verify official result and score. Never create or submit any order, and never restore Polymarket CLOB execution.

When settling, copy win_probability/dk_fair_prob/net_edge from the schedule candidate into the ledger row when present. Update canonical records atomically — recomputing record.json counters from picks.json statuses so they match. When citing the record anywhere (reflection, INDEX, Telegram), recompute it from picks.json only and present it with its Wilson 95% CI on win rate (~32 bets is small; never present streaks or day-level P&L as signal). Loss reflections must answer "what stated probability did we assign, and would we assign it again?" — "variance" is only an acceptable answer when the pre-game probability was defensible. If no event is final and no audit discrepancy exists, return [SILENT].'''
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
