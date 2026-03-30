from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections import defaultdict
from collections import deque
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Iterable
from typing import Protocol

from blackjack_ai.api.service import IdempotentRequestRecord
from blackjack_ai.api.service import PlayerRecord
from blackjack_ai.api.service import PlayerStatsRecord
from blackjack_ai.api.service import RoundEventRecord
from blackjack_ai.api.service import RoundParticipantRecord
from blackjack_ai.api.service import RoundRecord
from blackjack_ai.api.service import SeatRecord
from blackjack_ai.api.service import TableRecord
from blackjack_ai.db import database_connection
from blackjack_ai.db import initialize_database
from blackjack_ai.engine import ActionType
from blackjack_ai.engine import Bet
from blackjack_ai.engine import Card
from blackjack_ai.engine import CardRank
from blackjack_ai.engine import CardSuit
from blackjack_ai.engine import DealerState
from blackjack_ai.engine import HandOutcome
from blackjack_ai.engine import HandResolution
from blackjack_ai.engine import HandState
from blackjack_ai.engine import HandStatus
from blackjack_ai.engine import ParticipantType
from blackjack_ai.engine import PayoutRatio
from blackjack_ai.engine import PlayerProfile
from blackjack_ai.engine import ResolutionReason
from blackjack_ai.engine import RoundPhase
from blackjack_ai.engine import RuleConfig
from blackjack_ai.engine import TableSessionStatus
from blackjack_ai.engine import TurnActor
from blackjack_ai.engine import TurnState


@dataclass(slots=True)
class PersistedGameState:
    players: dict[str, PlayerRecord] = field(default_factory=dict)
    tables: dict[str, TableRecord] = field(default_factory=dict)
    rounds: dict[str, RoundRecord] = field(default_factory=dict)


class GameRepository(Protocol):
    def load_state(self) -> PersistedGameState: ...

    def persist(
        self,
        *,
        players: Iterable[PlayerRecord] = (),
        tables: Iterable[TableRecord] = (),
        rounds: Iterable[RoundRecord] = (),
    ) -> None: ...


