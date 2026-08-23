"""Dedicated CBS capture runner (Playwright, persisted session).

The user logs in ONCE via a headed browser window (`... runner login`); the
session persists in a local browser profile (never in git, never in code —
the system does not see or store credentials). After that, `... runner
snapshot` captures the full league state headlessly into data/raw/<today>/.

Usage:
    python -m fantasy_assistant.capture.runner login
    python -m fantasy_assistant.capture.runner snapshot
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

from fantasy_assistant.capture import parsers
from fantasy_assistant.graph.refdata import period_for_date

BASE = "https://buecker.baseball.cbssports.com"
PROFILE_DIR = Path.home() / ".fantasy-assistant" / "cbs-profile"
RAW_ROOT = Path(__file__).resolve().parents[3] / "data" / "raw"

# path -> output filename; {period} entries expand per period
PAGES = {
    "/transactions?print_rows=9999": "transactions_all_raw.txt",
    "/teams/roster-grid": "roster_grid.txt",
    "/standings/overall": "standings_overall_raw.txt",
    "/teams": "my_team_raw.txt",
    "/scoring/standard": "live_scoring_raw.txt",
    # pending trade offers (both directions); the /teams header only carries a
    # count badge, so without this page offers are invisible to the pipeline
    "/transactions/trade?show_pending=1": "pending_trades_raw.txt",
}

TABLE_EXTRACT_JS = """
() => {
  const rows = [];
  for (const tr of document.querySelectorAll('table tr')) {
    const cells = tr.cells;
    if (!cells || cells.length < 2) continue;
    const vals = [];
    for (const c of cells) {
      const action = c.querySelector('a[href*="default_add="]');
      if (action) {
        const m = (c.innerHTML || '').match(/default_add=([A-Z0-9]+):(\\d+)/);
        vals.push(m ? m[2] : ''); vals.push(m ? m[1] : '');
      } else {
        // innerText preserves line breaks between players/actions within a
        // cell; join them with '; ' so multi-entry cells stay parseable
        vals.push(c.innerText.trim().replace(/\\s*\\n\\s*/g, '; ').replace(/[ \\t]+/g, ' '));
      }
    }
    rows.push(vals.join('|'));
  }
  return rows.join('\\n');
}
"""


def login() -> None:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            PROFILE_DIR, headless=False, channel="chromium",
            viewport={"width": 1400, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(BASE)
        print("A browser window is open. Log in to CBS, wait until the league")
        print("home page loads, then close the window. The session persists in")
        print(f"{PROFILE_DIR} (local only).")
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass
        ctx.close()


def _logged_in(page) -> bool:
    return "login" not in page.url


def snapshot(out_dir: Path | None = None) -> None:
    out = out_dir or (RAW_ROOT / date.today().isoformat())
    out.mkdir(parents=True, exist_ok=True)
    captured: list[tuple[str, str, int]] = []
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(PROFILE_DIR, headless=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        page.goto(BASE, wait_until="domcontentloaded")
        if not _logged_in(page):
            sys.exit("Session expired — run `python -m fantasy_assistant.capture.runner login` first.")

        period = period_for_date(date.today())
        if date.today().weekday() == 6:  # Sunday: SportsLine shows next week
            period += 1
        pages = dict(PAGES)
        pages["/stats/stats-main?print_rows=9999"] = "fa_pool_batters.psv"
        pages[f"/stats/stats-main/fa:P/period-{period}:p/standard/projections?print_rows=9999"] = "fa_pool_pitchers.psv"

        for path, fname in pages.items():
            # the stats-main pool dumps render 44 pages of rows and routinely
            # blow past the default 30s goto timeout (three -1 captures the
            # week of 8/22); give them longer and retry every page once
            slow = "stats-main" in path
            for attempt in (1, 2):
                try:
                    page.goto(BASE + path, wait_until="domcontentloaded",
                              timeout=90_000 if slow else 30_000)
                    page.wait_for_timeout(2500 if slow else 1500)
                    text = page.evaluate(TABLE_EXTRACT_JS) if ("print_rows" in path or "roster" in path) \
                        else page.inner_text("body")
                    stamp = f"source: {BASE + path}\ncaptured: {datetime.now().isoformat(timespec='seconds')}\n---\n"
                    (out / fname).write_text(stamp + text)
                    captured.append((path, fname, len(text)))
                    break
                except Exception as exc:  # one bad page must not kill the snapshot
                    if attempt == 2:
                        captured.append((path, fname, -1))
                    print(f"  ERROR capturing {path} (attempt {attempt}): {exc}",
                          file=sys.stderr)

        # by-period standings: every closed period via the page's own select
        page.goto(BASE + "/standings/byperiod", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        hist = page.evaluate("""
            async () => {
              const sel = Array.from(document.querySelectorAll('select'))
                .find(s => Array.from(s.options).some(o => /Period \\d+ \\(/i.test(o.text)));
              if (!sel) return 'NO-PERIOD-SELECT';
              const chunks = [];
              for (const o of Array.from(sel.options).filter(o => /Period \\d+ \\(/.test(o.text))) {
                const r = await fetch(o.value, {credentials: 'same-origin'});
                const doc = new DOMParser().parseFromString(await r.text(), 'text/html');
                const rows = [];
                for (const tr of doc.querySelectorAll('table tr')) {
                  const cells = tr.cells;
                  if (!cells || cells.length < 3) continue;
                  const vals = [];
                  for (const c of cells) vals.push(c.textContent.trim().replace(/\\s+/g, ' '));
                  rows.push(vals.join('|'));
                }
                chunks.push('=== ' + o.text + ' ===\\n' + rows.join('\\n'));
              }
              return chunks.join('\\n\\n');
            }
        """)
        (out / "standings_byperiod_all.txt").write_text(hist)
        captured.append(("/standings/byperiod (all periods)", "standings_byperiod_all.txt", len(hist)))

        ctx.close()

    # the ingest parser reads a distilled transcription of the standings page;
    # derive it here so the capture is self-contained (it used to be hand-made,
    # and a missing transcription broke standings ingest silently)
    raw_standings = out / "standings_overall_raw.txt"
    if raw_standings.exists():
        try:
            (out / "standings_overall.txt").write_text(
                parsers.transcribe_standings(
                    raw_standings.read_text(), out.name, BASE + "/standings/overall")
            )
            captured.append(("(derived from standings_overall_raw.txt)",
                             "standings_overall.txt",
                             len((out / "standings_overall.txt").read_text())))
        except Exception as exc:
            print(f"  ERROR transcribing standings: {exc}", file=sys.stderr)
            captured.append(("(derived)", "standings_overall.txt", -1))

    manifest = out / "MANIFEST.md"
    lines = [f"# Raw capture manifest — {out.name} (runner)", ""]
    lines += [f"- `{f}` <- `{p}` ({n:,} chars)" for p, f, n in captured]
    manifest.write_text("\n".join(lines) + "\n")
    for p, f, n in captured:
        print(f"  {f:<32} {n:>9,} chars  <- {p}")
    print(f"snapshot complete: {out}")




# every artifact a complete daily capture leaves behind. draft_results.txt is
# deliberately absent: it is static pre-season data, not captured daily.
EXPECTED_ARTIFACTS = (
    "MANIFEST.md",
    "transactions_all_raw.txt",
    "roster_grid.txt",
    "standings_overall_raw.txt",
    "standings_overall.txt",
    "my_team_raw.txt",
    "live_scoring_raw.txt",
    "fa_pool_batters.psv",
    "fa_pool_pitchers.psv",
    "standings_byperiod_all.txt",
    "lineups_all.psv",
    "player_news_raw.txt",
)


def verify_capture(out_dir: Path | None = None) -> dict:
    """Report what a capture is missing.

    A crashed run still leaves a dated directory behind, so 'the directory
    exists' is not evidence the day was captured — 2026-08-04 held nothing
    but the news file and its FA-pool snapshot is gone for good. This checks
    contents instead: missing artifacts, empty files, and pages the manifest
    recorded as failed (-1 chars)."""
    out = out_dir or (RAW_ROOT / date.today().isoformat())
    missing = [f for f in EXPECTED_ARTIFACTS if not (out / f).exists()]
    empty = [f for f in EXPECTED_ARTIFACTS
             if (out / f).exists() and (out / f).stat().st_size == 0]
    failed = []
    manifest = out / "MANIFEST.md"
    if manifest.exists():
        failed = [l.strip() for l in manifest.read_text().splitlines()
                  if "(-1 chars)" in l]
    return {"dir": str(out), "exists": out.exists(), "missing": missing,
            "empty": empty, "failed_pages": failed,
            "complete": out.exists() and not missing and not empty and not failed}


def verify_all(root: Path | None = None) -> list[dict]:
    """verify_capture across every captured date, oldest first."""
    r = root or RAW_ROOT
    return [verify_capture(d) for d in sorted(r.iterdir()) if d.is_dir()]


TEAM_IDS = {  # discovered from the standings page team links, 2026 season
    "Like a Nightmare": 1, "Young Guns": 2, "Guillotine": 3, "Magnum GI": 4,
    "Rieken Havoc": 5, "Dawg": 7, "Gashouse Gang": 8, "Big Sticks": 9,
    "Long Balls": 10, "Maga Doge": 11, "Simba's Dublin Green Sox": 12,
    "Trex": 16, "Runtime Terror": 18,
}

LINEUP_FETCH_JS = """
async (pairs) => {
  const out = [];
  for (const [team, tid, period] of pairs) {
    const r = await fetch(`/teams/roster-report/${tid}/${period}/`, {credentials: 'same-origin'});
    const doc = new DOMParser().parseFromString(await r.text(), 'text/html');
    let section = 'active';
    for (const tr of doc.querySelectorAll('table tr')) {
      const cells = tr.cells; if (!cells || cells.length < 3) continue;
      const pos = cells[1].textContent.trim();
      const player = cells[2].textContent.trim().replace(/\\s+/g, ' ');
      if (pos === 'Pos') { section = 'active'; continue; }
      if (['Bench', 'Injured', 'Minors'].includes(pos)) {
        section = pos.toLowerCase(); continue;
      }
      if (!pos || player === 'EMPTY') continue;
      out.push([team, period, section, pos, player].join('|'));
    }
  }
  return out.join('\\n');
}
"""


def capture_lineups(out_dir: Path | None = None, through_period: int | None = None) -> None:
    """Capture per-period lineup assignments for every team, periods 1..N."""
    out = out_dir or (RAW_ROOT / date.today().isoformat())
    out.mkdir(parents=True, exist_ok=True)
    n = through_period or period_for_date(date.today())
    pairs = [[team, tid, p] for team, tid in TEAM_IDS.items() for p in range(1, n + 1)]
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(PROFILE_DIR, headless=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(BASE, wait_until="domcontentloaded")
        if not _logged_in(page):
            sys.exit("Session expired — run `runner login` first.")
        text = page.evaluate(LINEUP_FETCH_JS, pairs)
        ctx.close()
    stamp = (f"source: {BASE}/teams/roster-report/<team>/<period>/ (periods 1-{n})\n"
             f"captured: {datetime.now().isoformat(timespec='seconds')}\n---\n")
    (out / "lineups_all.psv").write_text(stamp + text)
    print(f"lineups: {len(text.splitlines()):,} rows across {len(pairs)} team-periods -> {out / 'lineups_all.psv'}")





def capture_news(out_dir: Path | None = None, quiet: bool = False) -> Path | None:
    """Capture the all-player-updates stream (RotoWire content syndicated
    into our CBS league subscription — timestamped player blurbs)."""
    out = out_dir or (RAW_ROOT / date.today().isoformat())
    out.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(PROFILE_DIR, headless=True)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(BASE + "/players/all-player-updates?print_rows=9999",
                      wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            text = page.inner_text("body")
            ctx.close()
    except Exception as exc:  # profile locked by another capture — skip cycle
        if not quiet:
            print(f"news capture skipped: {exc}", file=sys.stderr)
        return None
    stamp = (f"source: {BASE}/players/all-player-updates\n"
             f"captured: {datetime.now().isoformat(timespec='seconds')}\n---\n")
    f = out / "player_news_raw.txt"
    f.write_text(stamp + text)
    if not quiet:
        print(f"news: {len(text):,} chars -> {f}")
    return f


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "snapshot"
    if cmd == "login":
        login()
    elif cmd == "snapshot":
        snapshot()
        capture_lineups()
        capture_news()
    elif cmd == "news":
        capture_news()
    elif cmd == "lineups":
        capture_lineups()
    elif cmd == "verify":
        rows = verify_all() if "--all" in sys.argv else [verify_capture()]
        bad = 0
        for r in rows:
            if r["complete"]:
                continue
            bad += 1
            print(f"INCOMPLETE {Path(r['dir']).name}: "
                  f"missing={r['missing']} empty={r['empty']} "
                  f"failed={r['failed_pages']}")
        print(f"{len(rows) - bad}/{len(rows)} captures complete")
        sys.exit(1 if bad else 0)
    else:
        sys.exit(f"unknown command: {cmd}")
