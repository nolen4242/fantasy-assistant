"""Golden-file tests: the committed capture under data/raw is the fixture.

These pin the parser contracts that everything downstream assumes: zero
rejects, exact transaction count, one fee per logical transaction (issue
uncovered in the ask-box gauntlet: fees were double-counted when uids were
raw-line hashes), and format-independent event identity.
"""
from pathlib import Path

import pytest

from fantasy_assistant.capture import parsers

RAW = sorted((Path(__file__).resolve().parents[1] / "data" / "raw").iterdir())[-1]


@pytest.fixture(scope="module")
def txns():
    txns, rejects = parsers.sniff_and_parse_transactions(RAW / "transactions_all_raw.txt")
    assert rejects == []
    return txns


class TestTransactions:
    def test_full_season_parses(self, txns):
        assert len(txns) == 521

    def test_lan_fee_ledger(self, txns):
        lan = [t for t in txns if t.team == "Like a Nightmare"]
        assert len(lan) == 54
        assert sum(t.cost or 0 for t in lan) == 112.5

    def test_uid_is_format_independent(self, txns):
        # rewriting the raw line (v1 vs v2 delimiters) must NOT change uid
        t = txns[0]
        clone = parsers.Txn(posted_at=t.posted_at, team=t.team, actions=t.actions,
                            effective_date=t.effective_date, cost=t.cost,
                            raw="TOTALLY DIFFERENT RAW ENCODING " + t.raw)
        assert clone.uid == t.uid

    def test_uids_unique(self, txns):
        uids = [t.uid for t in txns]
        assert len(uids) == len(set(uids))

    def test_trades_have_counterparty(self, txns):
        trade_actions = [a for t in txns for a in t.actions if a.action == "Traded"]
        assert trade_actions, "season contains trades"
        assert all(a.trade_counterparty for a in trade_actions)


class TestPool:
    @pytest.mark.parametrize("stem,kind", [("fa_pool_batters", "bat"),
                                           ("fa_pool_pitchers", "pit")])
    def test_pool_parses_clean(self, stem, kind):
        f = RAW / f"{stem}.psv"
        if not f.exists():
            f = sorted(RAW.glob(f"{stem}_period*.psv"))[-1]
        rows, rejects = parsers.parse_pool(f, kind)
        assert rejects == []
        assert len(rows) > 1000

    def test_normalize_name_unicode(self):
        assert parsers.normalize_name("Julio Rodríguez") == parsers.normalize_name("Julio Rodriguez")
        assert parsers.normalize_name("  Luis  García Jr. ") == parsers.normalize_name("Luis Garcia Jr.")


class TestDraft:
    def test_all_picks(self):
        picks, rejects = parsers.parse_draft(RAW / "draft_results.txt")
        assert rejects == []
        assert len(picks) == 299


class TestStandings:
    def test_categories_present(self):
        data = parsers.parse_standings(RAW / "standings_overall.txt")
        cats = set(data["categories"])
        assert {"HR", "SB", "OBP", "ERA", "WHIP", "K"} <= cats
        for code, lines in data["categories"].items():
            assert len(lines) == 13, f"{code}: 13-team league"
