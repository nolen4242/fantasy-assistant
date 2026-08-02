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
        vals.push(c.textContent.trim().replace(/\\s+/g, ' '));
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

        for path, fname in PAGES.items():
            page.goto(BASE + path, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            text = page.evaluate(TABLE_EXTRACT_JS) if "print_rows" in path or "roster" in path \
                else page.inner_text("body")
            stamp = f"source: {BASE + path}\ncaptured: {datetime.now().isoformat(timespec='seconds')}\n---\n"
            (out / fname).write_text(stamp + text)
            captured.append((path, fname, len(text)))

        # FA pools with SportsLine projections (batters, then pitcher report)
        for path, fname in [
            ("/stats/stats-main?print_rows=9999", "fa_pool_batters.psv"),
            ("/stats/stats-main/fa:P/period-all:p/standard/projections?print_rows=9999", "fa_pool_pitchers.psv"),
        ]:
            page.goto(BASE + path, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            text = page.evaluate(TABLE_EXTRACT_JS)
            (out / fname).write_text(text)
            captured.append((path, fname, len(text)))

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

    manifest = out / "MANIFEST.md"
    lines = [f"# Raw capture manifest — {out.name} (runner)", ""]
    lines += [f"- `{f}` <- `{p}` ({n:,} chars)" for p, f, n in captured]
    manifest.write_text("\n".join(lines) + "\n")
    for p, f, n in captured:
        print(f"  {f:<32} {n:>9,} chars  <- {p}")
    print(f"snapshot complete: {out}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "snapshot"
    if cmd == "login":
        login()
    elif cmd == "snapshot":
        snapshot()
    else:
        sys.exit(f"unknown command: {cmd}")
