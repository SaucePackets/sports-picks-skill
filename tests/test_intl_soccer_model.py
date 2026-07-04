import unittest

from scripts.intl_soccer_model import MatchModel, TeamStrength


class IntlSoccerModelTests(unittest.TestCase):
    def test_knockout_stage_aliases_have_advance_probability(self):
        favorite = TeamStrength("France", adj_gf=3.33, adj_ga=0.67, wins=5, draws=0, losses=0)
        underdog = TeamStrength("Paraguay", adj_gf=0.67, adj_ga=1.67, wins=2, draws=2, losses=1)

        for stage in ["R32", "R16", "QF", "SF", "final", "3rd", "knockout"]:
            with self.subTest(stage=stage):
                model = MatchModel(favorite, underdog, stage=stage)
                self.assertEqual(model.stage, "knockout")
                self.assertGreater(
                    model.probabilities.p_advance_fav,
                    model.probabilities.p_win_90,
                )

    def test_group_stage_advance_matches_90_min_win(self):
        favorite = TeamStrength("France", adj_gf=3.33, adj_ga=0.67, wins=5, draws=0, losses=0)
        underdog = TeamStrength("Paraguay", adj_gf=0.67, adj_ga=1.67, wins=2, draws=2, losses=1)

        model = MatchModel(favorite, underdog, stage="group")

        self.assertEqual(model.stage, "group")
        self.assertEqual(model.probabilities.p_advance_fav, model.probabilities.p_win_90)

    def test_france_july_4_to_advance_edge(self):
        france = TeamStrength("France", adj_gf=3.33, adj_ga=0.67, wins=5, draws=0, losses=0)
        paraguay = TeamStrength("Paraguay", adj_gf=0.67, adj_ga=1.67, wins=2, draws=2, losses=1)

        model = MatchModel(france, paraguay, stage="R16")
        edge = model.edge_to_advance("France", pm_price=0.835)

        self.assertAlmostEqual(edge.model_prob, 0.901, places=3)
        self.assertAlmostEqual(edge.edge_pct, 6.6, places=1)
        self.assertEqual(edge.edge_quality, "thin_heavy_fav")
        self.assertEqual(edge.direction, "bet")

    def test_cross_market_comparison_not_needed_for_edge(self):
        favorite = TeamStrength("Favorite", adj_gf=2.0, adj_ga=1.0, wins=3, draws=1, losses=1)
        underdog = TeamStrength("Underdog", adj_gf=1.0, adj_ga=1.5, wins=2, draws=1, losses=2)

        model = MatchModel(favorite, underdog, stage="R16")
        advance_edge = model.edge_to_advance("Favorite", pm_price=0.70)
        ml_edge = model.edge_90min_ml("Favorite", market_implied=0.70)

        self.assertEqual(advance_edge.market_type, "to_advance")
        self.assertEqual(ml_edge.market_type, "90min_ml")
        self.assertNotEqual(advance_edge.model_prob, ml_edge.model_prob)


if __name__ == "__main__":
    unittest.main()
