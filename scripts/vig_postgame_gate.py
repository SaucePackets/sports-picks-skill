#!/usr/bin/env python3
from __future__ import annotations
import json, subprocess, sys
from pathlib import Path
HERMES='/home/clawdbot/.local/bin/hermes'
PICKS=Path('/home/clawdbot/notes/Sports/picks/picks.json')
ROOT=Path('/home/clawdbot/projects/sports-picks-skill')

def _num(x):
    try: return float(x)
    except (TypeError, ValueError): return None

def marginal_cohort_stats(picks):
    """Fee-fix probation tracker (2026-08-03): the phantom 2.4% fee removal unlocked a
    tier of marginal-edge picks (true edge = win_prob - fill in the 2.0-4.4% band) that
    were previously hard-passed. Report that cohort's record + per-unit ROI separately
    from the strong (>=4.4%) cohort so the loosening proves or kills itself with data."""
    buckets={'marginal (edge 2.0-4.4%)':[], 'strong (edge >=4.4%)':[]}
    for p in picks:
        if p.get('status')!='settled' or p.get('result') not in ('win','loss'): continue
        wp=_num(p.get('win_probability'))
        ask=_num(p.get('entry_price')) or _num(p.get('polymarket_ask')) or _num(p.get('ask_at_recheck'))
        if wp is None or ask is None or not (0<ask<1): continue
        edge=wp-ask
        if edge<0.02: continue
        pnl=(1-ask)/ask if p['result']=='win' else -1.0
        buckets['marginal (edge 2.0-4.4%)' if edge<0.044 else 'strong (edge >=4.4%)'].append((p['result'],pnl))
    lines=[]
    for key,rows in buckets.items():
        if not rows: lines.append(f'  {key}: no settled picks yet'); continue
        w=sum(1 for r,_ in rows if r=='win'); n=len(rows); roi=sum(x for _,x in rows)/n
        lines.append(f'  {key}: {w}-{n-w} ({100*w//n}% win), per-unit ROI {roi:+.1%} over {n} settled')
    return '\n'.join(lines)

