# Changelog

## 1.3.0 — 2026-09-14

- Prefer official Codex daily token totals and preserve service date labels.
- Keep current-day local estimates visibly separate and exclude them from official totals.
- Show unavailable historical days explicitly; retain stale official data without affecting quota reads.
- Keep cache-hit rates and DeepSeek token details scoped to local logs.

## 1.2.0 — 2026-09-14

- Add separate seven-day local token charts for Codex and DeepSeek, with midnight rollover and live updates.
- Add optional Windows low-quota notifications with persistent deduplication and stale-data guards.
- Show local daily cache-hit percentage beside Codex and DeepSeek token totals and in usage details.
- Use all input tokens as the denominator; show unknown for zero input or incomplete data.

## 1.1.0 — 2026-09-14

- Read-only Codex reset-credit count and expiry details.
- Local daily Codex and DeepSeek/Claude Code token counters, refreshed every ten seconds.
- Input, output and included cache-hit breakdown in a live details window.
- Incremental log reading with duplicate handling and local-midnight rollover.
- Explicit local-only scope and unavailable/partial data states.

## 1.0.0 — 2026-09-14

- Windows tray panel with manual and five-minute automatic refresh.
- Codex account quota and local reset countdown.
- DeepSeek API balance using existing Claude Code configuration.
- Codex Radar scores for Astra, Sol, Terra and Luna, with three benchmark views.
- Explicit stale-data states, local caching, and always-on-top mode.
