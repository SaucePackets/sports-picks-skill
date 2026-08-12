import importlib.util
import sys
import unittest
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "mlb_lineup_watchlist.py"
spec = importlib.util.spec_from_file_location("mlb_lineup_watchlist", SCRIPT_PATH)
assert spec is not None
mlb_lineup_watchlist = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules["mlb_lineup_watchlist"] = mlb_lineup_watchlist
spec.loader.exec_module(mlb_lineup_watchlist)

from mlb_baseball_evidence import valid_baseball_evidence  # noqa: E402


def enabled_starter_policy(min_conservative_edge=0.05):
    return mlb_lineup_watchlist.MlbSelectionPolicy(
        min_conservative_edge=min_conservative_edge,
        max_mlb_official_bets_per_day=2,
        starter_pending_promotions_enabled=True,
        max_small_bets_per_day_probation=1,
        policy_version="test",
        effective_at="2026-08-11T00:00:00Z",
    )


def prob_block(**overrides):
    """A self-consistent probability-components block (edge = cons - ask)."""
    block = {
        "dk_fair_prob": 0.55,
        "raw_probability": 0.57,
        "uncertainty_haircut": 0.02,
        "conservative_probability": 0.55,
        "current_ask": 0.50,
        "projected_edge_at_current_ask": 0.05,
        "model_version": "market-prior-v1",
    }
    block.update(overrides)
    return block


