from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote

_SCHEMA_VERSION = 3

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS players (
    player_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    participant_type TEXT NOT NULL,
    starting_bankroll INTEGER NOT NULL,
    player_token_digest TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS player_stats (
    player_id TEXT PRIMARY KEY
        REFERENCES players(player_id) ON DELETE CASCADE,
    bankroll_delta INTEGER NOT NULL DEFAULT 0,
    rounds_played INTEGER NOT NULL DEFAULT 0,
    hands_played INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    pushes INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    blackjack_count INTEGER NOT NULL DEFAULT 0,
    bust_count INTEGER NOT NULL DEFAULT 0,
    action_counts_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS tables (
    table_id TEXT PRIMARY KEY,
    seat_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    active_round_id TEXT,
    rules_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    shoe_json TEXT NOT NULL DEFAULT '[]',
    shuffle_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS table_seats (
    table_id TEXT NOT NULL
        REFERENCES tables(table_id) ON DELETE CASCADE,
    seat_number INTEGER NOT NULL,
    player_id TEXT NOT NULL
        REFERENCES players(player_id) ON DELETE RESTRICT,
    bankroll INTEGER NOT NULL,
    ready_for_next_round INTEGER NOT NULL,
    active_hand_ids_json TEXT NOT NULL,
    PRIMARY KEY (table_id, seat_number)
);

CREATE TABLE IF NOT EXISTS rounds (
    round_id TEXT PRIMARY KEY,
    table_id TEXT NOT NULL
        REFERENCES tables(table_id) ON DELETE CASCADE,
    phase TEXT NOT NULL,
    action_count INTEGER NOT NULL DEFAULT 0,
    snapshot_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS round_events (
    event_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL
        REFERENCES rounds(round_id) ON DELETE CASCADE,
    table_id TEXT NOT NULL
        REFERENCES tables(table_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_table_seats_table_id ON table_seats(table_id, seat_number);
CREATE INDEX IF NOT EXISTS idx_round_events_round_id ON round_events(round_id, sequence);
"""


def database_backend(database_url: str) -> str:
    return database_url.split(":", maxsplit=1)[0]


def _sqlite_path(database_url: str) -> str:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise ValueError("Only sqlite:/// URLs are currently supported.")

    raw_path = unquote(database_url.removeprefix(prefix))
    if raw_path == ":memory:":
        return raw_path

    path = Path(raw_path)
    if not path.is_absolute():
        path = Path.cwd() / path

    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def _connect_sqlite(database_url: str) -> sqlite3.Connection:
    connection = sqlite3.connect(_sqlite_path(database_url), check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON;")
    return connection


def _apply_migrations(connection: sqlite3.Connection) -> None:
    current_version = connection.execute("PRAGMA user_version;").fetchone()[0]
    if current_version >= _SCHEMA_VERSION:
        return

    if current_version < 1:
        connection.executescript(_SCHEMA_V1)
        connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION};")
        return

    if current_version < 2:
        connection.execute("ALTER TABLE tables ADD COLUMN shoe_json TEXT NOT NULL DEFAULT '[]';")
        connection.execute("ALTER TABLE tables ADD COLUMN shuffle_count INTEGER NOT NULL DEFAULT 0;")
        current_version = 2

    if current_version < 3:
        connection.execute("ALTER TABLE players ADD COLUMN player_token_digest TEXT NOT NULL DEFAULT '';")
        current_version = 3

    connection.execute(f"PRAGMA user_version = {current_version};")


@contextmanager
def database_connection(database_url: str) -> Generator[sqlite3.Connection, None, None]:
    connection = _connect_sqlite(database_url)

    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database(database_url: str) -> None:
    with database_connection(database_url) as connection:
        _apply_migrations(connection)


def probe_database(database_url: str) -> None:
    with database_connection(database_url) as connection:
        connection.execute("SELECT 1;").fetchone()
