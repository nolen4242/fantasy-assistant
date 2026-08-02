# ADR-0003: Player identity strategy

Date: 2026-08-02 · Status: accepted

## Problem

Name-keyed Player nodes merged distinct humans (two rostered Max Muncys; a
Luis García Jr / veteran-pitcher mixup that silently zeroed a rival's 2B for
seven weeks) and the fuzzy crosswalk pass polluted 472 pool-longtail players
with wrong MLBAM ids.

## Decision

1. **Person-level identity anchors on ids, join-level on names.** CBS renders
   names consistently across its own pages, so `player:name:*` uids remain the
   ingestion join key. `cbs_id` (harvested from FA-pool action buttons AND
   roster-report player links — full rostered coverage as of today) and
   `mlbam_id` are the identity anchors carried as properties.
2. **Collisions are resolved by hand, never by heuristic**: crosswalk
   `OVERRIDES` for wrong-id fixes; node surgery (team-scoped relinking) when
   one name is two rostered humans. The Muncy split is the template.
3. **Nightly identity audit** (`graph.identity.audit`) in the daily routine:
   shared mlbam/cbs ids, rostered players without mlbam, active regulars with
   zero day lines. Findings are reported, never auto-fixed.
4. Fuzzy crosswalk passes (suffix/first-initial) stay restricted in effect:
   pollution cleared once; audit catches recurrence.

## Deferred

Full uid migration to `player:cbs:{id}`: now *possible* (cbs_id coverage is
complete) but a cross-module change with little marginal benefit while the
audit stays clean. Revisit in the offseason refactor window.
