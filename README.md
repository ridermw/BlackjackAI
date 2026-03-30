# Blackjack AI

Blackjack AI is a Python service for running blackjack games through a REST API, driving scripted clients against that API, and benchmarking different player strategies over repeated sessions.

The repository is organized around a pure game engine, a FastAPI application layer, a small client SDK, and a benchmark harness that can run locally in-process or against a deployed server.

## Features

- Blackjack domain engine with round flow, dealer automation, payouts, and legal-action enforcement
- FastAPI service for players, tables, rounds, events, and leaderboard queries
- SQLite-backed persistence for players, tables, rounds, events, and aggregate stats
- Python gameplay client for driving the API from tests or automation
- Benchmark runner with built-in strategies ranging from conservative to counting-based play
- Comprehensive automated test suite covering engine, API, client, and benchmark behavior

## Project layout

```text
.
├── LICENSE
├── README.md
├── pyproject.toml
├── src/
│   └── blackjack_ai/
│       ├── __init__.py
│       ├── api/
│       ├── benchmark/
│       ├── client/
│       ├── engine/
│       ├── persistence/
│       └── config.py
└── tests/
    ├── api/
    ├── benchmark/
    ├── client/
    └── engine/
```

### Package responsibilities

- `blackjack_ai.engine` contains the domain model, shoe logic, and round orchestration.
- `blackjack_ai.api` exposes the service through FastAPI and maps HTTP requests to the game service.
- `blackjack_ai.persistence` owns SQLite schema setup, connections, and persisted game state loading/saving.
- `blackjack_ai.client` provides a higher-level Python client for gameplay flows.
- `blackjack_ai.benchmark` contains the transport client, harness, and built-in AI strategies.
- `blackjack_ai.config` reads environment-based runtime settings.

## Requirements

- Python 3.11 or newer
- `pip` for local installation

## Installation

Create a virtual environment, activate it, and install the package with test dependencies:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[test]"
```

## Running the API

The project exposes a console script that launches the FastAPI app with Uvicorn:

```powershell
blackjack-ai-api
```

By default, the API listens on `127.0.0.1:8000` and writes data to `blackjack_ai.db` in the current working directory.

### Runtime configuration

All runtime settings are configured through environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `BLACKJACK_AI_APP_NAME` | `Blackjack AI Service` | FastAPI application title |
| `BLACKJACK_AI_ENVIRONMENT` | `development` | Environment label surfaced by `/health` |
| `BLACKJACK_AI_HOST` | `127.0.0.1` | Host passed to Uvicorn |
| `BLACKJACK_AI_PORT` | `8000` | Port passed to Uvicorn |
| `BLACKJACK_AI_DATABASE_URL` | `sqlite:///blackjack_ai.db` | Database URL for SQLite persistence |

Example:

```powershell
$env:BLACKJACK_AI_DATABASE_URL = "sqlite:///data\blackjack.sqlite3"
$env:BLACKJACK_AI_PORT = "8080"
blackjack-ai-api
```

## Running benchmarks

The benchmark runner can exercise the public API either against the default in-process server or against a separately running API.

List the built-in strategies:

```powershell
blackjack-ai-benchmark --list-strategies
```

Run a local benchmark series:

```powershell
blackjack-ai-benchmark --strategy conservative --strategy counting --rounds 100 --series 5
```

Target an already running API:

```powershell
blackjack-ai-benchmark --base-url http://127.0.0.1:8000 --strategy basic --strategy aggressive --rounds 50 --json
```

### Built-in strategies

- `conservative` - Flat minimum bets, earlier stands, minimal doubles
- `balanced` - Moderate bankroll pressure with simple threshold heuristics
- `basic` - Basic-strategy style play with splits, doubles, and surrender
- `counting` - Public-card Hi-Lo counting with conservative bet spreads
- `aggressive` - Larger bets and more aggressive doubles/splits

## Quick API walkthrough

The API is centered around a few core resources:

- `players` represent human or AI participants
- `tables` represent long-lived game sessions
- `rounds` represent individual hands played at a table
- `round_events` provide an append-only event log for polling or replay
- `leaderboard` aggregates performance across persisted play

### Important authentication rule

Creating a player returns a `player_token`. Public reads never expose that token again, but player-owned mutations require it in the `X-Player-Token` header.

### Example flow

Create a player:

```powershell
curl.exe -X POST http://127.0.0.1:8000/players `
  -H "Content-Type: application/json" `
  -d "{\"display_name\":\"Alice\",\"participant_type\":\"human\",\"starting_bankroll\":1000}"
```

Create a table:

