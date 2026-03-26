# Blackjack Game Engine Implementation Plan

## Problem
Build a new blackjack game engine service that lets human and AI clients play through REST calls. The service must manage blackjack rules, state transitions, legal actions, round outcomes, and benchmarking metrics so multiple AIs can be evaluated over repeated play.

## Current state
- Repository is greenfield. The only tracked file is `LICENSE`.
- No runtime, project scaffold, API framework, persistence layer, tests, or docs exist yet.
- This plan assumes we are free to choose the backend stack and persistence approach.

## Proposed approach
Build the system in three layers:
1. A pure blackjack domain engine that owns rules, dealing, turn progression, dealer automation, and settlement.
2. An application layer that manages players, table sessions, rounds, persistence, and statistics.
3. A REST API that exposes the same workflows to human clients and AI agents.

Confirmed choices for this plan:
- Use a Python backend. Recommended concrete stack when implementation starts: `FastAPI`, `Pydantic`, `SQLAlchemy` or `SQLModel`, and `pytest`.
- Use `SQLite` from day one for sessions, rounds, event history, and aggregate stats.
- Use REST plus polling-friendly reads rather than WebSockets in v1.
- Model a long-lived `table/session` that contains many `rounds`, because repeated play matters more than a single hand for AI comparison.
- Support multiple players at the same table.
- Start with configurable but opinionated house rules: 6 decks, dealer stands on soft 17, blackjack pays 3:2, minimum bet `$10`, maximum bet `$500`, and v1 supports hit/stand/double/split.
- Defer insurance and surrender until after the core engine is stable.
- Rank AIs with multiple metrics, using bankroll delta as the primary leaderboard sort.

## Core functional design

### Game concepts
- `PlayerProfile`: registered human or AI participant with display name, type, and optional metadata.
- `TableSession`: long-lived game container with rules, limits, seating, and session status.
- `Round`: a single blackjack hand within a table session.
- `SeatState`: player bankroll, current bet, readiness, and active hands for the round.
- `HandState`: cards, total interpretations, soft/hard info, legal actions, and result.
- `RuleConfig`: deck count, payout rules, dealer behavior, split/double options, and table limits.
- `TurnState`: which player and hand can act, plus the legal actions available right now.
- `ActionLog` and `StatsLedger`: immutable event history plus rolled-up performance metrics.

### State lifecycle
1. Register players or bots.
2. Create a table session with limits and rule configuration.
3. Seat one or more players.
4. Start a round.
5. Accept opening bets.
6. Deal initial cards and expose only public state.
7. Advance through player turns while enforcing legal actions.
8. Auto-run dealer logic after player hands are complete.
9. Resolve payouts, update bankrolls and metrics, and close the round.
10. Start another round or end the session.

### API surface (draft)
- `POST /players`
- `GET /players/{playerId}`
- `GET /players/{playerId}/stats`
- `POST /tables`
- `GET /tables/{tableId}`
- `POST /tables/{tableId}/seats/{seatNumber}/join`
- `POST /tables/{tableId}/rounds`
- `GET /rounds/{roundId}`
- `POST /rounds/{roundId}/bets`
- `POST /rounds/{roundId}/actions`
- `GET /rounds/{roundId}/events`
- `GET /leaderboard`

Response design goals:
- Humans and AIs use the same endpoints.
- Responses always include enough state to know the next legal move.
- Hidden information remains hidden, especially the dealer hole card and any shuffle seed.
- Action endpoints must reject duplicate, late, or illegal moves predictably.

### Stats and benchmarking
Track at least:
- bankroll delta
- rounds played
- wins / pushes / losses
- blackjack count
- bust rate
- average return per hand
- action distribution

Support comparing AIs across a single session and across many sessions.

### Suggested persistence shape
Store enough state to resume games, reconstruct outcomes, and score AIs over time:
- `players`
- `table_sessions`
- `table_seats`
- `rounds`
- `hands`
- `round_actions`
- `round_results`
- `player_stat_snapshots` or equivalent aggregate metrics table

## Implementation phases
1. Scaffold the Python service, configuration, SQLite wiring, and test setup.
2. Implement the blackjack domain model and legal-action engine.
3. Implement round orchestration, dealer flow, and settlement.
4. Implement REST endpoints and public/private state shaping.
5. Add persistence for sessions, rounds, events, and aggregate stats.
6. Add tests for rule correctness, edge cases, and API flows.
7. Add a lightweight simulation or scripted-client harness for AI benchmarking.

## Risks and considerations
- Rule variants increase complexity quickly, especially around split behavior, insurance, and surrender.
- Fair AI benchmarking may need deterministic or audit-friendly shuffles without exposing hidden state to active players.
- The system should distinguish public game state from internal/admin state so bots cannot see hidden information.
- Multi-client concurrency and duplicate action submissions need explicit handling.
- A continuous table/session model is more useful than a one-hand-only model for ranking players.

## Locked-in plan inputs
- Backend runtime: Python
- Initial storage: SQLite
- V1 rule scope: hit / stand / double / split only
- Table model: multiple players per table
- Leaderboard priority: multiple metrics with bankroll delta primary
- Table limits: minimum bet `$10`, maximum bet `$500`