class SqliteGameRepository:
    def __init__(self, database_url: str) -> None:
        if database_url == "sqlite:///:memory:":
            raise ValueError("SqliteGameRepository requires a file-backed SQLite database.")

        self.database_url = database_url
        initialize_database(database_url)

    def load_state(self) -> PersistedGameState:
        with database_connection(self.database_url) as connection:
            return PersistedGameState(
                players=self._load_players(connection),
                tables=self._load_tables(connection),
                rounds=self._load_rounds(connection),
            )

    def persist(
        self,
        *,
        players: Iterable[PlayerRecord] = (),
        tables: Iterable[TableRecord] = (),
        rounds: Iterable[RoundRecord] = (),
    ) -> None:
        player_records = list({player.profile.player_id: player for player in players}.values())
        table_records = list({table.table_id: table for table in tables}.values())
        round_records = list({round_record.round_id: round_record for round_record in rounds}.values())
        if not player_records and not table_records and not round_records:
            return

        with database_connection(self.database_url) as connection:
            for player in player_records:
                self._upsert_player(connection, player)
            for table in table_records:
                self._upsert_table(connection, table)
            for round_record in round_records:
                self._upsert_round(connection, round_record)

    def _load_players(self, connection: sqlite3.Connection) -> dict[str, PlayerRecord]:
        rows = connection.execute(
            """
            SELECT
                p.player_id,
                p.display_name,
                p.participant_type,
                p.starting_bankroll,
                p.player_token_digest,
                p.metadata_json,
                COALESCE(ps.bankroll_delta, 0) AS bankroll_delta,
                COALESCE(ps.rounds_played, 0) AS rounds_played,
                COALESCE(ps.hands_played, 0) AS hands_played,
                COALESCE(ps.wins, 0) AS wins,
                COALESCE(ps.pushes, 0) AS pushes,
                COALESCE(ps.losses, 0) AS losses,
                COALESCE(ps.blackjack_count, 0) AS blackjack_count,
                COALESCE(ps.bust_count, 0) AS bust_count,
                COALESCE(ps.action_counts_json, '{}') AS action_counts_json
            FROM players p
            LEFT JOIN player_stats ps ON ps.player_id = p.player_id
            ORDER BY p.player_id
            """
        ).fetchall()

        players: dict[str, PlayerRecord] = {}
        for row in rows:
            players[row["player_id"]] = PlayerRecord(
                profile=PlayerProfile(
                    player_id=row["player_id"],
                    display_name=row["display_name"],
                    participant_type=ParticipantType(row["participant_type"]),
                    metadata=_load_json(row["metadata_json"], default={}),
                ),
                starting_bankroll=row["starting_bankroll"],
                player_token_digest=row["player_token_digest"],
                stats=PlayerStatsRecord(
                    bankroll_delta=row["bankroll_delta"],
                    rounds_played=row["rounds_played"],
                    hands_played=row["hands_played"],
                    wins=row["wins"],
                    pushes=row["pushes"],
                    losses=row["losses"],
                    blackjack_count=row["blackjack_count"],
                    bust_count=row["bust_count"],
                    action_counts=Counter(_load_json(row["action_counts_json"], default={})),
                ),
            )
        return players

    def _load_tables(self, connection: sqlite3.Connection) -> dict[str, TableRecord]:
        seat_rows = connection.execute(
            """
            SELECT
                table_id,
                seat_number,
                player_id,
                bankroll,
                ready_for_next_round,
                active_hand_ids_json
            FROM table_seats
            ORDER BY table_id, seat_number
            """
        ).fetchall()
        seats_by_table: dict[str, dict[int, SeatRecord]] = defaultdict(dict)
        for row in seat_rows:
            seats_by_table[row["table_id"]][row["seat_number"]] = SeatRecord(
                seat_number=row["seat_number"],
                player_id=row["player_id"],
                bankroll=row["bankroll"],
                ready_for_next_round=bool(row["ready_for_next_round"]),
                active_hand_ids=list(_load_json(row["active_hand_ids_json"], default=[])),
            )

        table_rows = connection.execute(
            """
            SELECT
                table_id,
                seat_count,
                status,
                active_round_id,
                rules_json,
                metadata_json,
                shoe_json,
                shuffle_count
            FROM tables
            ORDER BY table_id
            """
        ).fetchall()

        tables: dict[str, TableRecord] = {}
        for row in table_rows:
            tables[row["table_id"]] = TableRecord(
                table_id=row["table_id"],
                rules=_deserialize_rules(_load_json(row["rules_json"], default={})),
                seat_count=row["seat_count"],
                status=TableSessionStatus(row["status"]),
                active_round_id=row["active_round_id"],
                metadata=_load_json(row["metadata_json"], default={}),
                seats=seats_by_table.get(row["table_id"], {}),
                shoe=deque(_deserialize_card(card) for card in _load_json(row["shoe_json"], default=[])),
                shuffle_count=row["shuffle_count"],
            )
        return tables

    def _load_rounds(self, connection: sqlite3.Connection) -> dict[str, RoundRecord]:
        event_rows = connection.execute(
            """
            SELECT
                event_id,
                sequence,
                event_type,
                round_id,
                table_id,
                timestamp,
                payload_json
            FROM round_events
            ORDER BY round_id, sequence
            """
        ).fetchall()
        events_by_round: dict[str, list[RoundEventRecord]] = defaultdict(list)
        for row in event_rows:
            events_by_round[row["round_id"]].append(
                RoundEventRecord(
                    event_id=row["event_id"],
                    sequence=row["sequence"],
                    event_type=row["event_type"],
                    round_id=row["round_id"],
                    table_id=row["table_id"],
                    timestamp=row["timestamp"],
                    payload=_load_json(row["payload_json"], default={}),
                )
            )

        round_rows = connection.execute(
            """
            SELECT
                round_id,
                snapshot_json
            FROM rounds
            ORDER BY round_id
            """
        ).fetchall()

        rounds: dict[str, RoundRecord] = {}
        for row in round_rows:
            snapshot = _load_json(row["snapshot_json"], default={})
            round_record = _deserialize_round(snapshot, events_by_round.get(row["round_id"], []))
            rounds[round_record.round_id] = round_record
        return rounds

    def _upsert_player(self, connection: sqlite3.Connection, player: PlayerRecord) -> None:
        connection.execute(
            """
            INSERT INTO players (
                player_id,
                display_name,
                participant_type,
                starting_bankroll,
                player_token_digest,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(player_id) DO UPDATE SET
                display_name = excluded.display_name,
                participant_type = excluded.participant_type,
                starting_bankroll = excluded.starting_bankroll,
                player_token_digest = excluded.player_token_digest,
                metadata_json = excluded.metadata_json
            """,
            (
                player.profile.player_id,
                player.profile.display_name,
                player.profile.participant_type.value,
                player.starting_bankroll,
                player.player_token_digest,
                _dump_json(dict(player.profile.metadata)),
            ),
        )
        connection.execute(
            """
            INSERT INTO player_stats (
                player_id,
                bankroll_delta,
                rounds_played,
                hands_played,
                wins,
                pushes,
                losses,
                blackjack_count,
                bust_count,
                action_counts_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(player_id) DO UPDATE SET
                bankroll_delta = excluded.bankroll_delta,
                rounds_played = excluded.rounds_played,
                hands_played = excluded.hands_played,
                wins = excluded.wins,
                pushes = excluded.pushes,
                losses = excluded.losses,
                blackjack_count = excluded.blackjack_count,
                bust_count = excluded.bust_count,
                action_counts_json = excluded.action_counts_json
            """,
            (
                player.profile.player_id,
                player.stats.bankroll_delta,
                player.stats.rounds_played,
                player.stats.hands_played,
                player.stats.wins,
                player.stats.pushes,
                player.stats.losses,
                player.stats.blackjack_count,
                player.stats.bust_count,
                _dump_json({action.value: player.stats.action_counts.get(action.value, 0) for action in ActionType}),
            ),
        )

    def _upsert_table(self, connection: sqlite3.Connection, table: TableRecord) -> None:
        connection.execute(
            """
            INSERT INTO tables (
                table_id,
                seat_count,
                status,
                active_round_id,
                rules_json,
                metadata_json,
                shoe_json,
                shuffle_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(table_id) DO UPDATE SET
                seat_count = excluded.seat_count,
                status = excluded.status,
                active_round_id = excluded.active_round_id,
                rules_json = excluded.rules_json,
                metadata_json = excluded.metadata_json,
                shoe_json = excluded.shoe_json,
                shuffle_count = excluded.shuffle_count
            """,
            (
                table.table_id,
                table.seat_count,
                table.status.value,
                table.active_round_id,
                _dump_json(table.rules.to_dict()),
                _dump_json(table.metadata),
                _dump_json([card.to_dict() for card in table.shoe]),
                table.shuffle_count,
            ),
        )
        connection.execute("DELETE FROM table_seats WHERE table_id = ?", (table.table_id,))
        for seat in sorted(table.seats.values(), key=lambda record: record.seat_number):
            connection.execute(
                """
                INSERT INTO table_seats (
                    table_id,
                    seat_number,
                    player_id,
                    bankroll,
                    ready_for_next_round,
                    active_hand_ids_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    table.table_id,
                    seat.seat_number,
                    seat.player_id,
                    seat.bankroll,
                    int(seat.ready_for_next_round),
                    _dump_json(seat.active_hand_ids),
                ),
            )

    def _upsert_round(self, connection: sqlite3.Connection, round_record: RoundRecord) -> None:
        connection.execute(
            """
            INSERT INTO rounds (
                round_id,
                table_id,
                phase,
                action_count,
                snapshot_json
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(round_id) DO UPDATE SET
                table_id = excluded.table_id,
                phase = excluded.phase,
                action_count = excluded.action_count,
                snapshot_json = excluded.snapshot_json
            """,
            (
                round_record.round_id,
                round_record.table_id,
                round_record.phase.value,
                round_record.action_count,
                _dump_json(_serialize_round(round_record)),
            ),
        )
        for event in round_record.events:
            connection.execute(
                """
                INSERT INTO round_events (
                    event_id,
                    round_id,
                    table_id,
                    sequence,
                    event_type,
                    timestamp,
                    payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    round_id = excluded.round_id,
                    table_id = excluded.table_id,
                    sequence = excluded.sequence,
                    event_type = excluded.event_type,
                    timestamp = excluded.timestamp,
                    payload_json = excluded.payload_json
                """,
                (
                    event.event_id,
                    event.round_id,
                    event.table_id,
                    event.sequence,
                    event.event_type,
                    event.timestamp,
                    _dump_json(event.payload),
                ),
            )


def _dump_json(payload: Any) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _load_json(payload: str | None, *, default: Any) -> Any:
    if payload in {None, ""}:
        return default
    return json.loads(payload)


def _serialize_round(round_record: RoundRecord) -> dict[str, Any]:
    return {
        "round_id": round_record.round_id,
        "table_id": round_record.table_id,
        "rules": round_record.rules.to_dict(),
        "participants": [_serialize_round_participant(participant) for participant in round_record.participants],
        "dealer": round_record.dealer.to_internal_dict(),
        "shoe": [card.to_dict() for card in round_record.shoe],
        "shuffle_count": round_record.shuffle_count,
        "phase": round_record.phase.value,
        "turn": round_record.turn.to_dict() if round_record.turn is not None else None,
        "action_count": round_record.action_count,
        "version": round_record.version,
        "event_sequence": round_record.event_sequence,
        "request_history": {
            player_id: {
                request_id: _serialize_idempotent_request(request)
                for request_id, request in request_history.items()
            }
            for player_id, request_history in round_record.request_history.items()
        },
    }


def _serialize_round_participant(participant: RoundParticipantRecord) -> dict[str, Any]:
    return {
        "player_id": participant.player_id,
        "display_name": participant.display_name,
        "participant_type": participant.participant_type.value,
        "seat_number": participant.seat_number,
        "bankroll_before_round": participant.bankroll_before_round,
        "hands": [_serialize_hand(hand) for hand in participant.hands],
        "bankroll_after_round": participant.bankroll_after_round,
        "opening_bet": participant.opening_bet.to_dict() if participant.opening_bet is not None else None,
    }


def _serialize_hand(hand: HandState) -> dict[str, Any]:
    payload = hand.to_dict()
    payload["insurance_wager"] = hand.insurance_wager.to_dict() if hand.insurance_wager is not None else None
    return payload


def _deserialize_round(snapshot: dict[str, Any], events: list[RoundEventRecord]) -> RoundRecord:
    round_record = RoundRecord(
        round_id=snapshot["round_id"],
        table_id=snapshot["table_id"],
        rules=_deserialize_rules(snapshot["rules"]),
        participants=[_deserialize_round_participant(participant) for participant in snapshot.get("participants", [])],
        dealer=_deserialize_dealer(snapshot["dealer"]),
        shoe=deque(_deserialize_card(card) for card in snapshot.get("shoe", [])),
        shuffle_count=snapshot.get("shuffle_count", 1 if "shoe" in snapshot else 0),
        phase=RoundPhase(snapshot.get("phase", RoundPhase.WAITING_FOR_BETS.value)),
        turn=_deserialize_turn(snapshot.get("turn")),
        action_count=snapshot.get("action_count", 0),
        version=snapshot.get("version", 0),
        events=list(events),
        event_sequence=snapshot.get(
            "event_sequence",
            max((event.sequence for event in events), default=0),
        ),
        request_history={
            player_id: {
                request_id: _deserialize_idempotent_request(request_payload)
                for request_id, request_payload in request_history.items()
            }
            for player_id, request_history in snapshot.get("request_history", {}).items()
        },
    )
    return round_record


def _serialize_idempotent_request(request: IdempotentRequestRecord) -> dict[str, Any]:
    return {
        "payload": request.payload,
        "response": request.response,
    }


def _deserialize_idempotent_request(payload: dict[str, Any]) -> IdempotentRequestRecord:
    return IdempotentRequestRecord(
        payload=payload.get("payload", {}),
        response=payload.get("response", {}),
    )


def _deserialize_round_participant(payload: dict[str, Any]) -> RoundParticipantRecord:
    hands = [_deserialize_hand(hand) for hand in payload.get("hands", [])]
    opening_bet = _deserialize_bet(payload.get("opening_bet"))
    if opening_bet is None and hands:
        opening_bet = hands[0].wager

    return RoundParticipantRecord(
        player_id=payload["player_id"],
        display_name=payload["display_name"],
        participant_type=ParticipantType(payload["participant_type"]),
        seat_number=payload["seat_number"],
        bankroll_before_round=payload["bankroll_before_round"],
        hands=hands,
        bankroll_after_round=payload.get("bankroll_after_round"),
        opening_bet=opening_bet,
    )


def _deserialize_dealer(payload: dict[str, Any]) -> DealerState:
    return DealerState(
        hand=_deserialize_hand(payload["hand"]),
        hole_card_index=payload.get("hole_card_index"),
        hole_card_revealed=payload.get("hole_card_revealed", False),
    )


def _deserialize_turn(payload: dict[str, Any] | None) -> TurnState | None:
    if payload is None:
        return None
    return TurnState(
        actor=TurnActor(payload["actor"]),
        legal_actions=tuple(ActionType(action) for action in payload.get("legal_actions", [])),
        seat_number=payload.get("seat_number"),
        player_id=payload.get("player_id"),
        hand_id=payload.get("hand_id"),
        reason=payload.get("reason"),
    )


def _deserialize_hand(payload: dict[str, Any]) -> HandState:
    resolution = _deserialize_resolution(payload.get("resolution"))
    return HandState(
        hand_id=payload["hand_id"],
        player_id=payload["player_id"],
        seat_number=payload["seat_number"],
        cards=tuple(_deserialize_card(card) for card in payload.get("cards", [])),
        wager=_deserialize_bet(payload.get("wager")),
        insurance_wager=_deserialize_bet(payload.get("insurance_wager")),
        status=HandStatus(payload["status"]),
        split_depth=payload.get("split_depth", 0),
        parent_hand_id=payload.get("parent_hand_id"),
        doubled_down=payload.get("doubled_down", False),
        resolution=resolution,
    )


def _deserialize_resolution(payload: dict[str, Any] | None) -> HandResolution | None:
    if payload is None:
        return None
    return HandResolution(
        outcome=HandOutcome(payload["outcome"]),
        reason=ResolutionReason(payload["reason"]),
        net_change=payload["net_change"],
        player_total=payload.get("player_total"),
        dealer_total=payload.get("dealer_total"),
    )


def _deserialize_bet(payload: dict[str, Any] | None) -> Bet | None:
    if payload is None:
        return None
    return Bet(
        amount=payload["amount"],
        currency=payload.get("currency", "USD"),
    )


def _deserialize_card(payload: dict[str, Any]) -> Card:
    return Card(
        rank=CardRank(payload["rank"]),
        suit=CardSuit(payload["suit"]),
    )


def _deserialize_rules(payload: dict[str, Any]) -> RuleConfig:
    blackjack_payout = payload.get("blackjack_payout", {})
    return RuleConfig(
        deck_count=payload.get("deck_count", 6),
        dealer_stands_on_soft_17=payload.get("dealer_stands_on_soft_17", True),
        blackjack_payout=PayoutRatio(
            numerator=blackjack_payout.get("numerator", 3),
            denominator=blackjack_payout.get("denominator", 2),
        ),
        minimum_bet=payload.get("minimum_bet", 10),
        maximum_bet=payload.get("maximum_bet", 500),
        allow_double_after_split=payload.get("allow_double_after_split", True),
        maximum_split_depth=payload.get("maximum_split_depth", 3),
        split_on_value_match=payload.get("split_on_value_match", False),
    )
