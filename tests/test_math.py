"""Unit tests for the pure math cores.

The ERA swap-impact test locks in the fix for issue #1 (the 3x overstatement
shipped in the period-20 brief); the tie-splitting and Cholesky tests guard
the standings and Monte Carlo foundations.
"""
import math

from fantasy_assistant.analytics.races import SWAP, _points_for, swap_impact
from fantasy_assistant.analytics.variance import cholesky


class TestPointsFor:
    def test_no_ties_higher(self):
        pts = _points_for({"a": 30, "b": 20, "c": 10}, "higher")
        assert pts == {"a": 3.0, "b": 2.0, "c": 1.0}

    def test_no_ties_lower(self):
        pts = _points_for({"a": 3.50, "b": 4.10, "c": 2.90}, "lower")
        assert pts == {"c": 3.0, "a": 2.0, "b": 1.0}

    def test_two_way_tie_splits_average(self):
        pts = _points_for({"a": 20, "b": 20, "c": 10}, "higher")
        assert pts["a"] == pts["b"] == 2.5
        assert pts["c"] == 1.0

    def test_three_way_tie(self):
        pts = _points_for({"a": 5, "b": 5, "c": 5, "d": 1}, "higher")
        assert pts["a"] == pts["b"] == pts["c"] == 3.0
        assert pts["d"] == 1.0

    def test_total_points_conserved(self):
        vals = {"a": 4, "b": 4, "c": 2, "d": 2, "e": 1}
        pts = _points_for(vals, "higher")
        assert sum(pts.values()) == sum(range(1, len(vals) + 1))


class TestSwapImpact:
    def test_era_divisor_is_27_not_9(self):
        # issue #1: 60 IP at 1.00 better ERA saves 60/9 ER; on 1000 team outs
        # that moves team ERA by 27*(60/9)/1000 = 0.180 — NOT 0.540
        assert math.isclose(swap_impact("ERA", 1000.0), 0.180)

    def test_era_first_principles(self):
        vol, edge, outs = SWAP["ERA"]["vol"], SWAP["ERA"]["edge"], 3600.0
        d_er = edge * vol / 9.0
        assert math.isclose(swap_impact("ERA", outs), 27.0 * d_er / outs)

    def test_whip(self):
        vol, edge, outs = SWAP["WHIP"]["vol"], SWAP["WHIP"]["edge"], 3600.0
        d_wh = edge * vol
        assert math.isclose(swap_impact("WHIP", outs), 3.0 * d_wh / outs)

    def test_obp(self):
        vol, edge, pa = SWAP["OBP"]["vol"], SWAP["OBP"]["edge"], 6000.0
        assert math.isclose(swap_impact("OBP", pa), vol * edge / pa)


class TestCholesky:
    def test_identity(self):
        eye = [[1.0, 0.0], [0.0, 1.0]]
        assert cholesky(eye) == eye

    def test_reconstructs_spd_matrix(self):
        a = [[4.0, 2.0, 0.6], [2.0, 5.0, 1.5], [0.6, 1.5, 3.0]]
        L = cholesky(a)
        n = len(a)
        recon = [[sum(L[i][m] * L[j][m] for m in range(n)) for j in range(n)]
                 for i in range(n)]
        for i in range(n):
            for j in range(n):
                assert math.isclose(recon[i][j], a[i][j], abs_tol=1e-9)

    def test_lower_triangular(self):
        L = cholesky([[2.0, 0.5], [0.5, 1.0]])
        assert L[0][1] == 0.0
