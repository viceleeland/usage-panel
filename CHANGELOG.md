# Changelog

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
