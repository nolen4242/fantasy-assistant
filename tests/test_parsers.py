"""Golden-file tests: the committed capture under data/raw is the fixture.

These pin the parser contracts that everything downstream assumes: zero
rejects, exact transaction count, one fee per logical transaction (issue
uncovered in the ask-box gauntlet: fees were double-counted when uids were
raw-line hashes), and format-independent event identity.
"""
from pathlib import Path

import pytest

from fantasy_assistant.capture import parsers

# FROZEN golden fixture — a specific committed capture, never "latest":
# live captures grow (new transactions daily) and would drift the pinned counts
RAW = Path(__file__).resolve().parents[1] / "data" / "raw" / "2026-08-02"


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


class TestStandingsTranscription:
    """The transcribed standings file used to be produced by hand; these pin
    the derivation against the hand-made files it replaces."""

    # every committed capture that carries both the raw page and the
    # hand-transcribed file it was reduced to
    ROOT = RAW.parent
    DAYS = sorted(
        d.name for d in ROOT.iterdir()
        if (d / "standings_overall.txt").exists()
        and (d / "standings_overall_raw.txt").exists()
    )

    # the two earliest captures were transcribed by hand in a looser header
    # style ("~11:35 ET", "through PERIOD 19"), and 08-02's raw page was
    # fetched hours after its transcription — so byte equality is only
    # meaningful once the header convention settled
    FREEFORM_HEADER_DAYS = {"2026-08-02", "2026-08-03"}

    def _derive(self, day, url=""):
        return parsers.transcribe_standings(
            (self.ROOT / day / "standings_overall_raw.txt").read_text(), day, url)

    def test_derived_standings_match_handmade_data(self):
        """The numbers must agree on every day, header prose aside."""
        assert self.DAYS, "no capture carries both raw and transcribed standings"
        for day in self.DAYS:
            tmp = self.ROOT / day / ".transcribe_cmp.txt"
            try:
                tmp.write_text(self._derive(day))
                got = parsers.parse_standings(tmp)
            finally:
                tmp.unlink(missing_ok=True)
            want = parsers.parse_standings(self.ROOT / day / "standings_overall.txt")
            assert got["overall"] == want["overall"], day
            assert got["categories"] == want["categories"], day

    def test_reproduces_handmade_transcriptions_byte_for_byte(self):
        days = [d for d in self.DAYS if d not in self.FREEFORM_HEADER_DAYS]
        assert days, "no capture with a settled-convention transcription"
        for day in days:
            got = self._derive(
                day, "https://buecker.baseball.cbssports.com/standings/overall")
            assert got == (self.ROOT / day / "standings_overall.txt").read_text(), day

    def test_transcription_round_trips_through_parser(self):
        day = self.DAYS[-1]
        text = parsers.transcribe_standings(
            (self.ROOT / day / "standings_overall_raw.txt").read_text(), day)
        tmp = self.ROOT / day / ".transcribe_roundtrip.txt"
        try:
            tmp.write_text(text)
            data = parsers.parse_standings(tmp)
            assert len(data["overall"]) == 13
            assert len(data["categories"]) == 10
            for code, lines in data["categories"].items():
                assert len(lines) == 13, f"{code}: 13-team league"
        finally:
            tmp.unlink(missing_ok=True)
