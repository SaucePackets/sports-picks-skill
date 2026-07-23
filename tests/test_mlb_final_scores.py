import unittest

from scripts.mlb_final_scores import final_scores


def game(game_pk, away, home, away_score, home_score, status="Final"):
    return {
        "gamePk": game_pk,
        "status": {"detailedState": status},
        "teams": {
            "away": {"score": away_score, "team": {"name": away}},
            "home": {"score": home_score, "team": {"name": home}},
        },
    }


class FinalScoresTests(unittest.TestCase):
    def test_only_final_games_are_returned_with_winner(self):
        schedule = {
            "dates": [{
                "games": [
                    game(1, "Cincinnati Reds", "Seattle Mariners", 2, 5),
                    game(2, "New York Yankees", "Boston Red Sox", 3, 1),
                    game(3, "Chicago Cubs", "Milwaukee Brewers", 0, 0, status="In Progress"),
                    game(4, "Miami Marlins", "Atlanta Braves", None, None, status="Postponed"),
                ]
            }]
        }

        rows = final_scores(schedule)

        self.assertEqual([row["gamePk"] for row in rows], [1, 2])
        self.assertEqual(rows[0]["winner"], "Seattle Mariners")
        self.assertEqual(rows[1]["winner"], "New York Yankees")
        self.assertEqual(rows[0]["status"], "Final")

    def test_missing_scores_yield_null_winner(self):
        schedule = {"dates": [{"games": [game(9, "A", "B", None, 4)]}]}

        rows = final_scores(schedule)

        self.assertEqual(rows[0]["away_score"], None)
        self.assertIsNone(rows[0]["winner"])

    def test_empty_schedule_yields_empty_list(self):
        self.assertEqual(final_scores({}), [])
        self.assertEqual(final_scores({"dates": []}), [])


if __name__ == "__main__":
    unittest.main()
