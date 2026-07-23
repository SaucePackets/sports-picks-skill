#!/usr/bin/env python3
"""Deterministic monthly calibration & performance report from the picks ledger.

No LLM. Reads picks.json, prints: record with Wilson CI, ROI, commission drag,
per-tier and per-price-band results, stated-probability calibration buckets
(once win_probability fields exist), and slippage (fill vs slate ask).
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path

PICKS = Path("/home/clawdbot/notes/Sports/picks/picks.json")


def wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    phat = wins / n
    denom = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    margin = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def fmt_pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def main() -> int:
    picks = json.loads(PICKS.read_text()).get("picks", [])
    settled = [p for p in picks if p.get("status") == "settled" and p.get("result") in ("win", "loss")]
    wins = sum(1 for p in settled if p["result"] == "win")
    n = len(settled)
    staked = sum(float(p.get("entry_notional") or p.get("unit_size") or 0) for p in settled)
    pnl = sum(float(p.get("pnl") or 0) for p in settled)
    commission = sum(float(p.get("commission") or 0) for p in settled)
    lo, hi = wilson_ci(wins, n)

    print(f"# Vig calibration report — {n} settled picks")
    print(f"Record: {wins}-{n - wins} | win rate {fmt_pct(wins / n if n else 0)} "
          f"(95% CI {fmt_pct(lo)}–{fmt_pct(hi)})")
    print(f"Staked ${staked:.2f} | P&L ${pnl:+.2f} | ROI {fmt_pct(pnl / staked if staked else 0)} "
          f"| commission drag {fmt_pct(commission / staked if staked else 0)} of stakes")
    breakeven = 0.545  # rough avg ask + fees
    print(f"NOTE: with n={n}, a CI spanning {fmt_pct(lo)}–{fmt_pct(hi)} cannot distinguish "
          f"this from break-even (~{fmt_pct(breakeven)} needed at typical prices). "
          "Treat ROI as unproven until the CI lower bound clears break-even.")

    bands = defaultdict(lambda: [0, 0, 0.0])
    for p in settled:
        price = p.get("entry_price")
        band = ("no-price" if price is None else
                "<0.50" if price < 0.50 else "0.50-0.55" if price < 0.55 else ">=0.55")
        bands[band][0] += 1 if p["result"] == "win" else 0
        bands[band][1] += 1
        bands[band][2] += float(p.get("pnl") or 0)
    print("\n## By entry price")
    for band, (w, tot, bpnl) in sorted(bands.items()):
        print(f"- {band}: {w}-{tot - w}, pnl ${bpnl:+.2f}")

    tiers = defaultdict(lambda: [0, 0, 0.0])
    for p in settled:
        tier = "High($30)" if float(p.get("unit_size") or 0) >= 30 else "Medium"
        tiers[tier][0] += 1 if p["result"] == "win" else 0
        tiers[tier][1] += 1
        tiers[tier][2] += float(p.get("pnl") or 0)
    print("\n## By tier")
    for tier, (w, tot, tpnl) in sorted(tiers.items()):
        print(f"- {tier}: {w}-{tot - w}, pnl ${tpnl:+.2f}")

    with_prob = [p for p in settled if isinstance(p.get("win_probability"), (int, float))]
    print(f"\n## Stated-probability calibration ({len(with_prob)} picks carry win_probability)")
    if with_prob:
        buckets = defaultdict(lambda: [0, 0, 0.0])
        for p in with_prob:
            wp = float(p["win_probability"])
            key = f"{math.floor(wp * 20) / 20:.2f}"
            buckets[key][0] += 1 if p["result"] == "win" else 0
            buckets[key][1] += 1
            buckets[key][2] += wp
        for key, (w, tot, sum_wp) in sorted(buckets.items()):
            print(f"- stated ~{key}: actual {w}/{tot} = {fmt_pct(w / tot)} (avg stated {fmt_pct(sum_wp / tot)})")
    else:
        print("- none yet; calibration becomes meaningful once cards carry win_probability")

    slips = []
    for p in settled:
        # slate ask lives in schedule files; approximate with thesis-recorded ask when present
        entry = p.get("entry_price")
        ask = p.get("slate_ask") or p.get("polymarket_ask")
        if isinstance(entry, (int, float)) and isinstance(ask, (int, float)):
            slips.append(entry - ask)
    if slips:
        print(f"\n## Slippage (fill - slate ask): avg {sum(slips) / len(slips):+.3f}, "
              f"worst {max(slips):+.3f} over {len(slips)} picks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
