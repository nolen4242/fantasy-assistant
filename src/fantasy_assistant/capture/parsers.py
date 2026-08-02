"""Parsers for raw CBS captures under data/raw/YYYY-MM-DD/.

Each parser returns plain dicts (no graph coupling) plus a reject list — every
line we could not parse is preserved for inspection, never silently dropped.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

TEAM_NAMES = [
    # longest-first so multi-word names match before substrings could
    "Simba's Dublin Green Sox", "Like a Nightmare", "Gashouse Gang",
    "Rieken Havoc", "Runtime Terror", "Young Guns", "Big Sticks", "Long Balls",
    "Maga Doge", "Magnum GI", "Guillotine", "Dawg", "Trex",
]

ACTIONS = [
    "Added off Waivers", "Added", "Dropped", "Moved to IR", "Activated",
    "Moved to Minors", "Called Up",
]

_TRADE_RE = re.compile(r"Traded (?:from|to) (.+)$")


def normalize_name(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace(".", "").replace("'", "").strip()
    return re.sub(r"\s+", " ", s)


def uid_hash(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

@dataclass
class TxnAction:
    player_name: str
    positions: str            # "3B,SS" as CBS renders
    mlb_team: str
    action: str               # normalized action verb
    trade_counterparty: str | None = None


@dataclass
class Txn:
    posted_at: str            # ISO local ET as captured
    team: str
    actions: list[TxnAction]
    effective_date: str | None
    cost: float | None
    raw: str
    uid: str = ""

    def __post_init__(self):
        if not self.uid:
            self.uid = "cbs:txn:" + uid_hash(self.posted_at, self.team, self.raw)


_DATE_RE = re.compile(r"^(\d{1,2}/\d{1,2}/\d{2}) (\d{1,2}:\d{2} [AP]M) ET (.+)$")
_TAIL_RE = re.compile(r"\s*(\d{1,2}/\d{1,2}/\d{2})?\s*(?:\$(\d+\.\d{2}))?\s*$")
# player segment: "Name POS[,POS...] •|| TEAM - Action"  (separator may be mangled)
_SEG_RE = re.compile(
    r"(?P<name>[A-Z][^|•�]*?)\s+(?P<pos>[A-Z0-9]{1,2}(?:,[A-Z0-9]{1,2})*)\s*[•�|]\s*"
    r"(?P<team>[A-Z]{2,3})\s*-?\s*"
    r"(?P<action>Added off Waivers|Added|Dropped|Moved to IR|Move to Injured|"
    r"Moved to Minors|Sent to Minors|Activated|Called Up|"
    r"Traded (?:from|to) [A-Za-z' ]+?)(?=\s+[A-Z][a-z'.]|\s*$)"
)

_ACTION_NORM = {
    "Move to Injured": "Moved to IR",
    "Sent to Minors": "Moved to Minors",
}


def parse_posted_at(date_s: str, time_s: str) -> str:
    dt = datetime.strptime(f"{date_s} {time_s}", "%m/%d/%y %I:%M %p")
    return dt.isoformat()


def _parse_segments(body: str) -> list[TxnAction]:
    actions = []
    for seg in _SEG_RE.finditer(body):
        action_text = seg.group("action")
        trade_cp = None
        tm = _TRADE_RE.match(action_text)
        if tm:
            fragment = tm.group(1).strip()
            # regex lookahead can clip multi-word team names ("Simba's ...");
            # resolve against the known team list by prefix
            trade_cp = next((t for t in TEAM_NAMES if t.startswith(fragment)
                             or fragment.startswith(t)), fragment)
            action_text = "Traded"
        actions.append(TxnAction(
            player_name=seg.group("name").strip(),
            positions=seg.group("pos"),
            mlb_team=seg.group("team"),
            action=_ACTION_NORM.get(action_text, action_text),
            trade_counterparty=trade_cp,
        ))
    return actions


def _split_tail(body: str) -> tuple[str, str | None, float | None]:
    tail = _TAIL_RE.search(body)
    if tail and (tail.group(1) or tail.group(2)):
        eff = tail.group(1)
        cost = float(tail.group(2)) if tail.group(2) else None
        return body[: tail.start()].strip(), eff, cost
    return body, None, None


def parse_transactions(path: Path) -> tuple[list[Txn], list[str]]:
    """CBS renders one transaction across 1..n lines: the first line carries
    the timestamp and team; combined add/drop rows continue on bare lines
    that carry the remaining player-action segments and the effective/cost
    tail. Continuation lines are folded into the preceding transaction."""
    txns: list[Txn] = []
    rejects: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        m = _DATE_RE.match(line)
        if m:
            date_s, time_s, rest = m.groups()
            team = next((t for t in TEAM_NAMES if rest.startswith(t)), None)
            if team is None:
                rejects.append(line)
                continue
            body, effective, cost = _split_tail(rest[len(team):].strip())
            actions = _parse_segments(body)
            if not actions:
                rejects.append(line)
                continue
            eff_iso = None
            if effective:
                eff_iso = datetime.strptime(effective, "%m/%d/%y").date().isoformat()
            txns.append(Txn(
                posted_at=parse_posted_at(date_s, time_s), team=team,
                actions=actions, effective_date=eff_iso, cost=cost, raw=line,
            ))
        elif txns:
            # candidate continuation line: player-action segments + optional tail
            body, effective, cost = _split_tail(line)
            actions = _parse_segments(body)
            if not actions:
                continue  # boilerplate line, not part of a transaction
            cur = txns[-1]
            cur.actions.extend(actions)
            cur.raw += " || " + line
            if effective:
                cur.effective_date = datetime.strptime(
                    effective, "%m/%d/%y").date().isoformat()
            if cost is not None:
                cur.cost = cost
    return txns, rejects


# ---------------------------------------------------------------------------
# Free-agent pool PSV (batters + pitchers)
# ---------------------------------------------------------------------------

@dataclass
class PoolRow:
    cbs_id: str | None
    add_pos: str | None
    avail: str                # "FA" or "W"
    waiver_clear: str | None  # "8/4" style when on waivers
    player_name: str
    positions: str
    mlb_team: str
    stats: dict = field(default_factory=dict)
    rank: int | None = None


_PLAYER_CELL_RE = re.compile(
    r"^(?P<name>.+?)\s+(?P<pos>[A-Z0-9]{1,3}(?:,[A-Z0-9]{1,3})*)\s*[•�]\s*(?P<team>[A-Z]{2,3})$"
)
_WAIVER_RE = re.compile(r"^W \((\d{1,2}/\d{1,2})\)$")

BAT_COLS = ["ab", "r", "h", "b1", "b2", "b3", "hr", "rbi", "bb", "so", "sb", "cs",
            "avg", "obp", "slg"]
PIT_COLS = ["inns", "app", "gs", "qs", "cg", "w", "l", "sv", "bs", "hld", "k",
            "bbi", "ha", "era", "whip"]


def parse_pool(path: Path, kind: str) -> tuple[list[PoolRow], list[str]]:
    cols = BAT_COLS if kind == "bat" else PIT_COLS
    rows: list[PoolRow] = []
    rejects: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split("|")
        if len(parts) != 5 + len(cols) or parts[3] in ("Player",):
            if line.strip() and "Avail|Player" not in line:
                rejects.append(line)
            continue
        cbs_id, add_pos, avail_raw, player_cell = parts[0], parts[1], parts[2], parts[3]
        pm = _PLAYER_CELL_RE.match(player_cell.strip())
        if not pm:
            rejects.append(line)
            continue
        wm = _WAIVER_RE.match(avail_raw.strip())
        try:
            rank = int(parts[-1])
        except ValueError:
            rank = None
        stats = {}
        for col, val in zip(cols, parts[4:4 + len(cols)]):
            try:
                stats[col] = float(val)
            except ValueError:
                stats[col] = None
        rows.append(PoolRow(
            cbs_id=cbs_id or None, add_pos=add_pos or None,
            avail="W" if wm else avail_raw.strip(),
            waiver_clear=wm.group(1) if wm else None,
            player_name=pm.group("name").strip(), positions=pm.group("pos"),
            mlb_team=pm.group("team"), stats=stats, rank=rank,
        ))
    return rows, rejects


# ---------------------------------------------------------------------------
# Standings file (our transcribed format)
# ---------------------------------------------------------------------------

def parse_standings(path: Path) -> dict:
    """Returns {overall: [{team, batting, pitching, total}], categories:
    {CODE: [{team, value, points}]}} from standings_overall.txt."""
    text = path.read_text(encoding="utf-8")
    result: dict = {"overall": [], "categories": {}}
    section = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("OVERALL"):
            section = "overall"
            continue
        m = re.match(r"^(?:BATTING|PITCHING): ([A-Z]+)$", line)
        if m:
            section = m.group(1)
            result["categories"][section] = []
            continue
        if not line or section is None:
            continue
        if section == "overall":
            om = re.match(r"^\d+ (.+?) ([\d.]+) ([\d.]+) ([\d.]+) ", line)
            if om:
                result["overall"].append({
                    "team": om.group(1), "batting": float(om.group(2)),
                    "pitching": float(om.group(3)), "total": float(om.group(4)),
                })
        elif section in result["categories"]:
            for chunk in line.split("|"):
                cm = re.match(r"^\s*(.+?) ([.\d]+) ([\d.]+)\s*$", chunk)
                if cm:
                    result["categories"][section].append({
                        "team": cm.group(1).strip(),
                        "value": float(cm.group(2)),
                        "points": float(cm.group(3)),
                    })
    return result


# ---------------------------------------------------------------------------
# Roster grid (our transcribed format)
# ---------------------------------------------------------------------------

@dataclass
class GridEntry:
    team: str
    slot_group: str           # C/1B/.../U/P
    label: str                # "D Rushing" as rendered
    status: str               # active|reserve|il|minors


_TAG_MAP = {"R": "reserve", "I": "il", "M": "minors"}


def parse_roster_grid(path: Path) -> list[GridEntry]:
    entries: list[GridEntry] = []
    team = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("source:", "captured:", "legend:")):
            continue
        if line in TEAM_NAMES:
            team = line
            continue
        if team is None:
            continue
        for part in line.split(" | "):
            if ":" not in part:
                continue
            slot, _, names = part.partition(":")
            for raw_name in names.split(","):
                raw_name = raw_name.strip()
                if not raw_name:
                    continue
                status = "active"
                tag = re.search(r"\(([RIM])\)$", raw_name)
                if tag:
                    status = _TAG_MAP[tag.group(1)]
                    raw_name = raw_name[: tag.start()].strip()
                entries.append(GridEntry(team=team, slot_group=slot.strip(),
                                         label=raw_name, status=status))
    return entries


# ---------------------------------------------------------------------------
# Draft results (our transcribed format)
# ---------------------------------------------------------------------------

@dataclass
class DraftPickRow:
    round: int
    pick_in_round: int
    overall: int
    team: str
    player_name: str
    positions: str
    mlb_team: str
    auto: bool
    queued: bool


_DRAFT_LINE_RE = re.compile(
    r"^(?P<pick>\d{1,2}) (?P<rest>.+?) (?P<pos>[A-Z0-9]{1,3}(?:,[A-Z0-9]{1,3})*) [•�] (?P<mlb>[A-Z]{2,3})\b"
)


def parse_draft(path: Path) -> tuple[list[DraftPickRow], list[str]]:
    picks: list[DraftPickRow] = []
    rejects: list[str] = []
    rnd = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        rm = re.match(r"^ROUND (\d+)$", line)
        if rm:
            rnd = int(rm.group(1))
            continue
        if not rnd or not line or line.startswith("==="):
            if line.startswith("==="):
                break  # chat log section
            continue
        m = _DRAFT_LINE_RE.match(line)
        if not m:
            rejects.append(f"R{rnd}: {line}")
            continue
        rest = m.group("rest")
        team = next((t for t in TEAM_NAMES if rest.startswith(t)), None)
        if team is None:
            rejects.append(f"R{rnd}: {line}")
            continue
        name = rest[len(team):].strip()
        auto = name.startswith("*") and not name.startswith("**")
        queued = name.startswith("**")
        name = name.lstrip("*").strip()
        pick = int(m.group("pick"))
        picks.append(DraftPickRow(
            round=rnd, pick_in_round=pick, overall=(rnd - 1) * 13 + pick,
            team=team, player_name=name, positions=m.group("pos"),
            mlb_team=m.group("mlb"), auto=auto, queued=queued,
        ))
    return picks, rejects
