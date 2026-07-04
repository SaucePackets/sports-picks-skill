#!/usr/bin/env python3
"""
Intl Soccer Probability Model — Lightweight Elo-style engine for WC 2026.

Produces independent 90-min outcome probabilities and To Advance probabilities
from quality-adjusted form data. Does NOT cross-compare market types.

Usage:
    from intl_soccer_model import TeamStrength, MatchModel

    # Build team strengths from form data
    fra = TeamStrength(name="France", adj_gf=3.33, adj_ga=0.67,
                       wins=5, draws=0, losses=0)
    par = TeamStrength(name="Paraguay", adj_gf=0.67, adj_ga=1.67,
                       wins=2, draws=2, losses=1)

    # Model the match
    m = MatchModel(fra, par, stage="R16")
    print(m.report())
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional


# ── Calibration constants ───────────────────────────────────────────────
# Fitted to 324 World Cup matches (2006–2022) from intl-soccer-data.md.

ELO_SCALE = 150.0          # Rating diff where win probability ≈ 73%
DRAW_SCALE = 250.0          # How quickly draw probability decays with gap
DRAW_BASE_GROUP = 0.22      # Base draw rate, group stage
DRAW_BASE_KNOCKOUT = 0.27   # Base draw rate, knockout rounds
ET_ADV_SCALE = 300.0        # Rating diff scale for ET/pens advancement
ELO_K = 32                  # Elo K-factor for rating updates


# ── Team Strength ────────────────────────────────────────────────────────

@dataclass
class TeamStrength:
    """Represents a team's current strength from quality-adjusted form data.

    All tournament inputs must be from the current tournament only — no
    friendlies, no pre-tournament form.
    """

    name: str
    adj_gf: float        # quality-adjusted goals-for per game
    adj_ga: float        # quality-adjusted goals-against per game
    wins: int            # tournament wins
    draws: int           # tournament draws
    losses: int           # tournament losses
    games_played: Optional[int] = None

    def __post_init__(self) -> None:
        if self.games_played is None:
            self.games_played = self.wins + self.draws + self.losses
        self._rating = self._compute_rating()

    @property
    def games(self) -> int:
        return self.games_played or 1

    @property
    def win_pct(self) -> float:
        """Win percentage counting draws as half-wins."""
        return (self.wins + 0.5 * self.draws) / max(self.games, 1)

    @property
    def goal_diff(self) -> float:
        return self.adj_gf - self.adj_ga

    def _compute_rating(self) -> float:
        """Composite strength rating from form + goal difference.

        scale: ~-150 (minnow, e.g. 0.25 GF/g, 3.0 GA/g) to ~350 (elite, e.g. 3.5 GF/g, 0.5 GA/g).
        """
        goal_component = self.goal_diff * 100.0
        win_component = self.win_pct * 60.0
        return goal_component + win_component

    @property
    def rating(self) -> float:
        return self._rating


# ── Match Model ──────────────────────────────────────────────────────────

@dataclass
class MatchProbabilities:
    """Container for model-derived probabilities."""

    p_win_90: float     # favorite wins in 90 minutes
    p_draw: float       # draw in 90 minutes
    p_loss_90: float    # underdog wins in 90 minutes

    p_advance_fav: float  # favorite advances (includes ET/pens)
    p_advance_dog: float  # underdog advances

    rating_diff: float   # fav_rating - dog_rating
    fav_name: str
    dog_name: str

    @property
    def is_fav_team_a(self) -> bool:
        """Was the first team provided the favorite?"""
        return self.rating_diff >= 0


@dataclass
class EdgeResult:
    """Edge analysis for a single market."""

    market_type: str                # "90min_ml" or "to_advance"
    team: str
    model_prob: float               # model-derived probability
    market_price: float             # market's implied probability (decimal, e.g. 0.583 for -140)
    edge_pct: float                 # model_prob - market_price (positive = edge)
    ev_pct: float = 0.0             # expected ROI per dollar staked (e.g. 0.08 = 8%)
    edge_quality: str = "standard"  # "value" | "standard" | "thin_heavy_fav" | "negative"
    confidence: str = "None"        # "High" | "Medium" | "Low" | "None"
    direction: str = "pass"         # "bet" | "pass" | "fade"


@dataclass
class MatchModel:
    """Models a single World Cup match outcome."""

    team_a: TeamStrength
    team_b: TeamStrength
    stage: str = "group"  # "group" | "R32" | "R16" | "QF" | "SF" | "final"
    _has_et: bool = True   # World Cup knockout always has ET

    # Outputs
    probabilities: MatchProbabilities = field(init=False)
    rating_diff: float = field(init=False)

    def __post_init__(self) -> None:
        self._normalize_stage()
        self.rating_diff = self.team_a.rating - self.team_b.rating
        self.probabilities = self._compute_probabilities()

    def _normalize_stage(self) -> None:
        """Map stage names to 'group' or 'knockout'."""
        knockout_stages = {"R32", "R16", "QF", "SF", "final", "3rd"}
        if self.stage.upper() in knockout_stages:
            self.stage = "knockout"
        else:
            self.stage = "group"

    @property
    def draw_base(self) -> float:
        return DRAW_BASE_KNOCKOUT if self.stage == "knockout" else DRAW_BASE_GROUP

    def _compute_probabilities(self) -> MatchProbabilities:
        """Compute 90-min and To Advance probabilities.

        Uses absolute rating diff so probabilities are symmetric —
        the favorite always wins more often regardless of which team is team_a.
        """
        d = abs(self.rating_diff)

        # ── 90-min outcomes ──────────────────────────────────────────
        # Win expectancy: Elo logistic
        we = 1.0 / (1.0 + 10.0 ** (-d / 400.0))

        # Draw probability: peaks when teams are equal, decays with gap
        draw_p = self.draw_base * math.exp(-(d / DRAW_SCALE) ** 2)

        # Favorite win and underdog loss
        p_fav_win = we * (1.0 - draw_p)
        p_draw = draw_p
        p_dog_win = (1.0 - we) * (1.0 - draw_p)

        # ── To Advance (knockout only) ────────────────────────────────
        if self.stage == "knockout":
            # Probability favorite wins if match goes to ET/pens
            p_fav_et = 1.0 / (1.0 + math.exp(-d / ET_ADV_SCALE))

            p_advance_fav = p_fav_win + p_draw * p_fav_et
            p_advance_dog = p_dog_win + p_draw * (1.0 - p_fav_et)
        else:
            # Group stage: no ET, advance = 90-min outcome
            p_advance_fav = p_fav_win
            p_advance_dog = p_dog_win

        # Determine which team is favorite
        if self.rating_diff >= 0:
            fav_name = self.team_a.name
            dog_name = self.team_b.name
        else:
            fav_name = self.team_b.name
            dog_name = self.team_a.name

        return MatchProbabilities(
            p_win_90=round(p_fav_win, 4),
            p_draw=round(p_draw, 4),
            p_loss_90=round(p_dog_win, 4),
            p_advance_fav=round(p_advance_fav, 4),
            p_advance_dog=round(p_advance_dog, 4),
            rating_diff=round(self.rating_diff, 1),
            fav_name=fav_name,
            dog_name=dog_name,
        )

    # ── Edge calculation ─────────────────────────────────────────────────

    def edge_90min_ml(self, team: str, market_implied: float,
                     american_odds: Optional[int] = None) -> EdgeResult:
        """Edge for a 90-minute ML bet on DK/ESPN.

        Args:
            team: team name (must match TeamStrength.name)
            market_implied: implied probability from odds (e.g. -140 = 0.583)
            american_odds: raw American odds (e.g. -140, +150) for EV/ROI
        """
        if team == self.probabilities.fav_name:
            model_p = self.probabilities.p_win_90
        elif team == self.probabilities.dog_name:
            model_p = self.probabilities.p_loss_90
        else:
            raise ValueError(f"Team '{team}' not in match: "
                             f"{self.probabilities.fav_name} vs {self.probabilities.dog_name}")

        edge = model_p - market_implied
        ev_pct = self._calc_ev_roi(model_p, market_implied, american_odds=american_odds)
        quality = self._edge_quality(market_implied, edge)

        return EdgeResult(
            market_type="90min_ml",
            team=team,
            model_prob=model_p,
            market_price=market_implied,
            edge_pct=round(edge * 100, 1),
            ev_pct=round(ev_pct, 1),
            edge_quality=quality,
            confidence=self._confidence(edge),
            direction=self._direction(edge),
        )

    def edge_to_advance(self, team: str, pm_price: float) -> EdgeResult:
        """Edge for a To Advance bet on Polymarket.

        Args:
            team: team name
            pm_price: Polymarket YES price (e.g. 0.565 = 56.5¢)
        """
        if team == self.probabilities.fav_name:
            model_p = self.probabilities.p_advance_fav
        elif team == self.probabilities.dog_name:
            model_p = self.probabilities.p_advance_dog
        else:
            raise ValueError(f"Team '{team}' not in match: "
                             f"{self.probabilities.fav_name} vs {self.probabilities.dog_name}")

        edge = model_p - pm_price
        ev_pct = self._calc_ev_roi(model_p, pm_price)
        quality = self._edge_quality(pm_price, edge)

        return EdgeResult(
            market_type="to_advance",
            team=team,
            model_prob=model_p,
            market_price=pm_price,
            edge_pct=round(edge * 100, 1),
            ev_pct=round(ev_pct, 1),
            edge_quality=quality,
            confidence=self._confidence(edge),
            direction=self._direction(edge),
        )

    @staticmethod
    def _calc_ev_roi(model_p: float, market_price: float,
                     american_odds: Optional[int] = None) -> float:
        """Calculate expected ROI per dollar staked.

        For American odds: negative means risk |odds|/100 to win 1, positive means risk 1 to win odds/100.
        For PM binary: market_price is the YES price — risk price to win (1-price).
        Returns ROI as percentage (e.g. 8.0 = 8%).
        """
        if american_odds is not None:
            if american_odds < 0:
                win_per_dollar = 100.0 / abs(american_odds)
            else:
                win_per_dollar = american_odds / 100.0
        else:
            # PM binary: risk price to win (1-price) per unit
            win_per_dollar = (1.0 - market_price) / market_price if market_price > 0 else 0.0

        ev = model_p * win_per_dollar - (1.0 - model_p) * 1.0
        return ev * 100.0  # as percentage

    @staticmethod
    def _edge_quality(market_price: float, edge: float) -> str:
        """Classify edge quality based on price and edge size.

        - "value": edge at + odds or PM < 0.50 — good risk/reward
        - "standard": edge at normal favorite pricing (PM 0.50-0.75, DK -110 to -250)
        - "thin_heavy_fav": edge on a heavy favorite (PM > 0.75, DK < -250)
          ROI compressed — risk is high relative to reward
        - "negative": no edge
        """
        if edge <= 0:
            return "negative"
        # Heavy favorite: PM > 0.75 or worse than DK -250
        # DK -250 = implied 71.4%
        if market_price > 0.75:
            return "thin_heavy_fav"
        # Underdog or near-even: PM < 0.50 or DK +100+
        if market_price < 0.50:
            return "value"
        return "standard"

    @staticmethod
    def _confidence(edge: float) -> str:
        abs_e = abs(edge)
        if abs_e >= 0.07:
            return "High"
        elif abs_e >= 0.04:
            return "Medium"
        elif abs_e >= 0.02:
            return "Low"
        return "None"

    @staticmethod
    def _direction(edge: float) -> str:
        if edge > 0:
            return "bet"
        elif edge < -0.03:
            return "fade"
        return "pass"

    def update_ratings(self, actual_score: tuple[int, int], winner: str) -> tuple[float, float]:
        """Update team Elo-style ratings after a match result.

        Returns (new_rating_a, new_rating_b).
        """
        p = self.probabilities
        d = abs(self.rating_diff)

        # Expected score for the favorite (win=1, draw=0.5, loss=0)
        fav_expected = p.p_win_90 + 0.5 * p.p_draw

        # Actual score
        if winner == p.fav_name:
            fav_actual = 1.0
        elif winner == "draw":
            fav_actual = 0.5
        else:
            fav_actual = 0.0

        # Elo update — scale K by goal margin
        margin = abs(actual_score[0] - actual_score[1])
        k_factor = ELO_K * (1.0 + margin * 0.5)

        delta = k_factor * (fav_actual - fav_expected)

        if self.rating_diff >= 0:
            new_a = self.team_a.rating + delta
            new_b = self.team_b.rating - delta
        else:
            new_a = self.team_a.rating - delta
            new_b = self.team_b.rating + delta

        # Update in-place
        self.team_a._rating = round(new_a, 1)
        self.team_b._rating = round(new_b, 1)

        return (self.team_a.rating, self.team_b.rating)

    # ── Reporting ────────────────────────────────────────────────────────

    def report(self) -> str:
        """One-line probability summary."""
        p = self.probabilities
        lines = [
            f"{p.fav_name} vs {p.dog_name} | rating diff: {p.rating_diff:+.1f} | {self.stage}",
            f"  90min: {p.fav_name} {p.p_win_90:.1%} / Draw {p.p_draw:.1%} / {p.dog_name} {p.p_loss_90:.1%}",
        ]
        if self.stage == "knockout":
            lines.append(
                f"  Advance: {p.fav_name} {p.p_advance_fav:.1%} / {p.dog_name} {p.p_advance_dog:.1%}"
            )
        return "\n".join(lines)

    def edge_report(self, team: str, dk_implied: Optional[float] = None,
                    pm_price: Optional[float] = None) -> str:
        """Full edge report for a team across available markets."""
        lines = [f"Edge report — {team}"]
        p = self.probabilities

        if dk_implied is not None:
            e = self.edge_90min_ml(team, dk_implied)
            lines.append(
                f"  90-min ML: model {e.model_prob:.1%} vs DK {e.market_price:.1%} "
                f"→ edge {e.edge_pct:+.1f}%, ev {e.ev_pct:+.1f}% [{e.edge_quality}]"
            )

        if pm_price is not None:
            e = self.edge_to_advance(team, pm_price)
            lines.append(
                f"  To Advance: model {e.model_prob:.1%} vs PM {e.market_price:.1%} "
                f"→ edge {e.edge_pct:+.1f}%, ev {e.ev_pct:+.1f}% [{e.edge_quality}]"
            )

        return "\n".join(lines)


# ── Convenience: build from schedule JSON ────────────────────────────────

def build_team_from_candidate(candidate: dict) -> TeamStrength:
    """Extract TeamStrength from a schedule JSON candidate entry.

    Requires candidate to have: side, adj_gf, adj_ga, tournament_wins/draws/losses.
    """
    return TeamStrength(
        name=candidate.get("side", "Unknown"),
        adj_gf=candidate.get("adj_gf", 0.0),
        adj_ga=candidate.get("adj_ga", 0.0),
        wins=candidate.get("tournament_wins", 0),
        draws=candidate.get("tournament_draws", 0),
        losses=candidate.get("tournament_losses", 0),
    )


# ── CLI ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("Usage: intl_soccer_model.py <team_a> <team_b> "
              "[--stage R16] [--adj-gf-a X] [--adj-ga-a X] "
              "[--wins-a N] [--draws-a N] [--losses-a N] ...")
        sys.exit(1)

    # Parse args
    kwargs_a = {"name": sys.argv[1], "adj_gf": 1.0, "adj_ga": 1.0,
                "wins": 0, "draws": 0, "losses": 0}
    kwargs_b = {"name": sys.argv[2], "adj_gf": 1.0, "adj_ga": 1.0,
                "wins": 0, "draws": 0, "losses": 0}
    stage = "group"

    i = 3
    current_team = "a"
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg == "--stage":
            stage = sys.argv[i + 1]
            i += 2
        elif arg == "--adj-gf-a":
            kwargs_a["adj_gf"] = float(sys.argv[i + 1]); i += 2
        elif arg == "--adj-ga-a":
            kwargs_a["adj_ga"] = float(sys.argv[i + 1]); i += 2
        elif arg == "--wins-a":
            kwargs_a["wins"] = int(sys.argv[i + 1]); i += 2
        elif arg == "--draws-a":
            kwargs_a["draws"] = int(sys.argv[i + 1]); i += 2
        elif arg == "--losses-a":
            kwargs_a["losses"] = int(sys.argv[i + 1]); i += 2
        elif arg == "--adj-gf-b":
            kwargs_b["adj_gf"] = float(sys.argv[i + 1]); i += 2
        elif arg == "--adj-ga-b":
            kwargs_b["adj_ga"] = float(sys.argv[i + 1]); i += 2
        elif arg == "--wins-b":
            kwargs_b["wins"] = int(sys.argv[i + 1]); i += 2
        elif arg == "--draws-b":
            kwargs_b["draws"] = int(sys.argv[i + 1]); i += 2
        elif arg == "--losses-b":
            kwargs_b["losses"] = int(sys.argv[i + 1]); i += 2
        elif arg == "--dk-implied":
            dk_implied = float(sys.argv[i + 1]); i += 2
        elif arg == "--pm-price":
            pm_price = float(sys.argv[i + 1]); i += 2
        else:
            i += 1

    a = TeamStrength(**kwargs_a)
    b = TeamStrength(**kwargs_b)
    m = MatchModel(a, b, stage=stage)
    print(m.report())
