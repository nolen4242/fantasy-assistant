"""Pure-function tests for the v2 trade machinery (no DB required)."""
from fantasy_assistant.analytics.lineup import (
    COUNTING, RATE_COMPS, eligible, team_totals)
from fantasy_assistant.analytics.trades import _points_for_values, _rate_value


def _mk(uid, pos, status="active", fa=False, **units):
    proj = {c: 0.0 for c in COUNTING + RATE_COMPS}
    proj.update(units)
    return {"uid": uid, "name": uid, "pos": set(pos), "status": status,
            "proj": proj, "fa": fa, "season": {}}


SCALES = {c: 100.0 for c in COUNTING}


# ---- roto points ---------------------------------------------------------

def test_points_basic_and_direction():
    vals = [10.0, 30.0, 20.0]
    assert _points_for_values(vals, higher_better=True) == [1.0, 3.0, 2.0]
    assert _points_for_values(vals, higher_better=False) == [3.0, 1.0, 2.0]


def test_points_ties_share():
    # two teams tied for best of three: they share (3+2)/2 = 2.5 each
    assert _points_for_values([5.0, 5.0, 1.0]) == [2.5, 2.5, 1.0]


# ---- eligibility ---------------------------------------------------------

def test_eligibility():
    assert eligible({"2B", "SS"}, "MI")
    assert eligible({"1B"}, "CI")
    assert not eligible({"OF"}, "MI")
    assert eligible({"OF"}, "U")
    assert not eligible({"P"}, "U")


# ---- assignment: displacement and replacement ----------------------------

def test_gained_player_only_adds_margin_over_displaced():
    # one OF slot world isn't expressible, but U vs OF displacement is:
    # two OF-only bats compete; only the better plus the rest of an empty
    # roster start, so team HR = sum of starters, not sum of roster.
    a = _mk("a", {"OF"}, HR=10)
    b = _mk("b", {"OF"}, HR=8)
    base = team_totals([a], SCALES)["totals"]["HR"]
    after = team_totals([a, b], SCALES)["totals"]["HR"]
    # both start (4 OF slots + U), so this ADDS fully...
    assert after == 18
    # ...but with the OF slots already saturated, a 5th OF adds nothing:
    ofs = [_mk(f"of{i}", {"OF"}, HR=20) for i in range(5)] + [a, b]
    full = team_totals(ofs, SCALES)["totals"]["HR"]
    # 4 OF slots + U take the five 20-HR bats; a and b ride the bench
    assert full == 100


def test_il_players_do_not_start():
    p = _mk("hurt", {"OF"}, status="il", HR=50)
    assert team_totals([p], SCALES)["totals"]["HR"] == 0


def test_fa_quota_caps_replacement_bodies():
    fa1 = _mk("fa1", {"P"}, fa=True, K=30)
    fa2 = _mk("fa2", {"P"}, fa=True, K=25)
    own = _mk("own", {"P"}, K=1)
    r = team_totals([own], SCALES, fa_quota=1, fa_candidates=[fa1, fa2])
    # only one FA may join despite two open P slots being available
    assert r["fa_used"] == 1
    assert r["totals"]["K"] == 31


def test_pitcher_slots_capped_at_ten():
    arms = [_mk(f"p{i}", {"P"}, K=10 + i) for i in range(12)]
    r = team_totals(arms, SCALES)
    # the two weakest arms (K=10, 11) sit
    assert r["totals"]["K"] == sum(10 + i for i in range(2, 12))


# ---- rate math -----------------------------------------------------------

def test_rate_value_era_blend():
    # ytd 3.00 ERA on uniform denominator; ROS adds 90 IP (270 outs) of
    # 0.00 ERA -> final must drop below 3.00 but stay above 0
    comps = {"er": 0.0, "outs": 270.0, "ob": 0, "paden": 0, "wh": 0}
    v = _rate_value("ERA", 3.00, comps)
    assert 0 < v < 3.00


def test_rate_value_whip_direction():
    # adding walks+hits with zero innings must raise WHIP
    base = _rate_value("WHIP", 1.20, {"wh": 0, "outs": 0, "ob": 0,
                                      "paden": 0, "er": 0})
    worse = _rate_value("WHIP", 1.20, {"wh": 50.0, "outs": 0, "ob": 0,
                                       "paden": 0, "er": 0})
    assert worse > base


def test_rate_value_obp():
    # ytd .340; ROS 600 PA at .400 OBP pulls the final up
    comps = {"ob": 240.0, "paden": 600.0, "er": 0, "outs": 0, "wh": 0}
    v = _rate_value("OBP", 0.340, comps)
    assert 0.340 < v < 0.400
