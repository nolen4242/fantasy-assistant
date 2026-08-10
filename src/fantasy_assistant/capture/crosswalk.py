"""MLBAM identity crosswalk.

Fetches the active-player universe from the MLB Stats API (public, no auth)
and attaches mlbam_id + primary position + MLB team to Player nodes, matching
on normalized name with position-type and team disambiguation for collisions.

Unmatched or ambiguous cases are returned, never guessed (SCHEMA: unresolved
identity becomes a Discrepancy, not a wrong edge).
"""
from __future__ import annotations

import httpx

from fantasy_assistant.capture.parsers import normalize_name
from fantasy_assistant.graph.client import session

STATSAPI = "https://statsapi.mlb.com/api/v1"

# CBS abbreviation -> MLB abbreviation, where they differ
CBS_TO_MLB_ABBREV = {"CHW": "CWS", "WAS": "WSH"}


def fetch_mlb_universe(season: int = 2026) -> list[dict]:
    from fantasy_assistant.capture.http import get_json
    with httpx.Client(timeout=30) as client:
        teams = get_json(f"{STATSAPI}/teams", params={"sportId": 1, "season": season}, client=client)["teams"]
        team_abbrev = {t["id"]: t["abbreviation"] for t in teams}
        players = client.get(f"{STATSAPI}/sports/1/players", params={"season": season}).json()["people"]
    out = []
    for p in players:
        out.append({
            "mlbam_id": p["id"],
            "name": p["fullName"],
            "norm": normalize_name(p["fullName"]),
            "position": (p.get("primaryPosition") or {}).get("abbreviation"),
            "is_pitcher": (p.get("primaryPosition") or {}).get("type") == "Pitcher",
            "team": team_abbrev.get((p.get("currentTeam") or {}).get("id")),
            "bats": (p.get("batSide") or {}).get("code"),
            "throws": (p.get("pitchHand") or {}).get("code"),
            "birthdate": p.get("birthDate"),
        })
    return out


def _cbs_is_pitcher(cbs_positions: str | None) -> bool | None:
    if not cbs_positions:
        return None
    toks = set(cbs_positions.split(","))
    if toks <= {"P", "SP", "RP"}:
        return True
    return False


_SUFFIXES = (" jr", " sr", " ii", " iii", " iv")


def _strip_suffix(norm: str) -> str:
    for suf in _SUFFIXES:
        if norm.endswith(suf):
            return norm[: -len(suf)]
    return norm


def _last_first(norm: str) -> tuple[str, str]:
    parts = _strip_suffix(norm).split()
    return (parts[-1] if parts else "", parts[0] if parts else "")


def search_mlb_player(client: httpx.Client, name: str, season: int | None) -> list[dict]:
    params = {"names": name, "sportIds": 1}
    if season is not None:
        params["seasons"] = season
    r = client.get(f"{STATSAPI}/people/search", params=params)
    people = r.json().get("people", []) if r.status_code == 200 else []
    return [{
        "mlbam_id": p["id"], "name": p["fullName"],
        "norm": normalize_name(p["fullName"]),
        "position": (p.get("primaryPosition") or {}).get("abbreviation"),
        "is_pitcher": (p.get("primaryPosition") or {}).get("type") == "Pitcher",
        "team": None,
        "bats": (p.get("batSide") or {}).get("code"),
        "throws": (p.get("pitchHand") or {}).get("code"),
        "birthdate": p.get("birthDate"),
    } for p in people]