def small_stake_cohort_stats(picks):
    """Probation tracker (2026-08-04) for the two gate changes that ADD bets:
    - small-stake tier: below-Medium +EV picks (confidence 'small') bet at $9 instead of
      being hard-passed by the Medium win-probability sizing floor.
    - two-sided pricing: dog-side picks (entry_price < 0.50) surfaced by pricing BOTH
      teams every game instead of anchoring the handicap on the favorite.
    Each cohort's settled record + per-unit ROI is reported so the loosening proves or
    kills itself; the settlement prompt recommends reverting a cohort that runs negative."""
    def bucket(pred):
        rows=[]
        for p in picks:
            if p.get('status')!='settled' or p.get('result') not in ('win','loss'): continue
            ask=_num(p.get('entry_price')) or _num(p.get('polymarket_ask')) or _num(p.get('ask_at_recheck'))
            if ask is None or not (0<ask<1): continue
            if not pred(p,ask): continue
            pnl=(1-ask)/ask if p['result']=='win' else -1.0
            rows.append((p['result'],pnl))
        return rows
    def fmt(rows):
        if not rows: return 'no settled picks yet'
        w=sum(1 for r,_ in rows if r=='win'); n=len(rows); roi=sum(x for _,x in rows)/n
        return f'{w}-{n-w} ({100*w//n}% win), per-unit ROI {roi:+.1%} over {n} settled'
    small=bucket(lambda p,ask: str(p.get('confidence') or '').strip().lower()=='small')
    dog=bucket(lambda p,ask: ask<0.5)
    return '\n'.join([f'  small-stake tier (confidence=small): {fmt(small)}',
                      f'  dog-side picks (entry_price < 0.50): {fmt(dog)}'])

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
    cohort_section = marginal_cohort_stats(picks)
    small_cohort_section = small_stake_cohort_stats(picks)
    recon_section = ''
    if recon_gap:
        recon_section = (
            '\n\nRECEIPT AUDIT DISCREPANCIES (fix these first — every filled receipt must have a ledger row; '
            'backfill missing rows from the execution schedule + receipt before settling):\n'
            + recon.stdout.strip()[:2000]
        )
    prompt=f'''You are Vig running settlement and reflection because the canonical picks ledger has {len(open_picks)} active or pending official wagers: {ids}.{recon_section}

Read /home/clawdbot/notes/Sports/picks/picks.json (canonical ledger) and /home/clawdbot/notes/Sports/picks/record.json. Settle only receipt-backed supported-venue or historically documented official wagers whose events are final. Get final scores DETERMINISTICALLY via the mlb_final_scores MCP tool (mcp-sports-data) for the game date — do NOT curl or web-search for scores. Verify official result and score from that tool. Never create or submit any order, and never restore Polymarket CLOB execution.

When settling, copy win_probability/dk_fair_prob/net_edge AND the price trail — slate_ask (the 10:30 polymarket_ask), captured_polymarket_ask or approved_polymarket_ask (the pre-pitch recheck price, store as ask_at_recheck) — from the schedule candidate into the ledger row when present; entry_price is the fill. This is the CLV trail: slate -> recheck -> fill. Update canonical records atomically — recomputing record.json counters from picks.json statuses so they match. When citing the record anywhere (reflection, INDEX, Telegram), recompute it from picks.json only and present it with its Wilson 95% CI on win rate (~32 bets is small; never present streaks or day-level P&L as signal). Loss reflections must answer "what stated probability did we assign, and would we assign it again?" — "variance" is only an acceptable answer when the pre-game probability was defensible.

SURFACE THE REFLECTION IN TELEGRAM: your final response must give each settled pick a one-line reflection takeaway directly under its result line, not bury it in the vault. For a LOSS: the single most important "what changes going forward" lesson, plus whether it was a bad read or a bad result. For a WIN: whether the edge actually held or variance carried it. Keep it to one line per pick — the full postmortem stays in REFLECTIONS.md. If a loss taught a repeatable, durable rule, also say one line: "Promoted to data rule: <name>" so Jerry sees the gate got tightened.

MARGINAL-EDGE COHORT (fee-fix probation, started 2026-08-03): the phantom 2.4% Polymarket fee was removed, unlocking picks whose true edge (win_probability - fill) sits in the 2.0-4.4% band — previously hard-passed. Track them as a cohort so the loosening proves or kills itself. Current standing computed from picks.json:
{cohort_section}
In your reflection, report the marginal cohort's running record + per-unit ROI on its own line (label it "Marginal-edge cohort (fee-fix probation)"). If that cohort reaches >=15 settled bets with negative per-unit ROI, explicitly recommend tightening the net-edge floor back toward 0.025-0.030 and flag it as "Promoted to data rule: raise net-edge floor". If it's positive over >=15 bets, say the loosening is validated.

SMALL-STAKE / TWO-SIDED COHORTS (probation, started 2026-08-04): two gate changes now add bets — a small-stake tier ($9) for +EV picks below the Medium win-probability sizing floor, and two-sided pricing that lets the dog side qualify on the 2% net-edge floor instead of anchoring on the favorite. Track each so it proves or kills itself. Current standing computed from picks.json:
{small_cohort_section}
In your reflection, report BOTH lines (label them "Small-stake tier (probation)" and "Dog-side picks (two-sided probation)"). For EITHER cohort, once it reaches >=15 settled bets with negative per-unit ROI, explicitly recommend disabling that change — for the small-stake tier flag "Promoted to data rule: retire small-stake tier"; for dog-side flag "Promoted to data rule: re-anchor to favorite-only pricing". If a cohort is positive over >=15 bets, say that change is validated. Below 15 settled, report the standing and say "sample still building".

If no event is final and no audit discrepancy exists, return [SILENT].'''
    cmd=[HERMES,'--profile','vig','--skills','betting-operations,sports-data-apis','chat','-q',prompt,'-t','terminal,file,web,skills,sports-data','--quiet']
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
