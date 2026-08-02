import unittest

from scripts.nfl_final_scores import final_scores


def event(event_id, away, home, away_score, home_score, completed=True, status="STATUS_FINAL"):
    return {
        "id": event_id,
        "competitions": [
            {
                "status": {"type": {"completed": completed, "name": status}},
                "competitors": [
                    {"homeAway": "away", "score": away_score, "team": {"displayName": away}},
                    {"homeAway": "home", "score": home_score, "team": {"displayName": home}},
                ],
            }
        ],
    }


class FinalScoresTests(unittest.TestCase):
    def test_only_completed_games_are_returned_with_winner(self):
        scoreboard = {
            "events": [
                event("1", "Kansas City Chiefs", "Buffalo Bills", "20", "24"),
                event("2", "Dallas Cowboys", "Philadelphia Eagles", "31", "17"),
                event("3", "Green Bay Packers", "Chicago Bears", "0", "0", completed=False, status="STATUS_IN_PROGRESS"),
            ]
        }

        rows = final_scores(scoreboard)

        self.assertEqual([row["event_id"] for row in rows], ["1", "2"])
        self.assertEqual(rows[0]["winner"], "Buffalo Bills")
        self.assertEqual(rows[1]["winner"], "Dallas Cowboys")
        self.assertEqual(rows[0]["status"], "STATUS_FINAL")

    def test_tie_yields_null_winner_with_scores_present(self):
        rows = final_scores({"events": [event("9", "A", "B", 20, 20)]})

        self.assertEqual(rows[0]["away_score"], 20)
        self.assertEqual(rows[0]["home_score"], 20)
        self.assertIsNone(rows[0]["winner"])

    def test_nested_score_objects_are_parsed(self):
        rows = final_scores(
            {"events": [event("7", "A", "B", {"value": 27.0, "displayValue": "27"}, {"value": 13.0})]}
        )

        self.assertEqual(rows[0]["away_score"], 27)
        self.assertEqual(rows[0]["home_score"], 13)
        self.assertEqual(rows[0]["winner"], "A")

    def test_missing_scores_yield_null_winner(self):
        rows = final_scores({"events": [event("5", "A", "B", None, 4)]})

        self.assertIsNone(rows[0]["away_score"])
        self.assertIsNone(rows[0]["winner"])

    def test_empty_scoreboard_yields_empty_list(self):
        self.assertEqual(final_scores({}), [])
        self.assertEqual(final_scores({"events": []}), [])


if __name__ == "__main__":
    unittest.main()