def run_crosswalk(season: int = 2026) -> dict:
    universe = fetch_mlb_universe(season)
    by_norm: dict[str, list[dict]] = {}
    for m in universe:
        by_norm.setdefault(m["norm"], []).append(m)

    with session() as s:
        graph_players = s.run(
            "MATCH (p:Player) RETURN p.uid AS uid, p.name_normalized AS norm, "
            "p.cbs_positions AS pos, p.cbs_mlb_team AS team"
        ).data()

        # pass 2/3 indexes: suffix-stripped exact, and (last, first-initial)
        by_stripped: dict[str, list[dict]] = {}
        by_last_fi: dict[tuple[str, str], list[dict]] = {}
        for m in universe:
            by_stripped.setdefault(_strip_suffix(m["norm"]), []).append(m)
            last, first = _last_first(m["norm"])
            by_last_fi.setdefault((last, first[:1]), []).append(m)

        # must stay in step with identity._CHECKS['rostered_without_mlbam']:
        # a player who only ever appears in a lineup (never drafted, never an
        # add we captured) is still ours, and without this clause he never
        # reaches the fuzzy/people-search passes and stays mlbam-less forever
        rostered = {
            r["uid"] for r in s.run(
                "MATCH (p:Player) WHERE (p)<-[:SELECTED]-(:DraftPick) "
                "OR (p)<-[:ADDS]-(:TransactionEvent) "
                "OR (p)<-[:FILLED_BY]-(:LineupAssignment {section:'active'}) "
                "RETURN p.uid AS uid"
            ).data()
        }

        # Nodes minted by identity.split_pool_collisions ('<uid>_bat',
        # '<uid>_pit', '<uid>_cbs<id>') are the SECOND human behind a shared
        # name. Name-matching them just re-finds the first human's MLB record,
        # so they are excluded here and resolved only by explicit override.
        from fantasy_assistant.graph import identity as _identity
        split_uids: set[str] = set()
        if _identity._splits_path().exists():
            import json as _json
            split_uids = set(_json.loads(_identity._splits_path().read_text()).values())

        matched, ambiguous, unmatched = [], [], []

        def narrow(cands: list[dict], gp: dict) -> list[dict]:
            if len(cands) <= 1:
                return cands
            want_pitcher = _cbs_is_pitcher(gp["pos"])
            mlb_team = CBS_TO_MLB_ABBREV.get(gp["team"], gp["team"])
            narrowed = [c for c in cands if want_pitcher is None or c["is_pitcher"] == want_pitcher]
            if len(narrowed) > 1:
                narrowed = [c for c in narrowed if c["team"] == mlb_team]
            return narrowed

        # match strength, so a collision can be resolved in favour of the
        # stronger evidence: 0 exact name, 1 suffix-stripped, 2 last+initial,
        # 3 people-search
        strength: dict[str, int] = {}

        for gp in graph_players:
            if gp["uid"] in split_uids:
                unmatched.append(gp["uid"])
                continue
            # CBS renders Ohtani as two entities; strip the qualifier for matching
            norm = (gp["norm"] or "").replace(" (batter)", "").replace(" (pitcher)", "")
            cands = narrow(by_norm.get(norm, []), gp)
            rank = 0
            if not cands and gp["uid"] in rostered:
                # fuzzy passes are rostered-only: on the 8k pool longtail they
                # mass-assign wrong ids (seth_gray -> Sonny Gray's mlbam)
                cands = narrow(by_stripped.get(_strip_suffix(norm), []), gp)
                rank = 1
                if not cands:
                    last, first = _last_first(norm)
                    cands = narrow(by_last_fi.get((last, first[:1]), []), gp)
                    rank = 2
            if len(cands) == 1:
                matched.append((gp["uid"], cands[0]))
                strength[gp["uid"]] = rank
            elif len(cands) > 1:
                ambiguous.append(gp["uid"])
            else:
                unmatched.append(gp["uid"])

        # One mlbam id belongs to exactly one human. Two nodes claiming the
        # same one means a fuzzy pass guessed (joshua_baez -> Javier Baez).
        # Keep only the strongest claim, and only when it is strictly stronger
        # than the runner-up — an unresolved identity is a discrepancy, never
        # a wrong edge. Runs BEFORE the people-search pass so a rejected guess
        # still gets its shot at the authoritative lookup.
        rejected: set[str] = set()
        collisions: list[dict] = []

        def resolve_collisions() -> None:
            by_mlbam: dict[int, list[str]] = {}
            for uid_, m_ in matched:
                by_mlbam.setdefault(m_["mlbam_id"], []).append(uid_)
            for mlbam, uids_ in by_mlbam.items():
                if len(uids_) < 2 or any("ohtani" in u for u in uids_):
                    continue
                ranked = sorted(uids_, key=lambda u: strength.get(u, 99))
                best = strength.get(ranked[0], 99)
                keep = ranked[0] if strength.get(ranked[1], 99) > best else None
                losers = [u for u in uids_ if u != keep]
                rejected.update(losers)
                collisions.append({"mlbam_id": mlbam, "uids": uids_, "kept": keep})
            for uid_ in rejected:
                matched[:] = [(u, m) for u, m in matched if u != uid_]
                strength.pop(uid_, None)
                if uid_ not in unmatched:
                    unmatched.append(uid_)

        resolve_collisions()

        # pass 4: MLB people-search for rostered players still unmatched
        # (60-day IL and minors players are absent from the actives list)
        still = [uid for uid in unmatched
                 if uid in rostered and uid not in split_uids]
        if still:
            names = {
                r["uid"]: (r["name"], r)
                for r in s.run(
                    "MATCH (p:Player) WHERE p.uid IN $uids RETURN p.uid AS uid, "
                    "p.name_full AS name, p.cbs_positions AS pos, p.cbs_mlb_team AS team",
                    uids=still,
                ).data()
            }
            with httpx.Client(timeout=30) as client:
                for uid, (name, gp) in names.items():
                    cands = narrow(search_mlb_player(client, name, season), gp)
                    if not cands:
                        # season-long IL / minors players fall outside the
                        # season roster filter; retry unfiltered
                        cands = narrow(search_mlb_player(client, name, None), gp)
                    if len(cands) == 1:
                        matched.append((uid, cands[0]))
                        strength[uid] = 3
                        rejected.discard(uid)
                        unmatched.remove(uid)

        # second pass: the people-search results can themselves collide
        resolve_collisions()
        rejected -= {uid for uid, _ in matched}

        # a rejected claim may already be on the node from an earlier run
        if rejected:
            s.run(
                "MATCH (p:Player) WHERE p.uid IN $uids "
                "SET p.mlbam_id = NULL, p.primary_position = NULL",
                uids=sorted(rejected),
            )

        s.run(
            """
            UNWIND $batch AS row
            MATCH (p:Player {uid: row.uid})
            SET p.mlbam_id = row.m.mlbam_id, p.primary_position = row.m.position,
                p.mlb_team_current = row.m.team, p.bats = row.m.bats,
                p.throws = row.m.throws, p.birthdate = row.m.birthdate
            """,
            batch=[{"uid": uid, "m": m} for uid, m in matched],
        )
    n_over = apply_overrides()
    return {
        "overrides_applied": n_over,
        "mlb_universe": len(universe),
        "graph_players": len(graph_players),
        "matched": len(matched),
        "ambiguous": ambiguous,
        "unmatched_count": len(unmatched),
        "mlbam_collisions": collisions,
        "collision_claims_dropped": len(rejected),
    }


# Hand-verified identity overrides for name collisions the automated passes
# can't safely resolve (same name, same position type, multiple MLB players).
# Keyed by (normalized CBS name, CBS MLB team) -> mlbam_id.
OVERRIDES_BY_UID = {
    "player:name:luis_garcia": {"mlbam_id": 671277, "primary_position": "2B"},
    "player:name:max_muncy": {"mlbam_id": 571970, "primary_position": "3B"},
    "player:name:max_muncy_ath": {"mlbam_id": 691777, "primary_position": "3B"},
}


def apply_overrides() -> int:
    with session() as s:
        n = 0
        for uid, props in OVERRIDES_BY_UID.items():
            n += s.run("MATCH (p:Player {uid:$uid}) SET p += $props RETURN count(p) AS c",
                       uid=uid, props=props).single()["c"]
    return n


if __name__ == "__main__":
    import json
    print(json.dumps(run_crosswalk(), indent=1, default=str)[:2000])