```powershell
curl.exe -X POST http://127.0.0.1:8000/tables `
  -H "Content-Type: application/json" `
  -d "{\"table_id\":\"table-1\",\"seat_count\":2}"
```

Seat the player using the token returned by `/players`:

```powershell
curl.exe -X POST http://127.0.0.1:8000/tables/table-1/seats/1/join `
  -H "Content-Type: application/json" `
  -H "X-Player-Token: <player-token>" `
  -d "{\"player_id\":\"alice\"}"
```

Start a round:

```powershell
curl.exe -X POST http://127.0.0.1:8000/tables/table-1/rounds `
  -H "Content-Type: application/json" `
  -d "{\"round_id\":\"round-1\"}"
```

Place a bet:

```powershell
curl.exe -X POST http://127.0.0.1:8000/rounds/round-1/bets `
  -H "Content-Type: application/json" `
  -H "X-Player-Token: <player-token>" `
  -d "{\"player_id\":\"alice\",\"amount\":25}"
```

Apply an action:

```powershell
curl.exe -X POST http://127.0.0.1:8000/rounds/round-1/actions `
  -H "Content-Type: application/json" `
  -H "X-Player-Token: <player-token>" `
  -d "{\"player_id\":\"alice\",\"hand_id\":\"<hand-id>\",\"action\":\"stand\"}"
```

Read the final leaderboard:

```powershell
curl.exe http://127.0.0.1:8000/leaderboard
```

## API surface

### Service endpoints

- `GET /health`
- `GET /status`

### Player endpoints

- `POST /players`
- `GET /players/{player_id}`
- `GET /players/{player_id}/stats`

### Table endpoints

- `POST /tables`
- `GET /tables/{table_id}`
- `POST /tables/{table_id}/seats/{seat_number}/join`
- `POST /tables/{table_id}/seats/{seat_number}/leave`
- `POST /tables/{table_id}/rounds`

### Round endpoints

- `GET /rounds/{round_id}`
- `POST /rounds/{round_id}/bets`
- `POST /rounds/{round_id}/actions`
- `GET /rounds/{round_id}/events`

### Leaderboard endpoint

- `GET /leaderboard`

## Gameplay model

The service supports multi-player tables and persists state between API restarts when a file-backed SQLite database is configured.

### Rules and round behavior

- Six-deck blackjack by default
- Dealer stands on soft 17
- Default blackjack payout is 3:2
- Default table limits are `$10` minimum and `$500` maximum
- Split depth and double-after-split rules are configurable per table
- Supported actions include `hit`, `stand`, `double`, `split`, `surrender`, and `insurance` when legal

### Concurrency and retry behavior

Bet and action requests support:

- `request_id` for idempotent retries
- `expected_version` for optimistic concurrency checks

This makes the API safer to drive from automated clients and benchmark harnesses.

## Python client usage

The `GameplayClient` offers a higher-level Python interface over the raw REST API:

```python
from blackjack_ai.client import GameplayClient

with GameplayClient.from_base_url("http://127.0.0.1:8000") as client:
    player = client.create_player("Alice", player_id="alice")
    client.create_table(table_id="table-1", seat_count=1)
    client.seat_player("table-1", 1, "alice", player_token=player["player_token"])
    round_session = client.start_round("table-1", round_id="round-1")
    round_session.bet(amount=25)
    round_session.stand()
```

The client automatically stores player tokens returned by `create_player`, so later seat, bet, and action calls can usually omit the token.

## Persistence

File-backed SQLite persistence is used automatically when `BLACKJACK_AI_DATABASE_URL` points at a real file. The persistence package is responsible for:

- creating the schema on startup
- applying schema migrations
- opening SQLite connections with foreign keys enabled
- loading persisted players, tables, and rounds into the service
- writing updated snapshots, events, and aggregate statistics

For purely in-memory runs, use `sqlite:///:memory:` and inject a `GameService` directly in tests.

## Testing

Run the full suite:

```powershell
python -m pytest -q
```

Run focused areas:

```powershell
python -m pytest tests\engine -q
python -m pytest tests\api -q
python -m pytest tests\client -q
python -m pytest tests\benchmark -q
```

The tests cover:

- engine rules and round orchestration
- API authentication and persistence behavior
- client session flows and retry semantics
- benchmark strategy evaluation and reporting

## Development notes

- Keep business rules in `blackjack_ai.engine` so they remain reusable outside the API layer.
- Keep persistence concerns in `blackjack_ai.persistence`.
- Keep API schemas and HTTP wiring in `blackjack_ai.api`.
- When adding new behavior, add or update tests in the matching `tests\` subpackage.

## License

This project is licensed under the terms of the `LICENSE` file in the repository root.