class MlbLineupWatchlistTests(unittest.TestCase):
    def entry(self, **overrides):
        item = {
            "id": "lineup-abc-def",
            "game": "ABC @ DEF",
            "side": "ABC",
            "first_pitch_utc": "2026-07-17T23:00:00Z",
            "recheck_due_utc": "2026-07-17T21:45:00Z",
            "blocked_only_by": ["lineups_unconfirmed"],
            "original_gate_results": {
                "starter_floor": True,
                "opposing_starter_shutdown_path": True,
                "bullpen_close_game_survival": True,
                "cold_fade_reset": True,
                "price_discipline": True,
                "real_winner_conviction": True,
                "lineups_confirmed": False,
            },
            "original_price": -125,
            "bettable_to_price": -135,
            "status": "pending_lineup_recheck",
        }
        item.update(overrides)
        return item

    def test_due_entry_is_selected_inside_thirtyfive_to_ninety_minute_window(self):
        schedule = {"lineup_watchlist": [self.entry()]}
        now = datetime(2026, 7, 17, 21, 45, tzinfo=timezone.utc)

        due = mlb_lineup_watchlist.due_entries(schedule, now)

        self.assertEqual([item["id"] for item in due], ["lineup-abc-def"])

    def test_entry_is_not_due_outside_window_or_after_terminal_status(self):
        early = datetime(2026, 7, 17, 21, 20, tzinfo=timezone.utc)
        late = datetime(2026, 7, 17, 22, 30, tzinfo=timezone.utc)  # 30 min pre-pitch, inside the 35 min floor
        promoted_candidate = {
            "watchlist_id": "lineup-abc-def",
            "sport": "MLB",
            "market_type": "moneyline",
            "execution_mode": "standing_authorized",
            "execution_status": "pending",
            "max_polymarket_price": 0.51,
            "executed": False,
            "baseball_evidence": valid_baseball_evidence(),
            **prob_block(),
        }
        promoted = self.entry(
            status="promoted",
            rechecked_at_utc="2026-07-17T21:45:00Z",
            slate_probability=prob_block(),
            recheck={
                "lineups_confirmed": True,
                "key_injuries_refreshed": True,
                "price_refreshed": True,
                "all_original_gates_hold": True,
                "probability": prob_block(),
                "material_changes": [],
            },
            promoted_candidate=promoted_candidate,
        )

        self.assertEqual(mlb_lineup_watchlist.due_entries({"lineup_watchlist": [self.entry()]}, early), [])
        self.assertEqual(mlb_lineup_watchlist.due_entries({"lineup_watchlist": [self.entry()]}, late), [])
        self.assertEqual(mlb_lineup_watchlist.due_entries({"lineup_watchlist": [promoted]}, datetime(2026, 7, 17, 21, 45, tzinfo=timezone.utc)), [])

    def test_entry_must_be_blocked_only_by_unconfirmed_lineups(self):
        extra_blocker = self.entry(blocked_only_by=["lineups_unconfirmed", "price_discipline"])
        broken_gate = self.entry()
        broken_gate["original_gate_results"]["starter_floor"] = False

        now = datetime(2026, 7, 17, 21, 45, tzinfo=timezone.utc)

        with self.assertRaises(mlb_lineup_watchlist.WatchlistFormatError):
            mlb_lineup_watchlist.due_entries({"lineup_watchlist": [extra_blocker, broken_gate]}, now)

    def test_pending_entry_requires_identity_timing_and_prices(self):
        broken = self.entry(id="", recheck_due_utc="bad", original_price=None, bettable_to_price=None)

        errors = mlb_lineup_watchlist.validate_entry(broken)

        self.assertIn("id must be a non-empty string", errors)
        self.assertIn("recheck_due_utc must be a valid timestamp", errors)
        self.assertIn("original_price must be numeric", errors)
        self.assertIn("bettable_to_price must be numeric", errors)

    def test_slate_schedule_rejects_descriptive_and_quoted_watchlist_prices(self):
        slate_schedule = {
            "date": "2026-07-18",
            "candidates": [],
            "lineup_watchlist": [
                self.entry(
                    id="LW20260718-MIN-CHC",
                    original_price="MIN +119 at DraftKings",
                    bettable_to_price="+105",
                )
            ],
        }

        errors = mlb_lineup_watchlist.validate_watchlist(slate_schedule)

        self.assertEqual(
            errors["LW20260718-MIN-CHC"],
            ["original_price must be numeric", "bettable_to_price must be numeric"],
        )

    def test_duplicate_watchlist_ids_fail_closed(self):
        with self.assertRaises(mlb_lineup_watchlist.WatchlistFormatError):
            mlb_lineup_watchlist.due_entries(
                {"lineup_watchlist": [self.entry(), self.entry()]},
                datetime(2026, 7, 17, 21, 45, tzinfo=timezone.utc),
            )

    def test_passed_entry_requires_timestamp_and_exact_blocker(self):
        errors = mlb_lineup_watchlist.validate_entry(self.entry(status="passed"))

        self.assertIn("passed entry requires rechecked_at_utc", errors)
        self.assertIn("passed entry requires non-empty recheck_notes", errors)

    def test_validation_rejects_manual_state_for_standing_authorized_mlb(self):
        promoted = self.entry(
            status="promoted",
            rechecked_at_utc="2026-07-17T21:45:00Z",
            recheck={"lineups_confirmed": True, "key_injuries_refreshed": True, "price_refreshed": True, "all_original_gates_hold": True},
            promoted_candidate={"watchlist_id": "lineup-abc-def", "execution_mode": "manual", "manual_bet_status": "awaiting_jerry", "executed": False},
        )

        errors = mlb_lineup_watchlist.validate_entry(promoted)

        self.assertIn("promoted_candidate.execution_mode must be standing_authorized", errors)
        self.assertIn("promoted_candidate.execution_status must be pending", errors)
        self.assertIn("promoted_candidate.max_polymarket_price must be between 0 and 1", errors)
        self.assertIn("promoted_candidate.sport must be MLB", errors)
        self.assertIn("promoted_candidate.market_type must be moneyline", errors)

    def test_entry_is_manual_only_requires_explicit_flag_not_prose(self):
        self.assertTrue(mlb_lineup_watchlist.entry_is_manual_only({"manual_only": True}))
        # Prose is deliberately NOT the control — "manual-only" is slate boilerplate
        # and would over-match executed standing-authorized bets.
        self.assertFalse(mlb_lineup_watchlist.entry_is_manual_only(
            {"thesis": "Promote only if lineups confirm; manual-only after Vig review."}))
        self.assertFalse(mlb_lineup_watchlist.entry_is_manual_only({"manual_only": "yes"}))
        self.assertFalse(mlb_lineup_watchlist.entry_is_manual_only({}))

    def test_manual_only_entry_cannot_auto_execute_even_when_standing_auth_on(self):
        from unittest import mock
        promoted = self.entry(
            status="promoted",
            manual_only=True,
            rechecked_at_utc="2026-07-17T21:45:00Z",
            recheck={"lineups_confirmed": True, "key_injuries_refreshed": True,
                     "price_refreshed": True, "all_original_gates_hold": True},
            promoted_candidate={
                "watchlist_id": "lineup-abc-def", "sport": "MLB", "market_type": "moneyline",
                "execution_mode": "standing_authorized", "execution_status": "pending",
                "max_polymarket_price": 0.51, "executed": False,
            },
        )
        with mock.patch.object(mlb_lineup_watchlist, "standing_authorization_enabled",
                               return_value=True):
            errors = mlb_lineup_watchlist.validate_entry(promoted)
        # Global standing auth is ON, but a manual-only pick must still route manual.
        self.assertIn("promoted_candidate.execution_mode must be manual", errors)
        self.assertIn("manual-only entry's promoted_candidate must set manual_only=true", errors)

    def test_manual_only_entry_accepts_manual_route_under_standing_auth(self):
        from unittest import mock
        promoted = self.entry(
            status="promoted",
            manual_only=True,
            rechecked_at_utc="2026-07-17T21:45:00Z",
            recheck={"lineups_confirmed": True, "key_injuries_refreshed": True,
                     "price_refreshed": True, "all_original_gates_hold": True},
            promoted_candidate={
                "watchlist_id": "lineup-abc-def", "execution_mode": "manual",
                "manual_bet_status": "awaiting_jerry", "manual_only": True, "executed": False,
            },
        )
        with mock.patch.object(mlb_lineup_watchlist, "standing_authorization_enabled",
                               return_value=True):
            errors = mlb_lineup_watchlist.validate_entry(promoted)
        self.assertEqual(errors, [])

    def test_lineup_context_distinguishes_pre_lineup_from_resolution_failure(self):
        entries = [self.entry(id="loaded"), self.entry(id="sparse")]
        snapshots = {
            "loaded": {"game_pk": 1, "player_count": 52, "away_team": "X", "home_team": "Y",
                       "away_batting_order": [], "home_batting_order": []},
            "sparse": {"game_pk": 2, "player_count": 2, "away_team": "P", "home_team": "Q",
                       "away_batting_order": [], "home_batting_order": []},
        }
        ctx = mlb_lineup_watchlist._lineup_context(entries, snapshots)
        self.assertIn("genuine pre-lineup", ctx)          # loaded feed, no orders yet
        self.assertIn("game-resolution or feed FAILURE", ctx)  # sparse feed = error, not a pass

    def test_recheck_prompt_routes_promotion_to_recurring_execution_poller(self):
        prompt = mlb_lineup_watchlist.build_recheck_prompt(Path("/tmp/schedule.json"), [self.entry()])

        self.assertIn("confirmed batting lineups", prompt)
        self.assertIn("late-scratch signal", prompt)
        self.assertIn("Polymarket ask", prompt)
        self.assertIn("every original gate", prompt)
        self.assertIn("execution_mode=standing_authorized", prompt)
        self.assertIn("execution_status=pending", prompt)
        self.assertIn("recurring MLB execution poller", prompt)
        self.assertNotIn("awaiting_jerry", prompt)
        self.assertIn("lineup-abc-def", prompt)

    def test_lineup_snapshot_maps_espn_event_to_mlb_game_pk_before_fetching_feed(self):
        entry = self.entry(
            event_id="401816229",
            game="Cincinnati Reds at Seattle Mariners",
            first_pitch_utc="2026-07-22T19:40:00Z",
        )
        schedule = {
            "dates": [{
                "games": [{
                    "gamePk": 823110,
                    "teams": {
                        "away": {"team": {"name": "Cincinnati Reds"}},
                        "home": {"team": {"name": "Seattle Mariners"}},
                    },
                }]
            }]
        }
        away_order = list(range(1, 10))
        home_order = list(range(10, 19))
        players = {
            f"ID{player_id}": {"fullName": f"Player {player_id}"}
            for player_id in range(1, 53)
        }
        feed = {
            "gameData": {"players": players},
            "liveData": {"boxscore": {"teams": {
                "away": {"battingOrder": away_order, "players": {}},
                "home": {"battingOrder": home_order, "players": {}},
            }}},
        }
        requested_urls = []

        def fetch_json(url):
            requested_urls.append(url)
            if "/api/v1/schedule?" in url:
                return schedule
            if url.endswith("/api/v1.1/game/823110/feed/live"):
                return feed
            self.fail(f"unexpected URL: {url}")

        snapshot = mlb_lineup_watchlist.fetch_lineup_snapshot(entry, fetch_json=fetch_json)

        self.assertEqual(snapshot["game_pk"], 823110)
        self.assertEqual(snapshot["player_count"], 52)
        self.assertEqual(len(snapshot["away_batting_order"]), 9)
        self.assertEqual(len(snapshot["home_batting_order"]), 9)
        self.assertIn("startDate=2026-07-21", requested_urls[0])
        self.assertIn("endDate=2026-07-23", requested_urls[0])
        self.assertNotIn("401816229/feed/live", "\n".join(requested_urls))

    def test_lineup_snapshot_uses_espn_event_teams_when_game_name_is_missing(self):
        entry = self.entry(
            event_id="401816229",
            game="",
            first_pitch_utc="2026-07-22T19:40:00Z",
        )
        schedule = {
            "dates": [{"games": [{
                "gamePk": 823110,
                "teams": {
                    "away": {"team": {"name": "Cincinnati Reds"}},
                    "home": {"team": {"name": "Seattle Mariners"}},
                },
            }]}]
        }
        espn = {
            "header": {"competitions": [{"competitors": [
                {"homeAway": "home", "team": {"displayName": "Seattle Mariners"}},
                {"homeAway": "away", "team": {"displayName": "Cincinnati Reds"}},
            ]}]}
        }
        feed = {
            "gameData": {"players": {}},
            "liveData": {"boxscore": {"teams": {
                "away": {"battingOrder": []},
                "home": {"battingOrder": []},
            }}},
        }
        requested_urls = []

        def fetch_json(url):
            requested_urls.append(url)
            if "/api/v1/schedule?" in url:
                return schedule
            if "site.api.espn.com" in url:
                return espn
            if url.endswith("/api/v1.1/game/823110/feed/live"):
                return feed
            self.fail(f"unexpected URL: {url}")

        snapshot = mlb_lineup_watchlist.fetch_lineup_snapshot(entry, fetch_json=fetch_json)

        self.assertEqual(snapshot["game_pk"], 823110)
        self.assertIn("event=401816229", requested_urls[1])
        self.assertTrue(requested_urls[2].endswith("/823110/feed/live"))

    def test_lineup_snapshot_resolves_west_coast_game_despite_utc_date_rollover(self):
        # Regression: a 6:40pm PT first pitch is 01:40Z the NEXT UTC day. MLB keys
        # the game under its ballpark-local officialDate (the prior day). When the
        # same two teams also play the following day, a single UTC-date query used
        # to resolve the WRONG (next-day, lineup-not-posted) game and falsely pass.
        entry = self.entry(
            game="Boston Red Sox at Athletics",
            side="Boston Red Sox",
            first_pitch_utc="2026-07-28T01:40:00Z",
        )
        # A +/-1 day window returns BOTH same-matchup games; tonight's has lineups.
        schedule = {
            "dates": [
                {"games": [{
                    "gamePk": 824977,
                    "gameDate": "2026-07-28T01:40:00Z",
                    "teams": {
                        "away": {"team": {"name": "Boston Red Sox"}},
                        "home": {"team": {"name": "Athletics"}},
                    },
                }]},
                {"games": [{
                    "gamePk": 824976,
                    "gameDate": "2026-07-29T01:40:00Z",
                    "teams": {
                        "away": {"team": {"name": "Boston Red Sox"}},
                        "home": {"team": {"name": "Athletics"}},
                    },
                }]},
            ]
        }
        order = [f"ID{i}" for i in range(1, 10)]
        feed_tonight = {
            "gameData": {"players": {f"ID{i}": {"fullName": f"P{i}"} for i in range(1, 53)}},
            "liveData": {"boxscore": {"teams": {
                "away": {"battingOrder": order},
                "home": {"battingOrder": order},
            }}},
        }
        requested_urls = []

        def fetch_json(url):
            requested_urls.append(url)
            if "/api/v1/schedule?" in url:
                return schedule
            if url.endswith("/api/v1.1/game/824977/feed/live"):
                return feed_tonight
            self.fail(f"resolved wrong game / unexpected URL: {url}")

        snapshot = mlb_lineup_watchlist.fetch_lineup_snapshot(entry, fetch_json=fetch_json)

        # Must resolve tonight's game (nearest first pitch), NOT tomorrow's.
        self.assertEqual(snapshot["game_pk"], 824977)
        self.assertEqual(len(snapshot["away_batting_order"]), 9)
        self.assertEqual(len(snapshot["home_batting_order"]), 9)
        self.assertIn("startDate=2026-07-27", requested_urls[0])
        self.assertIn("endDate=2026-07-29", requested_urls[0])

    def test_resolve_game_pk_picks_doubleheader_game_nearest_first_pitch(self):
        schedule = {
            "dates": [{
                "games": [
                    {
                        "gamePk": 111,
                        "gameDate": "2026-07-22T17:10:00Z",
                        "teams": {
                            "away": {"team": {"name": "Cincinnati Reds"}},
                            "home": {"team": {"name": "Seattle Mariners"}},
                        },
                    },
                    {
                        "gamePk": 222,
                        "gameDate": "2026-07-22T23:40:00Z",
                        "teams": {
                            "away": {"team": {"name": "Cincinnati Reds"}},
                            "home": {"team": {"name": "Seattle Mariners"}},
                        },
                    },
                ]
            }]
        }
        nightcap_first_pitch = datetime(2026, 7, 22, 23, 40, tzinfo=timezone.utc)

        game_pk = mlb_lineup_watchlist.resolve_game_pk(
            schedule, "Cincinnati Reds", "Seattle Mariners", first_pitch=nightcap_first_pitch
        )

        self.assertEqual(game_pk, 222)

        opener_first_pitch = datetime(2026, 7, 22, 17, 0, tzinfo=timezone.utc)
        self.assertEqual(
            mlb_lineup_watchlist.resolve_game_pk(
                schedule, "Cincinnati Reds", "Seattle Mariners", first_pitch=opener_first_pitch
            ),
            111,
        )

    def test_lineup_snapshot_uses_stamped_game_pk_without_schedule_lookup(self):
        entry = self.entry(
            game_pk=823110,
            game="Cincinnati Reds at Seattle Mariners",
            first_pitch_utc="2026-07-22T19:40:00Z",
        )
        feed = {
            "gameData": {
                "players": {"ID1": {"fullName": "Player 1"}},
                "teams": {
                    "away": {"name": "Cincinnati Reds"},
                    "home": {"name": "Seattle Mariners"},
                },
            },
            "liveData": {"boxscore": {"teams": {
                "away": {"battingOrder": [1]},
                "home": {"battingOrder": []},
            }}},
        }
        requested_urls = []

        def fetch_json(url):
            requested_urls.append(url)
            if url.endswith("/api/v1.1/game/823110/feed/live"):
                return feed
            self.fail(f"unexpected URL: {url}")

        snapshot = mlb_lineup_watchlist.fetch_lineup_snapshot(entry, fetch_json=fetch_json)

        self.assertEqual(snapshot["game_pk"], 823110)
        self.assertEqual(snapshot["away_team"], "Cincinnati Reds")
        self.assertEqual(snapshot["home_team"], "Seattle Mariners")
        self.assertEqual(requested_urls, ["https://statsapi.mlb.com/api/v1.1/game/823110/feed/live"])

    def test_validate_entry_rejects_non_positive_or_non_integer_game_pk(self):
        for bad in (0, -5, True, "823110", 1.5):
            errors = mlb_lineup_watchlist.validate_entry(self.entry(game_pk=bad))
            self.assertIn("game_pk must be a positive integer when present", errors, msg=repr(bad))

        self.assertEqual(mlb_lineup_watchlist.validate_entry(self.entry(game_pk=823110)), [])
        self.assertEqual(mlb_lineup_watchlist.validate_entry(self.entry()), [])

    def test_recheck_prompt_includes_concise_resolved_mlb_lineups(self):
        snapshot = {
            "game_pk": 823110,
            "away_team": "Cincinnati Reds",
            "home_team": "Seattle Mariners",
            "player_count": 52,
            "away_batting_order": [f"Red {number}" for number in range(1, 10)],
            "home_batting_order": [f"Mariner {number}" for number in range(1, 10)],
        }

        prompt = mlb_lineup_watchlist.build_recheck_prompt(
            Path("/tmp/schedule.json"),
            [self.entry(id="2026-07-22-SEA-ML")],
            {"2026-07-22-SEA-ML": snapshot},
        )

        self.assertIn("MLB gamePk 823110", prompt)
        self.assertIn("52 roster players", prompt)
        self.assertIn("Cincinnati Reds batting order (9)", prompt)
        self.assertIn("Seattle Mariners batting order (9)", prompt)
        self.assertNotIn("{\"game_pk\"", prompt)


    # --- Phase 4 refresh contract (standing-authorized promotions) ---

    def refresh_promoted_entry(self, morning=None, refreshed=None, recheck_extra=None,
                               candidate_extra=None, **overrides):
        """A fully refresh-contract-compliant standing-authorized promotion."""
        morning = morning if morning is not None else prob_block()
        refreshed = refreshed if refreshed is not None else prob_block()
        recheck = {
            "lineups_confirmed": True,
            "key_injuries_refreshed": True,
            "price_refreshed": True,
            "all_original_gates_hold": True,
            "probability": refreshed,
            "material_changes": [],
        }
        recheck.update(recheck_extra or {})
        candidate = {
            "watchlist_id": "lineup-abc-def",
            "sport": "MLB",
            "market_type": "moneyline",
            "execution_mode": "standing_authorized",
            "execution_status": "pending",
            "max_polymarket_price": 0.5,
            "executed": False,
            "baseball_evidence": valid_baseball_evidence(),
            **refreshed,
        }
        candidate.update(candidate_extra or {})
        entry = self.entry(
            status="promoted",
            rechecked_at_utc="2026-07-17T21:45:00Z",
            slate_probability=morning,
            recheck=recheck,
            promoted_candidate=candidate,
        )
        entry.update(overrides)
        return entry

    def validate_authorized(self, entry):
        with mock.patch.object(
            mlb_lineup_watchlist, "standing_authorization_enabled", return_value=True
        ):
            return mlb_lineup_watchlist.validate_entry(entry)

    def test_refresh_contract_compliant_promotion_passes(self):
        self.assertEqual(self.validate_authorized(self.refresh_promoted_entry()), [])

    def test_promotion_that_merely_asserts_gates_hold_is_rejected(self):
        # No slate_probability, no recheck.probability, no material_changes, no
        # refreshed evidence — the pre-Phase-4 shape must now fail.
        promoted = self.entry(
            status="promoted",
            rechecked_at_utc="2026-07-17T21:45:00Z",
            recheck={
                "lineups_confirmed": True,
                "key_injuries_refreshed": True,
                "price_refreshed": True,
                "all_original_gates_hold": True,
            },
            promoted_candidate={
                "watchlist_id": "lineup-abc-def",
                "sport": "MLB",
                "market_type": "moneyline",
                "execution_mode": "standing_authorized",
                "execution_status": "pending",
                "max_polymarket_price": 0.51,
                "executed": False,
            },
        )
        errors = self.validate_authorized(promoted)
        self.assertTrue(any("slate_probability must be an object" in e for e in errors), errors)
        self.assertTrue(
            any("merely asserts all original gates hold is invalid" in e for e in errors), errors
        )
        self.assertTrue(any("recheck.material_changes must be a list" in e for e in errors), errors)
        self.assertTrue(any("baseball evidence" in e for e in errors), errors)

    def test_changed_probability_component_requires_written_reason(self):
        refreshed = prob_block(
            raw_probability=0.58,
            conservative_probability=0.56,
            projected_edge_at_current_ask=0.06,
        )
        promoted = self.refresh_promoted_entry(refreshed=refreshed)
        errors = self.validate_authorized(promoted)
        self.assertIn(
            "recheck.probability_change_reasons.raw_probability is required: "
            "raw_probability changed from the morning slate",
            errors,
        )
        self.assertIn(
            "recheck.probability_change_reasons.conservative_probability is required: "
            "conservative_probability changed from the morning slate",
            errors,
        )

    def test_probability_increase_requires_quantified_upgrade(self):
        refreshed = prob_block(
            raw_probability=0.58,
            conservative_probability=0.56,
            projected_edge_at_current_ask=0.06,
        )
        reasons = {
            "raw_probability": "confirmed order upgraded the offense",
            "conservative_probability": "raw moved; haircut unchanged",
        }
        no_upgrade = self.refresh_promoted_entry(
            refreshed=refreshed,
            recheck_extra={"probability_change_reasons": reasons},
        )
        errors = self.validate_authorized(no_upgrade)
        self.assertTrue(
            any("lineup confirmation alone adds zero probability" in e for e in errors),
            errors,
        )

        wrong_delta = self.refresh_promoted_entry(
            refreshed=refreshed,
            recheck_extra={
                "probability_change_reasons": reasons,
                "quantified_upgrade": {
                    "component": "lineup",
                    "delta": 0.05,
                    "evidence": "Confirmed order carries both platoon bats",
                },
            },
        )
        errors = self.validate_authorized(wrong_delta)
        self.assertTrue(
            any("quantified_upgrade.delta must equal" in e for e in errors), errors
        )

        quantified = self.refresh_promoted_entry(
            refreshed=refreshed,
            recheck_extra={
                "probability_change_reasons": reasons,
                "quantified_upgrade": {
                    "component": "lineup",
                    "delta": 0.01,
                    "evidence": "Confirmed order carries both platoon bats",
                },
            },
        )
        self.assertEqual(self.validate_authorized(quantified), [])

    def test_material_change_with_unchanged_probability_requires_justification(self):
        promoted = self.refresh_promoted_entry(
            recheck_extra={"material_changes": ["bullpen: closer threw 25 pitches yesterday"]},
        )
        errors = self.validate_authorized(promoted)
        self.assertTrue(
            any("probability_unchanged_justification is required" in e for e in errors),
            errors,
        )

        justified = self.refresh_promoted_entry(
            recheck_extra={
                "material_changes": ["bullpen: closer threw 25 pitches yesterday"],
                "probability_unchanged_justification": (
                    "Setup arm covers the ninth at equivalent leverage quality; "
                    "haircut already priced bullpen fatigue"
                ),
            },
        )
        self.assertEqual(self.validate_authorized(justified), [])

    def test_promoted_candidate_cannot_route_morning_numbers(self):
        refreshed = prob_block(
            raw_probability=0.58,
            conservative_probability=0.56,
            projected_edge_at_current_ask=0.06,
        )
        promoted = self.refresh_promoted_entry(
            refreshed=refreshed,
            recheck_extra={
                "probability_change_reasons": {
                    "raw_probability": "announced arm weaker",
                    "conservative_probability": "raw moved",
                },
                "quantified_upgrade": {
                    "component": "opposing_starter",
                    "delta": 0.01,
                    "evidence": "6.1 ERA arm announced",
                },
            },
            # Candidate silently keeps the MORNING numbers — must be rejected.
            candidate_extra=prob_block(),
        )
        errors = self.validate_authorized(promoted)
        self.assertTrue(
            any(
                "promoted_candidate.raw_probability must equal "
                "recheck.probability.raw_probability" in e
                for e in errors
            ),
            errors,
        )

    def test_manual_only_promotion_is_exempt_from_refresh_contract(self):
        # Manual/awaiting_jerry routing is not standing-authorized; the Phase 4
        # contract deliberately does not apply (PR2 scoping precedent).
        promoted = self.entry(
            status="promoted",
            manual_only=True,
            rechecked_at_utc="2026-07-17T21:45:00Z",
            recheck={"lineups_confirmed": True, "key_injuries_refreshed": True,
                     "price_refreshed": True, "all_original_gates_hold": True},
            promoted_candidate={
                "watchlist_id": "lineup-abc-def", "execution_mode": "manual",
                "manual_bet_status": "awaiting_jerry", "manual_only": True, "executed": False,
            },
        )
        self.assertEqual(self.validate_authorized(promoted), [])

    def test_recheck_prompt_carries_refresh_contract_and_policy_floor(self):
        with mock.patch.object(
            mlb_lineup_watchlist, "standing_authorization_enabled", return_value=True
        ), mock.patch.object(
            mlb_lineup_watchlist, "load_mlb_selection_policy",
            return_value=enabled_starter_policy(),
        ):
            prompt = mlb_lineup_watchlist.build_recheck_prompt(
                Path("/tmp/schedule.json"), [self.entry()]
            )
        self.assertIn("RECHECK REFRESH CONTRACT", prompt)
        self.assertIn("adds ZERO win probability", prompt)
        self.assertIn("recheck.probability", prompt)
        self.assertIn("recheck.material_changes", prompt)
        self.assertIn("merely asserts the original gates hold", prompt)

    def test_starter_rehandicap_prompt_floor_comes_from_policy_not_hardcode(self):
        entry = self.entry(blocked_only_by=["lineups_unconfirmed", "starter_unannounced"])
        entry["original_gate_results"]["opposing_starter_shutdown_path"] = None
        entry["original_gate_results"]["real_winner_conviction"] = None
        with mock.patch.object(
            mlb_lineup_watchlist, "load_mlb_selection_policy",
            return_value=enabled_starter_policy(min_conservative_edge=0.07),
        ):
            prompt = mlb_lineup_watchlist.build_recheck_prompt(
                Path("/tmp/schedule.json"), [entry]
            )
        self.assertIn("net_edge >= 0.07", prompt)
        self.assertNotIn("0.05", prompt)

    # --- starter-pending recheck (deferred opposing-starter gates) ---

    def starter_pending_entry(self, **overrides):
        """A near-miss whose ONLY open blockers are unconfirmed lineups AND an
        opposing starter not yet announced at slate time."""
        entry = self.entry(
            blocked_only_by=["lineups_unconfirmed", "starter_unannounced"],
        )
        # The two starter-dependent gates are deferred (null), re-derived at recheck.
        entry["original_gate_results"]["opposing_starter_shutdown_path"] = None
        entry["original_gate_results"]["real_winner_conviction"] = None
        entry.update(overrides)
        return entry

    def test_starter_pending_entry_defers_opposing_starter_gates(self):
        # Admissible only when the shared policy re-enables starter-pending work.
        with mock.patch.object(
            mlb_lineup_watchlist, "load_mlb_selection_policy",
            return_value=enabled_starter_policy(),
        ):
            errors = mlb_lineup_watchlist.validate_entry(self.starter_pending_entry())
        self.assertEqual(errors, [])

    def test_pending_starter_entry_is_inadmissible_while_policy_disables_it(self):
        # Phase 4: the live watchlist is lineups_unconfirmed-only. A pending
        # starter_unannounced entry fails validation under the deployed default
        # policy AND when no policy loads at all (fail closed).
        disabled = mlb_lineup_watchlist.MlbSelectionPolicy(
            min_conservative_edge=0.05,
            max_mlb_official_bets_per_day=2,
            starter_pending_promotions_enabled=False,
            max_small_bets_per_day_probation=1,
            policy_version="test",
            effective_at="2026-08-11T00:00:00Z",
        )
        for policy in (None, disabled):
            with mock.patch.object(
                mlb_lineup_watchlist, "load_mlb_selection_policy", return_value=policy
            ):
                errors = mlb_lineup_watchlist.validate_entry(self.starter_pending_entry())
            self.assertTrue(
                any("starter_unannounced entries are not admissible" in e for e in errors),
                errors,
            )

    def test_terminal_starter_entry_remains_valid_historical_record(self):
        # Transition rule: an already-passed starter entry from before the
        # restriction is historical record, not a live violation.
        passed = self.starter_pending_entry(
            status="passed",
            rechecked_at_utc="2026-07-17T21:45:00Z",
            recheck_notes="Announced starter erased the edge.",
        )
        with mock.patch.object(
            mlb_lineup_watchlist, "load_mlb_selection_policy", return_value=None
        ):
            errors = mlb_lineup_watchlist.validate_entry(passed)
        self.assertEqual(errors, [])

    def test_starter_only_blocker_allows_confirmed_lineups(self):
        entry = self.entry(blocked_only_by=["starter_unannounced"])
        entry["original_gate_results"]["opposing_starter_shutdown_path"] = None
        entry["original_gate_results"]["real_winner_conviction"] = None
        entry["original_gate_results"]["lineups_confirmed"] = True
        with mock.patch.object(
            mlb_lineup_watchlist, "load_mlb_selection_policy",
            return_value=enabled_starter_policy(),
        ):
            self.assertEqual(mlb_lineup_watchlist.validate_entry(entry), [])

    def test_starter_pending_rejects_asserted_deferred_gate(self):
        entry = self.starter_pending_entry()
        entry["original_gate_results"]["opposing_starter_shutdown_path"] = True
        errors = mlb_lineup_watchlist.validate_entry(entry)
        self.assertIn(
            "original_gate_results.opposing_starter_shutdown_path must be null while "
            "starter_unannounced (it is re-derived at recheck)",
            errors,
        )

    def test_non_starter_entry_still_requires_all_six_gates(self):
        entry = self.entry()
        entry["original_gate_results"]["opposing_starter_shutdown_path"] = None
        errors = mlb_lineup_watchlist.validate_entry(entry)
        self.assertIn(
            "original_gate_results.opposing_starter_shutdown_path must be true", errors
        )

    def test_unknown_blocker_is_rejected(self):
        entry = self.entry(blocked_only_by=["lineups_unconfirmed", "price_discipline"])
        errors = mlb_lineup_watchlist.validate_entry(entry)
        self.assertTrue(any("blocked_only_by must be a non-empty list" in e for e in errors))

    def test_empty_blocker_list_is_rejected(self):
        errors = mlb_lineup_watchlist.validate_entry(self.entry(blocked_only_by=[]))
        self.assertTrue(any("blocked_only_by must be a non-empty list" in e for e in errors))

    def test_starter_pending_promotion_requires_rehandicap_flags(self):
        promoted = self.starter_pending_entry(
            status="promoted",
            rechecked_at_utc="2026-07-17T21:45:00Z",
            recheck={
                "lineups_confirmed": True,
                "key_injuries_refreshed": True,
                "price_refreshed": True,
                "all_original_gates_hold": True,
                # starter_confirmed / net_edge_recomputed intentionally missing
            },
            promoted_candidate={
                "watchlist_id": "lineup-abc-def",
                "sport": "MLB",
                "market_type": "moneyline",
                "execution_mode": "standing_authorized",
                "execution_status": "pending",
                "max_polymarket_price": 0.53,
                "executed": False,
            },
        )
        with mock.patch.object(
            mlb_lineup_watchlist, "standing_authorization_enabled", return_value=True
        ):
            errors = mlb_lineup_watchlist.validate_entry(promoted)
        self.assertIn("recheck.starter_confirmed must be true", errors)
        self.assertIn("recheck.net_edge_recomputed must be true", errors)

    def test_starter_pending_promotion_with_rehandicap_flags_blocked_by_default_policy(self):
        # Default policy disables starter-pending promotions: a fully re-handicapped
        # promotion is still rejected while the shared switch is off.
        promoted = self.starter_pending_entry(
            status="promoted",
            rechecked_at_utc="2026-07-17T21:45:00Z",
            recheck={
                "lineups_confirmed": True,
                "key_injuries_refreshed": True,
                "price_refreshed": True,
                "all_original_gates_hold": True,
                "starter_confirmed": True,
                "net_edge_recomputed": True,
            },
            promoted_candidate={
                "watchlist_id": "lineup-abc-def",
                "sport": "MLB",
                "market_type": "moneyline",
                "execution_mode": "standing_authorized",
                "execution_status": "pending",
                "max_polymarket_price": 0.53,
                "executed": False,
            },
        )
        with mock.patch.object(
            mlb_lineup_watchlist, "standing_authorization_enabled", return_value=True
        ):
            errors = mlb_lineup_watchlist.validate_entry(promoted)
        self.assertIn(
            "starter_unannounced entries cannot be promoted: "
            "starter_pending_promotions_enabled is false in the shared MLB selection policy",
            errors,
        )

    def test_starter_pending_promotion_with_rehandicap_flags_passes(self):
        # The announced starter changed the read: raw/conservative moved and the
        # refresh contract records the reasons + the quantified upgrade.
        morning = prob_block()
        refreshed = prob_block(
            raw_probability=0.58,
            conservative_probability=0.56,
            projected_edge_at_current_ask=0.06,
        )
        promoted = self.starter_pending_entry(
            status="promoted",
            rechecked_at_utc="2026-07-17T21:45:00Z",
            slate_probability=morning,
            recheck={
                "lineups_confirmed": True,
                "key_injuries_refreshed": True,
                "price_refreshed": True,
                "all_original_gates_hold": True,
                "starter_confirmed": True,
                "net_edge_recomputed": True,
                "probability": refreshed,
                "material_changes": ["opposing starter announced: soft-contact rookie"],
                "probability_change_reasons": {
                    "raw_probability": "announced opposing starter is a weaker arm than priced",
                    "conservative_probability": "haircut unchanged; raw moved up on the announced arm",
                },
                "quantified_upgrade": {
                    "component": "opposing_starter",
                    "delta": 0.01,
                    "evidence": "Announced starter: 6.1 ERA, 8% K-BB over last 5 starts",
                },
            },
            promoted_candidate={
                "watchlist_id": "lineup-abc-def",
                "sport": "MLB",
                "market_type": "moneyline",
                "execution_mode": "standing_authorized",
                "execution_status": "pending",
                "max_polymarket_price": 0.51,
                "executed": False,
                "baseball_evidence": valid_baseball_evidence(),
                **refreshed,
            },
        )
        with mock.patch.object(
            mlb_lineup_watchlist, "standing_authorization_enabled", return_value=True
        ), mock.patch.object(
            mlb_lineup_watchlist, "load_mlb_selection_policy",
            return_value=enabled_starter_policy(),
        ):
            errors = mlb_lineup_watchlist.validate_entry(promoted)
        self.assertEqual(errors, [])

    def test_snapshot_extracts_announced_probable_pitchers(self):
        entry = self.entry(game_pk=823110)
        feed = {
            "gameData": {
                "players": {f"ID{n}": {"fullName": f"P{n}"} for n in range(1, 40)},
                "probablePitchers": {
                    "away": {"id": 1, "fullName": "Logan Webb"},
                    "home": {},  # not yet announced
                },
            },
            "liveData": {"boxscore": {"teams": {
                "away": {"battingOrder": list(range(1, 10))},
                "home": {"battingOrder": []},
            }}},
        }

        def fetch_json(url):
            self.assertTrue(url.endswith("/api/v1.1/game/823110/feed/live"))
            return feed

        snapshot = mlb_lineup_watchlist.fetch_lineup_snapshot(entry, fetch_json=fetch_json)
        self.assertEqual(snapshot["away_probable_pitcher"], "Logan Webb")
        self.assertEqual(snapshot["home_probable_pitcher"], "")

    def test_recheck_prompt_includes_starter_rehandicap_block(self):
        snapshot = {
            "game_pk": 823110,
            "away_team": "Cincinnati Reds",
            "home_team": "Seattle Mariners",
            "player_count": 52,
            "away_batting_order": [f"Red {n}" for n in range(1, 10)],
            "home_batting_order": [f"Mariner {n}" for n in range(1, 10)],
            "away_probable_pitcher": "Hunter Greene",
            "home_probable_pitcher": "",
        }
        entry = self.starter_pending_entry(id="2026-07-22-SEA-ML")
        policy = mlb_lineup_watchlist.MlbSelectionPolicy(
            min_conservative_edge=0.05,
            max_mlb_official_bets_per_day=2,
            starter_pending_promotions_enabled=True,
            max_small_bets_per_day_probation=1,
            policy_version="test",
            effective_at="2026-08-11T00:00:00Z",
        )
        with mock.patch.object(
            mlb_lineup_watchlist, "load_mlb_selection_policy", return_value=policy
        ):
            prompt = mlb_lineup_watchlist.build_recheck_prompt(
                Path("/tmp/schedule.json"), [entry], {"2026-07-22-SEA-ML": snapshot}
            )
        self.assertIn("STARTER-PENDING RE-HANDICAP", prompt)
        self.assertIn("net_edge = win_probability - current_ask", prompt)
        self.assertIn("net_edge >= 0.05", prompt)
        self.assertNotIn("net_edge >= 0.02", prompt)
        self.assertIn("PROBABLE STARTERS", prompt)
        self.assertIn("Hunter Greene", prompt)
        self.assertIn("NOT YET ANNOUNCED", prompt)

    def test_recheck_prompt_blocks_starter_pending_when_policy_disables_promotions(self):
        snapshot = {
            "game_pk": 823110,
            "away_team": "Cincinnati Reds",
            "home_team": "Seattle Mariners",
            "player_count": 52,
            "away_batting_order": [f"Red {n}" for n in range(1, 10)],
            "home_batting_order": [f"Mariner {n}" for n in range(1, 10)],
        }
        entry = self.starter_pending_entry(id="2026-07-22-SEA-ML")
        policy = mlb_lineup_watchlist.MlbSelectionPolicy(
            min_conservative_edge=0.05,
            max_mlb_official_bets_per_day=2,
            starter_pending_promotions_enabled=False,
            max_small_bets_per_day_probation=1,
            policy_version="test",
            effective_at="2026-08-11T00:00:00Z",
        )
        with mock.patch.object(
            mlb_lineup_watchlist, "load_mlb_selection_policy", return_value=policy
        ):
            prompt = mlb_lineup_watchlist.build_recheck_prompt(
                Path("/tmp/schedule.json"), [entry], {"2026-07-22-SEA-ML": snapshot}
            )
        self.assertIn("STARTER-PENDING PROMOTIONS DISABLED", prompt)
        self.assertNotIn("STARTER-PENDING RE-HANDICAP", prompt)


if __name__ == "__main__":
    unittest.main()
