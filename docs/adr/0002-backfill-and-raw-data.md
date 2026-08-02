# ADR-0002: 2026 backfill scope & raw-capture retention

Date: 2026-08-02 · Status: accepted

## Backfill scope (2026 season, periods 1–19)

**Backfill now (available on demand from CBS):**
- Full transaction log (captured 2026-08-02: 518 transactions to 3/27/26).
- Standings by period (`/standings` → BY PERIOD views).
- Draft results (draft report pages).
- Rosters: reconstructable = draft + transaction replay, verified against the
  current roster grid. Discrepancies logged per SCHEMA reconciliation model.

**Capture-forward only (history unrecoverable):**
- Free-agent pool snapshots incl. waiver flags & SportsLine weekly projections.
- Waiver order (current value only; past values gone).
- Lineup assignments per period for rivals (grid shows current only; period
  views may allow partial backfill — investigate).
- MLB dailies: backfillable from MLB Stats API / Savant at leisure (public,
  stable), so treated as low-urgency backfill, not capture-critical.

## Raw-capture retention

Every capture writes the raw page text/derived PSV under
`data/raw/YYYY-MM-DD/` with a `MANIFEST.md` (source URL, capture time, row
counts, caveats). Raw is the recovery path when parsers have bugs — parse again,
never re-scrape history you can't get back.

In git for now (small). When daily FA-pool snapshots accumulate (~1MB/day),
move `data/raw/` out of git to local object storage + backup; revisit ~Sep 2026.

## Capture mechanics note

CBS report pages accept `?print_rows=9999` (full listing, no pagination) and
deep-link report paths like `/stats/stats-main/fa:P/period-20:p/standard/projections`.
Prefer these parameterized URLs over UI-driving. Session: user-authenticated
browser session (or exported session cookies — never credentials).

**In-page fetch loop works** (proven 2026-08-02): from an authenticated CBS
page, same-origin `fetch()` + `DOMParser` retrieved all 19 by-period standings
pages in one JS call (~132KB). The per-period option URLs come from the page's
own PERIOD `<select>` values.

**Exfiltration constraint**: getting bulk data out of the interactive Chrome
session is the bottleneck, not capture. Clipboard (`navigator.clipboard`) works
only with fresh user activation and Chrome eventually hard-denied it;
`fetch()` to a localhost collector is silently stalled by Chrome's local
network access policy (PNA preflight answered, still blocked). For the real
pipeline, do NOT scrape via the user's interactive Chrome: run capture in a
dedicated browser context (e.g. Playwright with a persisted CBS session the
user logs into once) where page content is read directly by the driver.

## Still pending backfill

- By-period standings, periods 1–19 (fetch loop ready; blocked only on the
  dedicated-browser capture runner). Also grab periods 20+ as they close.
- Draft results: captured 2026-08-02 (`draft_results.txt`) including the
  draft-room chat log (opponent intel + written confirmation of the
  2026 rule change to plain Saves and 2 reserve slots).
