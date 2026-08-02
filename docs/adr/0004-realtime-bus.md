# ADR-0004: Real-time event bus

Date: 2026-08-02 · Status: accepted

MLB offers no public push feed (no websocket/webhook), so the "bus" is a
local always-on poller (`capture/bus.py`, launchd agent
`com.fantasy-assistant.bus`, log at ~/.fantasy-assistant/bus.log):

- **Fast lane (3 min):** MLB transactions API (IL/paternity/options/DFA/
  trades — structured, minutes-fresh, league-wide), filtered to our player
  universe; new events -> Alert nodes + macOS notifications. RotoWire news
  joins this lane automatically when ROTOWIRE_API_KEY lands in .env
  (capture/rotowire.py — Nolen purchases the RotoWire Fantasy News API
  subscription himself; we never handle the checkout).
- **Slow lane (~30 min):** probable starters, per-start fastball velocity
  (+ trend Signals at ±0.8 mph, last2 vs prior5), position-game eligibility
  counters (15-19 games at a new position = window opening).
- CBS stays on snapshot cadence (session-bound, waivers process nightly —
  minutes-latency there buys little).

Manage: `launchctl unload ~/Library/LaunchAgents/com.fantasy-assistant.bus.plist`
to stop; delete the plist to remove.
