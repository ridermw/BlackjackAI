from __future__ import annotations

import hashlib
import secrets
from collections import Counter
from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from random import Random
from threading import RLock
from typing import Any
from typing import Sequence
from typing import TYPE_CHECKING
from uuid import uuid4

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
from blackjack_ai.engine import PlayerProfile
from blackjack_ai.engine import ResolutionReason
from blackjack_ai.engine import RoundParticipantState
from blackjack_ai.engine import RoundPhase
from blackjack_ai.engine import RoundState
from blackjack_ai.engine import RuleConfig
from blackjack_ai.engine import SeatState
from blackjack_ai.engine import SeatStatus
from blackjack_ai.engine import ShoeState
from blackjack_ai.engine import TableSessionState
from blackjack_ai.engine import TableSessionStatus
from blackjack_ai.engine import TurnState

from blackjack_ai.api.schemas import ActionRequest
from blackjack_ai.api.schemas import BetRequest
from blackjack_ai.api.schemas import CreatePlayerRequest
from blackjack_ai.api.schemas import CreateTableRequest
from blackjack_ai.api.schemas import SeatLeaveRequest
from blackjack_ai.api.schemas import SeatJoinRequest
from blackjack_ai.api.schemas import StartRoundRequest


if TYPE_CHECKING:
    from blackjack_ai.persistence import GameRepository


class ApiServiceError(Exception):
    status_code = 400

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class InvalidRequestError(ApiServiceError):
    status_code = 400


class NotFoundError(ApiServiceError):
    status_code = 404


class ConflictError(ApiServiceError):
    status_code = 409


class UnauthorizedError(ApiServiceError):
    status_code = 401


class ForbiddenError(ApiServiceError):
    status_code = 403


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _build_identifier(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def _build_shoe(rules: RuleConfig, randomizer: Random) -> deque[Card]:
    cards = [
        Card(rank=rank, suit=suit)
        for _ in range(rules.deck_count)
        for suit in CardSuit
        for rank in CardRank
    ]
    randomizer.shuffle(cards)
    return deque(cards)


_MINIMUM_CUT_CARD_REMAINING = 20
_CUT_CARD_REMAINING_DIVISOR = 4


def _payout_amount(amount: int, rules: RuleConfig) -> int:
    ratio = rules.blackjack_payout
    return (amount * ratio.numerator) // ratio.denominator


def _hand_display_total(hand: HandState) -> int | None:
    value = hand.value
    return value.best_total if value.best_total is not None else value.hard_total


def _dealer_up_card(dealer: DealerState) -> Card | None:
    visible_cards = dealer.visible_cards()
    return visible_cards[0] if visible_cards else None


def _dealer_shows_ace_from_cards(cards: tuple[Card, ...]) -> bool:
    return bool(cards) and cards[0].rank is CardRank.ACE


def _insurance_net_change(participant: RoundParticipantRecord, dealer_hand: HandState) -> int:
    insurance_wager = next(
        (hand.insurance_wager for hand in participant.hands if hand.insurance_wager is not None),
        None,
    )
    if insurance_wager is None:
        return 0
    return insurance_wager.amount * 2 if dealer_hand.value.is_blackjack else -insurance_wager.amount


def _player_token_digest(player_token: str) -> str:
    return hashlib.sha256(player_token.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class PlayerStatsRecord:
    bankroll_delta: int = 0
    rounds_played: int = 0
    hands_played: int = 0
    wins: int = 0
    pushes: int = 0
    losses: int = 0
    blackjack_count: int = 0
    bust_count: int = 0
    action_counts: Counter[str] = field(default_factory=Counter)

    @property
    def bust_rate(self) -> float:
        return round(self.bust_count / self.hands_played, 4) if self.hands_played else 0.0

    @property
    def average_return_per_hand(self) -> float:
        return round(self.bankroll_delta / self.hands_played, 4) if self.hands_played else 0.0

    def to_dict(self) -> dict[str, Any]:
        total_actions = sum(self.action_counts.values())
        return {
            "bankroll_delta": self.bankroll_delta,
            "rounds_played": self.rounds_played,
            "hands_played": self.hands_played,
            "wins": self.wins,
            "pushes": self.pushes,
            "losses": self.losses,
            "blackjack_count": self.blackjack_count,
            "bust_rate": self.bust_rate,
            "average_return_per_hand": self.average_return_per_hand,
            "action_counts": {action.value: self.action_counts.get(action.value, 0) for action in ActionType},
            "action_distribution": {
                action.value: round(self.action_counts.get(action.value, 0) / total_actions, 4) if total_actions else 0.0
                for action in ActionType
            },
        }


@dataclass(slots=True)
class PlayerRecord:
    profile: PlayerProfile
    starting_bankroll: int
    player_token_digest: str = ""
    stats: PlayerStatsRecord = field(default_factory=PlayerStatsRecord)

    def to_dict(self) -> dict[str, Any]:
        payload = self.profile.to_internal_dict()
        payload["starting_bankroll"] = self.starting_bankroll
        payload["stats"] = self.stats.to_dict()
        return payload


@dataclass(slots=True)
class SeatRecord:
    seat_number: int
    player_id: str
    bankroll: int
    ready_for_next_round: bool = True
    active_hand_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RoundParticipantRecord:
    player_id: str
    display_name: str
    participant_type: ParticipantType
    seat_number: int
    bankroll_before_round: int
    hands: list[HandState] = field(default_factory=list)
    bankroll_after_round: int | None = None
    opening_bet: Bet | None = None

    def to_engine_state(self) -> RoundParticipantState:
        return RoundParticipantState(
            player_id=self.player_id,
            display_name=self.display_name,
            participant_type=self.participant_type,
            seat_number=self.seat_number,
            bankroll_before_round=self.bankroll_before_round,
            hands=tuple(self.hands),
            bankroll_after_round=self.bankroll_after_round,
        )

    @property
    def available_bankroll(self) -> int:
        return self.to_engine_state().available_bankroll


@dataclass(slots=True)
class RoundEventRecord:
    event_id: str
    sequence: int
    event_type: str
    round_id: str
    table_id: str
    timestamp: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "round_id": self.round_id,
            "table_id": self.table_id,
            "timestamp": self.timestamp,
            "payload": self.payload,
        }


@dataclass(slots=True)
class IdempotentRequestRecord:
    payload: dict[str, Any]
    response: dict[str, Any]


@dataclass(slots=True)
class TableRecord:
    table_id: str
    rules: RuleConfig
    seat_count: int
    status: TableSessionStatus = TableSessionStatus.OPEN
    active_round_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    seats: dict[int, SeatRecord] = field(default_factory=dict)
    shoe: deque[Card] = field(default_factory=deque)
    shuffle_count: int = 0

    def occupied_seats(self) -> list[SeatRecord]:
        return sorted(self.seats.values(), key=lambda seat: seat.seat_number)

    def current_shoe_state(self) -> ShoeState:
        return ShoeState.from_cards_remaining(len(self.shoe), shuffle_count=self.shuffle_count)

    def to_public_dict(self, players: dict[str, PlayerRecord]) -> dict[str, Any]:
        is_active = self.active_round_id is not None
        seats: list[SeatState] = []
        for seat_number in range(1, self.seat_count + 1):
            seat = self.seats.get(seat_number)
            if seat is None:
                seats.append(SeatState(seat_number=seat_number))
                continue

            status = SeatStatus.SEATED if is_active or seat.ready_for_next_round else SeatStatus.SITTING_OUT
            seats.append(
                SeatState(
                    seat_number=seat_number,
                    status=status,
                    occupant=players[seat.player_id].profile,
                    bankroll=seat.bankroll,
                    ready_for_next_round=seat.ready_for_next_round,
                    active_hand_ids=tuple(seat.active_hand_ids),
                )
            )

        payload = TableSessionState(
            table_id=self.table_id,
            status=self.status,
            rules=self.rules,
            seats=tuple(seats),
            shoe_state=self.current_shoe_state(),
            active_round_id=self.active_round_id,
            metadata=self.metadata,
        ).to_public_dict()
        payload["seat_count"] = self.seat_count
        return payload


@dataclass(slots=True)
class RoundRecord:
    round_id: str
    table_id: str
    rules: RuleConfig
    participants: list[RoundParticipantRecord]
    dealer: DealerState
    shoe: deque[Card]
    shuffle_count: int = 0
    phase: RoundPhase = RoundPhase.WAITING_FOR_BETS
    turn: TurnState | None = None
    action_count: int = 0
    version: int = 0
    events: list[RoundEventRecord] = field(default_factory=list)
    event_sequence: int = 0
    request_history: dict[str, dict[str, IdempotentRequestRecord]] = field(default_factory=dict)

    def current_shoe_state(self) -> ShoeState:
        return ShoeState.from_cards_remaining(len(self.shoe), shuffle_count=self.shuffle_count)

    def to_round_state(self) -> RoundState:
        return RoundState(
            round_id=self.round_id,
            table_id=self.table_id,
            phase=self.phase,
            rules=self.rules,
            participants=tuple(participant.to_engine_state() for participant in self.participants),
            dealer=self.dealer,
            shoe_state=self.current_shoe_state(),
            turn=self.turn,
            action_count=self.action_count,
            version=self.version,
        )

    def pending_player_ids(self) -> list[str]:
        return [
            participant.player_id
            for participant in self.participants
            if participant.opening_bet is None
        ]

    def accepted_bets(self) -> list[dict[str, Any]]:
        accepted: list[dict[str, Any]] = []
        for participant in self.participants:
            if participant.opening_bet is None:
                continue
            accepted.append(
                {
                    "player_id": participant.player_id,
                    "seat_number": participant.seat_number,
                    "bet": participant.opening_bet.to_dict(),
                }
            )
        return accepted

    def next_request(self) -> dict[str, Any]:
        if self.phase is RoundPhase.WAITING_FOR_BETS:
            return {
                "type": "bet",
                "pending_player_ids": self.pending_player_ids(),
                "minimum_bet": self.rules.minimum_bet,
                "maximum_bet": self.rules.maximum_bet,
            }
        if self.phase is RoundPhase.PLAYER_TURNS and self.turn is not None:
            return {
                "type": "action",
                "player_id": self.turn.player_id,
                "hand_id": self.turn.hand_id,
                "legal_actions": [action.value for action in self.turn.legal_actions],
            }
        if self.phase is RoundPhase.COMPLETE:
            return {"type": "round_complete"}
        return {"type": "wait", "reason": self.phase.value}

    def to_public_dict(self) -> dict[str, Any]:
        payload = self.to_round_state().to_public_dict()
        payload["betting"] = {
            "pending_player_ids": self.pending_player_ids(),
            "accepted_bets": self.accepted_bets(),
        }
        payload["next_request"] = self.next_request()
        return payload

    def log_event(self, event_type: str, payload: dict[str, Any]) -> None:
        self.event_sequence += 1
        self.events.append(
            RoundEventRecord(
                event_id=_build_identifier("event"),
                sequence=self.event_sequence,
                event_type=event_type,
                round_id=self.round_id,
                table_id=self.table_id,
                timestamp=_utc_now(),
                payload=payload,
            )
        )


class GameService:
    def __init__(
        self,
        *,
        randomizer: Random | None = None,
        repository: GameRepository | None = None,
    ) -> None:
        self._lock = RLock()
        self._randomizer = randomizer or Random()
        self._repository = repository
        self._test_shoes: deque[list[Card]] = deque()
        self.players: dict[str, PlayerRecord] = {}
        self.tables: dict[str, TableRecord] = {}
        self.rounds: dict[str, RoundRecord] = {}
        if self._repository is not None:
            persisted_state = self._repository.load_state()
            self.players.update(persisted_state.players)
            self.tables.update(persisted_state.tables)
            self.rounds.update(persisted_state.rounds)
            self._bind_active_round_shoes()

    def queue_test_shoe(self, cards: Sequence[Card]) -> None:
        with self._lock:
            self._test_shoes.append(list(cards))

    def register_player(self, payload: CreatePlayerRequest) -> dict[str, Any]:
        with self._lock:
            player_id = payload.player_id or _build_identifier("player")
            if player_id in self.players:
                raise ConflictError(f"Player '{player_id}' already exists.")

            player_token = secrets.token_hex(32)
            player = PlayerRecord(
                profile=PlayerProfile(
                    player_id=player_id,
                    display_name=payload.display_name,
                    participant_type=payload.participant_type,
                    metadata=payload.metadata,
                ),
                starting_bankroll=payload.starting_bankroll,
                player_token_digest=_player_token_digest(player_token),
            )
            self.players[player_id] = player
            self._persist(players=(player,))
            response = player.to_dict()
            response["player_token"] = player_token
            return response

    def get_player(self, player_id: str) -> dict[str, Any]:
        with self._lock:
            return self._player(player_id).to_dict()

    def get_player_stats(self, player_id: str) -> dict[str, Any]:
        with self._lock:
            player = self._player(player_id)
            return {
                "player_id": player.profile.player_id,
                "display_name": player.profile.display_name,
                "participant_type": player.profile.participant_type.value,
                "stats": player.stats.to_dict(),
            }

    def require_player_token(self, player_id: str, player_token: str | None) -> None:
        with self._lock:
            self._verify_player_token(self._player(player_id), player_token)

    def create_table(self, payload: CreateTableRequest) -> dict[str, Any]:
        with self._lock:
            table_id = payload.table_id or _build_identifier("table")
            if table_id in self.tables:
                raise ConflictError(f"Table '{table_id}' already exists.")

            try:
                rules = payload.rules.to_domain() if payload.rules is not None else RuleConfig()
            except ValueError as exc:
                raise InvalidRequestError(str(exc)) from exc

            table = TableRecord(
                table_id=table_id,
                rules=rules,
                seat_count=payload.seat_count,
                metadata=dict(payload.metadata),
            )
            self.tables[table_id] = table
            self._persist(tables=(table,))
            return table.to_public_dict(self.players)

    def get_table(self, table_id: str) -> dict[str, Any]:
        with self._lock:
            return self._table(table_id).to_public_dict(self.players)

    def join_table_seat(self, table_id: str, seat_number: int, payload: SeatJoinRequest) -> dict[str, Any]:
        with self._lock:
            table = self._table(table_id)
            player = self._player(payload.player_id)

            if seat_number < 1 or seat_number > table.seat_count:
                raise InvalidRequestError(f"Seat number must be between 1 and {table.seat_count}.")
            if table.active_round_id is not None:
                raise ConflictError("Cannot change seating while a round is active.")
            if seat_number in table.seats:
                raise ConflictError(f"Seat {seat_number} is already occupied.")
            if any(seat.player_id == player.profile.player_id for seat in table.seats.values()):
                raise ConflictError(f"Player '{player.profile.player_id}' is already seated at table '{table_id}'.")

            bankroll = payload.bankroll or player.starting_bankroll
            table.seats[seat_number] = SeatRecord(
                seat_number=seat_number,
                player_id=player.profile.player_id,
                bankroll=bankroll,
                ready_for_next_round=bankroll >= table.rules.minimum_bet,
            )
            self._persist(tables=(table,))
            return table.to_public_dict(self.players)

    def leave_table_seat(self, table_id: str, seat_number: int, payload: SeatLeaveRequest) -> dict[str, Any]:
        with self._lock:
            table = self._table(table_id)
            player = self._player(payload.player_id)

            if seat_number < 1 or seat_number > table.seat_count:
                raise InvalidRequestError(f"Seat number must be between 1 and {table.seat_count}.")
            if table.active_round_id is not None:
                raise ConflictError("Cannot change seating while a round is active.")
            if seat_number not in table.seats:
                raise ConflictError(f"Seat {seat_number} is empty.")

            seat = table.seats[seat_number]
            if seat.player_id != player.profile.player_id:
                raise ForbiddenError(f"Only player '{seat.player_id}' can leave seat {seat_number}.")

            del table.seats[seat_number]
            self._persist(tables=(table,))
            return table.to_public_dict(self.players)

    def start_round(self, table_id: str, payload: StartRoundRequest | None = None) -> dict[str, Any]:
        with self._lock:
            table = self._table(table_id)
            if table.active_round_id is not None:
                raise ConflictError(f"Table '{table_id}' already has an active round.")
            if table.status is TableSessionStatus.CLOSED:
                raise ConflictError(f"Table '{table_id}' is closed.")

            eligible_seats = [
                seat
                for seat in table.occupied_seats()
                if seat.ready_for_next_round and seat.bankroll >= table.rules.minimum_bet
            ]
            if not eligible_seats:
                raise ConflictError("No seated players are ready for the next round.")

            round_id = (payload.round_id if payload is not None else None) or _build_identifier("round")
            if round_id in self.rounds:
                raise ConflictError(f"Round '{round_id}' already exists.")

            previous_shuffle_count = table.shuffle_count
            reshuffle_reason = self._prepare_table_shoe(table)
            participants = [
                RoundParticipantRecord(
                    player_id=self.players[seat.player_id].profile.player_id,
                    display_name=self.players[seat.player_id].profile.display_name,
                    participant_type=self.players[seat.player_id].profile.participant_type,
                    seat_number=seat.seat_number,
                    bankroll_before_round=seat.bankroll,
                )
                for seat in eligible_seats
            ]
            for seat in eligible_seats:
                seat.ready_for_next_round = False
                seat.active_hand_ids.clear()

            round_record = RoundRecord(
                round_id=round_id,
                table_id=table_id,
                rules=table.rules,
                participants=participants,
                dealer=self._empty_dealer(round_id),
                shoe=table.shoe,
                shuffle_count=table.shuffle_count,
            )
            round_record.log_event(
                "round_started",
                {
                    "participants": [
                        {
                            "player_id": participant.player_id,
                            "display_name": participant.display_name,
                            "participant_type": participant.participant_type.value,
                            "seat_number": participant.seat_number,
                            "bankroll_before_round": participant.bankroll_before_round,
                        }
                        for participant in participants
                    ],
                    "pending_player_ids": round_record.pending_player_ids(),
                },
            )
            if reshuffle_reason is not None and previous_shuffle_count > 0:
                self._log_shoe_reshuffled_event(round_record, reason=reshuffle_reason)

            table.active_round_id = round_id
            table.status = TableSessionStatus.ACTIVE
            self.rounds[round_id] = round_record
            self._persist(tables=(table,), rounds=(round_record,))
            return round_record.to_public_dict()

    def get_round(self, round_id: str) -> dict[str, Any]:
        with self._lock:
            return self._round(round_id).to_public_dict()

    def place_bet(self, round_id: str, payload: BetRequest) -> dict[str, Any]:
        with self._lock:
            round_record = self._round(round_id)
            replayed_response = self._replayed_public_request(round_record, payload)
            if replayed_response is not None:
                return replayed_response
            self._enforce_expected_round_version(round_record, payload.expected_version)
            if round_record.phase is not RoundPhase.WAITING_FOR_BETS:
                raise ConflictError(f"Round '{round_id}' is no longer accepting bets.")

            participant = self._round_participant(round_record, payload.player_id)
            if participant.opening_bet is not None:
                raise ConflictError(f"Player '{payload.player_id}' has already placed an opening bet.")

            bet = Bet(amount=payload.amount)
            try:
                bet.validate(round_record.rules, participant.available_bankroll)
            except ValueError as exc:
                raise InvalidRequestError(str(exc)) from exc

            round_record.version += 1
            participant.opening_bet = bet
            participant.hands = [
                HandState(
                    hand_id=self._new_hand_id(round_record.round_id),
                    player_id=participant.player_id,
                    seat_number=participant.seat_number,
                    wager=bet,
                    status=HandStatus.PENDING,
                )
            ]
            self._sync_table_seats(round_record)
            round_record.log_event(
                "bet_placed",
                {
                    "player_id": participant.player_id,
                    "seat_number": participant.seat_number,
                    "bet": bet.to_dict(),
                },
            )

            if not round_record.pending_player_ids():
                self._deal_initial_cards(round_record)

            response = round_record.to_public_dict()
            self._remember_public_request(round_record, payload, response)
            self._persist(
                players=self._round_players(round_record),
                tables=(self._table(round_record.table_id),),
                rounds=(round_record,),
            )
            return response

    def apply_action(self, round_id: str, payload: ActionRequest) -> dict[str, Any]:
        with self._lock:
            round_record = self._round(round_id)
            replayed_response = self._replayed_public_request(round_record, payload)
            if replayed_response is not None:
                return replayed_response
            self._enforce_expected_round_version(round_record, payload.expected_version)
            if round_record.phase is not RoundPhase.PLAYER_TURNS or round_record.turn is None:
                raise ConflictError(f"Round '{round_id}' is not waiting for a player action.")
            if payload.player_id != round_record.turn.player_id or payload.hand_id != round_record.turn.hand_id:
                raise ConflictError("It is not this player's turn.")
            if payload.action not in round_record.turn.legal_actions:
                raise ConflictError(f"Action '{payload.action.value}' is not legal right now.")

            participant = self._round_participant(round_record, payload.player_id)
            hand_index, hand = self._hand(participant, payload.hand_id)
            round_record.version += 1
            round_record.action_count += 1
            self.players[participant.player_id].stats.action_counts[payload.action.value] += 1

            if payload.action is ActionType.HIT:
                updated_hand = self._normalize_live_hand(
                    replace(
                        hand,
                        cards=hand.cards + (self._draw(round_record),),
                        status=HandStatus.ACTIVE,
                    )
                )
                participant.hands[hand_index] = updated_hand

            elif payload.action is ActionType.STAND:
                participant.hands[hand_index] = replace(hand, status=HandStatus.STANDING)

            elif payload.action is ActionType.DOUBLE:
                if hand.wager is None:
                    raise ConflictError("Cannot double a hand without an opening bet.")
                updated_hand = replace(
                    hand,
                    wager=Bet(amount=hand.wager.amount * 2, currency=hand.wager.currency),
                    doubled_down=True,
                    cards=hand.cards + (self._draw(round_record),),
                    status=HandStatus.STANDING,
                )
                if updated_hand.value.is_bust:
                    updated_hand = replace(updated_hand, status=HandStatus.BUSTED)
                participant.hands[hand_index] = updated_hand

            elif payload.action is ActionType.SPLIT:
                if hand.wager is None or len(hand.cards) != 2:
                    raise ConflictError("Only two-card wagered hands can be split.")
                first_hand = HandState(
                    hand_id=self._new_hand_id(round_record.round_id),
                    player_id=participant.player_id,
                    seat_number=participant.seat_number,
                    cards=(hand.cards[0], self._draw(round_record)),
                    wager=hand.wager,
                    insurance_wager=hand.insurance_wager,
                    status=HandStatus.ACTIVE,
                    split_depth=hand.split_depth + 1,
                    parent_hand_id=hand.hand_id,
                )
                second_hand = HandState(
                    hand_id=self._new_hand_id(round_record.round_id),
                    player_id=participant.player_id,
                    seat_number=participant.seat_number,
                    cards=(hand.cards[1], self._draw(round_record)),
                    wager=hand.wager,
                    status=HandStatus.ACTIVE,
                    split_depth=hand.split_depth + 1,
                    parent_hand_id=hand.hand_id,
                )
                participant.hands[hand_index : hand_index + 1] = [
                    self._normalize_live_hand(first_hand),
                    self._normalize_live_hand(second_hand),
                ]

            elif payload.action is ActionType.SURRENDER:
                if hand.wager is None:
                    raise ConflictError("Cannot surrender a hand without an opening bet.")
                participant.hands[hand_index] = replace(
                    hand,
                    status=HandStatus.COMPLETE,
                    resolution=HandResolution(
                        outcome=HandOutcome.LOSS,
                        reason=ResolutionReason.SURRENDER,
                        net_change=-(hand.wager.amount // 2),
                        player_total=_hand_display_total(hand),
                        dealer_total=None,
                    ),
                )

            elif payload.action is ActionType.INSURANCE:
                if hand.wager is None:
                    raise ConflictError("Cannot insure a hand without an opening bet.")
                participant.hands[hand_index] = replace(
                    hand,
                    insurance_wager=Bet(amount=hand.wager.amount // 2, currency=hand.wager.currency),
                )

            round_record.log_event(
                "player_action",
                {
                    "player_id": participant.player_id,
                    "seat_number": participant.seat_number,
                    "hand_id": payload.hand_id,
                    "action": payload.action.value,
                    "resulting_hands": [hand_state.to_dict() for hand_state in participant.hands],
                },
            )

            self._sync_table_seats(round_record)
            self._recompute_turn_or_resolve(round_record)
            response = round_record.to_public_dict()
            self._remember_public_request(round_record, payload, response)
            self._persist(
                players=self._round_players(round_record),
                tables=(self._table(round_record.table_id),),
                rounds=(round_record,),
            )
            return response

    def get_round_events(
        self,
        round_id: str,
        *,
        limit: int | None = None,
        after_sequence: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            round_record = self._round(round_id)
            last_sequence = round_record.events[-1].sequence if round_record.events else 0
            resolved_after_sequence = min(after_sequence, last_sequence) if after_sequence is not None else 0

            if after_sequence is None:
                events = round_record.events[-limit:] if limit is not None else round_record.events
            else:
                events_after_cursor = [
                    event
                    for event in round_record.events
                    if event.sequence > resolved_after_sequence
                ]
                events = events_after_cursor[:limit] if limit is not None else events_after_cursor

            next_after_sequence = events[-1].sequence if events else resolved_after_sequence
            return {
                "round_id": round_id,
                "after_sequence": resolved_after_sequence,
                "count": len(events),
                "total_count": len(round_record.events),
                "last_sequence": last_sequence,
                "next_after_sequence": next_after_sequence,
                "has_more": next_after_sequence < last_sequence,
                "events": [event.to_dict() for event in events],
            }

    def get_leaderboard(self, *, participant_type: ParticipantType | None = None) -> dict[str, Any]:
        with self._lock:
            records = list(self.players.values())
            if participant_type is not None:
                records = [
                    record
                    for record in records
                    if record.profile.participant_type is participant_type
                ]

            records.sort(
                key=lambda record: (
                    -record.stats.bankroll_delta,
                    -record.stats.wins,
                    -record.stats.average_return_per_hand if record.stats.hands_played else 0.0,
                    record.profile.display_name.lower(),
                )
            )

            entries = []
            for rank, record in enumerate(records, start=1):
                entries.append(
                    {
                        "rank": rank,
                        "player_id": record.profile.player_id,
                        "display_name": record.profile.display_name,
                        "participant_type": record.profile.participant_type.value,
                        "stats": record.stats.to_dict(),
                    }
                )

            return {
                "sorted_by": "bankroll_delta",
                "total_players": len(entries),
                "entries": entries,
            }

    def _persist(
        self,
        *,
        players: Sequence[PlayerRecord] = (),
        tables: Sequence[TableRecord] = (),
        rounds: Sequence[RoundRecord] = (),
    ) -> None:
        if self._repository is None:
            return
        self._repository.persist(players=players, tables=tables, rounds=rounds)

    def _replayed_public_request(
        self,
        round_record: RoundRecord,
        payload: BetRequest | ActionRequest,
    ) -> dict[str, Any] | None:
        if payload.request_id is None:
            return None

        request_record = round_record.request_history.get(payload.player_id, {}).get(payload.request_id)
        if request_record is None:
            return None

        request_payload = self._public_request_payload(payload)
        if request_record.payload != request_payload:
            raise ConflictError(
                f"Request identifier '{payload.request_id}' for player '{payload.player_id}' was already used with a different payload."
            )
        return deepcopy(request_record.response)

    def _enforce_expected_round_version(self, round_record: RoundRecord, expected_version: int | None) -> None:
        if expected_version is None:
            return
        if round_record.version != expected_version:
            raise ConflictError(
                f"Round '{round_record.round_id}' version mismatch: expected version {expected_version}, found {round_record.version}."
            )

    def _remember_public_request(
        self,
        round_record: RoundRecord,
        payload: BetRequest | ActionRequest,
        response: dict[str, Any],
    ) -> None:
        if payload.request_id is None:
            return

        round_record.request_history.setdefault(payload.player_id, {})[payload.request_id] = IdempotentRequestRecord(
            payload=deepcopy(self._public_request_payload(payload)),
            response=deepcopy(response),
        )

    @staticmethod
    def _public_request_payload(payload: BetRequest | ActionRequest) -> dict[str, Any]:
        return payload.model_dump(mode="json", exclude={"request_id", "expected_version"}, exclude_none=True)

    def _round_players(self, round_record: RoundRecord) -> list[PlayerRecord]:
        return [self.players[participant.player_id] for participant in round_record.participants]

    def _verify_player_token(self, player: PlayerRecord, player_token: str | None) -> None:
        if not player_token:
            raise UnauthorizedError("X-Player-Token header is required for this player operation.")

        presented_digest = _player_token_digest(player_token)
        stored_digest = player.player_token_digest or ("0" * len(presented_digest))
        if not secrets.compare_digest(stored_digest, presented_digest):
            raise ForbiddenError(f"Invalid player token for player '{player.profile.player_id}'.")

    def _player(self, player_id: str) -> PlayerRecord:
        try:
            return self.players[player_id]
        except KeyError as exc:
            raise NotFoundError(f"Player '{player_id}' was not found.") from exc

    def _table(self, table_id: str) -> TableRecord:
        try:
            return self.tables[table_id]
        except KeyError as exc:
            raise NotFoundError(f"Table '{table_id}' was not found.") from exc

    def _round(self, round_id: str) -> RoundRecord:
        try:
            return self.rounds[round_id]
        except KeyError as exc:
            raise NotFoundError(f"Round '{round_id}' was not found.") from exc

    def _round_participant(self, round_record: RoundRecord, player_id: str) -> RoundParticipantRecord:
        for participant in round_record.participants:
            if participant.player_id == player_id:
                return participant
        raise NotFoundError(f"Player '{player_id}' is not part of round '{round_record.round_id}'.")

    def _hand(self, participant: RoundParticipantRecord, hand_id: str) -> tuple[int, HandState]:
        for index, hand in enumerate(participant.hands):
            if hand.hand_id == hand_id:
                return index, hand
        raise NotFoundError(f"Hand '{hand_id}' was not found for player '{participant.player_id}'.")

    def _empty_dealer(self, round_id: str) -> DealerState:
        return DealerState(
            hand=HandState(
                hand_id=f"{round_id}-dealer",
                player_id="dealer",
                seat_number=0,
                status=HandStatus.PENDING,
            ),
            hole_card_index=1,
            hole_card_revealed=False,
        )

    def _next_shoe(self, rules: RuleConfig) -> deque[Card]:
        if self._test_shoes:
            return deque(self._test_shoes.popleft())
        return _build_shoe(rules, self._randomizer)

    def _bind_active_round_shoes(self) -> None:
        for table in self.tables.values():
            if table.active_round_id is None:
                continue
            round_record = self.rounds.get(table.active_round_id)
            if round_record is None:
                continue
            if not table.shoe and round_record.shoe:
                table.shoe = round_record.shoe
            if table.shuffle_count == 0 and round_record.shuffle_count > 0:
                table.shuffle_count = round_record.shuffle_count
            round_record.shoe = table.shoe
            round_record.shuffle_count = table.shuffle_count

    def _cut_card_remaining(self, rules: RuleConfig) -> int:
        return max(_MINIMUM_CUT_CARD_REMAINING, (rules.deck_count * 52) // _CUT_CARD_REMAINING_DIVISOR)

    def _prepare_table_shoe(self, table: TableRecord) -> str | None:
        if table.shoe and len(table.shoe) > self._cut_card_remaining(table.rules):
            return None
        reason = "cut_card_reached" if table.shoe else "shoe_empty"
        table.shoe = self._next_shoe(table.rules)
        table.shuffle_count += 1
        return reason

    def _log_shoe_reshuffled_event(self, round_record: RoundRecord, *, reason: str) -> None:
        table = self._table(round_record.table_id)
        round_record.shoe = table.shoe
        round_record.shuffle_count = table.shuffle_count
        round_record.log_event(
            "shoe_reshuffled",
            {
                "reason": reason,
                "shoe_state": round_record.current_shoe_state().to_dict(),
            },
        )

    def _new_hand_id(self, round_id: str) -> str:
        return f"{round_id}-hand-{uuid4().hex[:8]}"

    def _draw(self, round_record: RoundRecord) -> Card:
        table = self._table(round_record.table_id)
        previous_shuffle_count = table.shuffle_count
        reshuffle_reason = None
        if not table.shoe:
            reshuffle_reason = self._prepare_table_shoe(table)
        round_record.shoe = table.shoe
        round_record.shuffle_count = table.shuffle_count
        if reshuffle_reason is not None and previous_shuffle_count > 0:
            self._log_shoe_reshuffled_event(round_record, reason=reshuffle_reason)
        return table.shoe.popleft()

    def _deal_initial_cards(self, round_record: RoundRecord) -> None:
        for participant in round_record.participants:
            opening_hand = participant.hands[0]
            participant.hands[0] = replace(
                opening_hand,
                cards=(self._draw(round_record),),
                status=HandStatus.PENDING,
            )

        dealer_hand = replace(
            round_record.dealer.hand,
            cards=(self._draw(round_record),),
            status=HandStatus.PENDING,
        )
        round_record.dealer = replace(round_record.dealer, hand=dealer_hand)

        for participant in round_record.participants:
            current_hand = participant.hands[0]
            participant.hands[0] = self._normalize_live_hand(
                replace(
                    current_hand,
                    cards=current_hand.cards + (self._draw(round_record),),
                    status=HandStatus.ACTIVE,
                )
            )

        dealer_hand = replace(
            round_record.dealer.hand,
            cards=round_record.dealer.hand.cards + (self._draw(round_record),),
            status=HandStatus.ACTIVE,
        )
        if dealer_hand.value.is_blackjack:
            dealer_hand = replace(
                dealer_hand,
                status=HandStatus.ACTIVE if _dealer_shows_ace_from_cards(dealer_hand.cards) else HandStatus.BLACKJACK,
            )
        round_record.dealer = replace(round_record.dealer, hand=dealer_hand)

        round_record.phase = RoundPhase.DEALING
        round_record.log_event(
            "initial_cards_dealt",
            {
                "round": round_record.to_round_state().to_public_dict(),
            },
        )
        self._sync_table_seats(round_record)
        self._recompute_turn_or_resolve(round_record)

    def _normalize_live_hand(self, hand: HandState) -> HandState:
        if hand.value.is_bust:
            return replace(hand, status=HandStatus.BUSTED)
        if hand.value.is_blackjack:
            return replace(hand, status=HandStatus.BLACKJACK)
        if hand.value.best_total == 21:
            return replace(hand, status=HandStatus.STANDING)
        return hand

    def _recompute_turn_or_resolve(self, round_record: RoundRecord) -> None:
        for participant in round_record.participants:
            for index, hand in enumerate(participant.hands):
                participant.hands[index] = self._normalize_live_hand(hand) if hand.status is HandStatus.ACTIVE else hand

        if round_record.dealer.hand.status is HandStatus.BLACKJACK:
            self._resolve_round(round_record)
            return

        dealer_up_card = _dealer_up_card(round_record.dealer)
        for participant in round_record.participants:
            participant_state = participant.to_engine_state()
            for hand in participant.hands:
                if hand.status is not HandStatus.ACTIVE:
                    continue
                legal_actions = hand.legal_actions(
                    round_record.rules,
                    participant_state.available_bankroll,
                    dealer_up_card=dealer_up_card,
                )
                if legal_actions:
                    round_record.phase = RoundPhase.PLAYER_TURNS
                    round_record.turn = TurnState.for_player_hand(
                        participant_state,
                        hand,
                        round_record.rules,
                        dealer_up_card=dealer_up_card,
                    )
                    self._sync_table_seats(round_record)
                    return

        round_record.turn = None
        self._resolve_round(round_record)

    def _resolve_round(self, round_record: RoundRecord) -> None:
        table = self._table(round_record.table_id)
        round_record.phase = RoundPhase.DEALER_TURN

        if round_record.dealer.hand.cards:
            round_record.dealer = replace(round_record.dealer, hole_card_revealed=True)
            round_record.log_event(
                "dealer_revealed",
                {
                    "dealer": round_record.dealer.to_public_dict(reveal_hidden=True),
                },
            )

        if self._dealer_should_play(round_record):
            dealer_hand = round_record.dealer.hand
            dealer_hand = replace(
                dealer_hand,
                status=HandStatus.ACTIVE if not dealer_hand.value.is_blackjack else HandStatus.BLACKJACK,
            )
            round_record.dealer = replace(round_record.dealer, hand=dealer_hand, hole_card_revealed=True)

            while round_record.dealer.should_hit(round_record.rules):
                dealer_hand = replace(
                    round_record.dealer.hand,
                    cards=round_record.dealer.hand.cards + (self._draw(round_record),),
                    status=HandStatus.ACTIVE,
                )
                if dealer_hand.value.is_bust:
                    dealer_hand = replace(dealer_hand, status=HandStatus.BUSTED)
                round_record.dealer = replace(round_record.dealer, hand=dealer_hand, hole_card_revealed=True)
                round_record.log_event(
                    "dealer_hit",
                    {
                        "dealer": round_record.dealer.to_public_dict(reveal_hidden=True),
                    },
                )

        dealer_hand = round_record.dealer.hand
        if dealer_hand.value.is_blackjack:
            dealer_hand = replace(dealer_hand, status=HandStatus.BLACKJACK)
        elif dealer_hand.value.is_bust:
            dealer_hand = replace(dealer_hand, status=HandStatus.BUSTED)
        else:
            dealer_hand = replace(dealer_hand, status=HandStatus.STANDING)
        round_record.dealer = replace(round_record.dealer, hand=dealer_hand, hole_card_revealed=True)

        round_record.phase = RoundPhase.SETTLEMENT
        for participant in round_record.participants:
            player_stats = self.players[participant.player_id].stats
            player_stats.rounds_played += 1
            insurance_net_change = _insurance_net_change(participant, dealer_hand)
            net_change = insurance_net_change
            for hand_index, hand in enumerate(participant.hands):
                resolved_hand = self._resolve_hand(hand, dealer_hand, round_record.rules)
                participant.hands[hand_index] = resolved_hand
                net_change += resolved_hand.resolution.net_change if resolved_hand.resolution is not None else 0

                player_stats.hands_played += 1
                if resolved_hand.value.is_blackjack:
                    player_stats.blackjack_count += 1
                if resolved_hand.value.is_bust:
                    player_stats.bust_count += 1
                if resolved_hand.resolution is not None:
                    player_stats.bankroll_delta += resolved_hand.resolution.net_change
                    if resolved_hand.resolution.outcome is HandOutcome.WIN:
                        player_stats.wins += 1
                    elif resolved_hand.resolution.outcome is HandOutcome.LOSS:
                        player_stats.losses += 1
                    else:
                        player_stats.pushes += 1

            player_stats.bankroll_delta += insurance_net_change
            participant.bankroll_after_round = participant.bankroll_before_round + net_change
            seat = table.seats[participant.seat_number]
            seat.bankroll = participant.bankroll_after_round
            seat.active_hand_ids.clear()
            seat.ready_for_next_round = seat.bankroll >= table.rules.minimum_bet

        table.active_round_id = None
        table.status = TableSessionStatus.OPEN
        round_record.phase = RoundPhase.COMPLETE
        round_record.turn = None
        self._sync_table_seats(round_record, clear_active_hands=True)
        round_record.shoe = deque(table.shoe)
        round_record.shuffle_count = table.shuffle_count
        round_record.log_event(
            "round_settled",
            {
                "round": round_record.to_public_dict(),
            },
        )

    def _dealer_should_play(self, round_record: RoundRecord) -> bool:
        for participant in round_record.participants:
            for hand in participant.hands:
                if hand.resolution is None and hand.status in {HandStatus.ACTIVE, HandStatus.STANDING}:
                    return True
        return False

    def _resolve_hand(self, hand: HandState, dealer_hand: HandState, rules: RuleConfig) -> HandState:
        if hand.wager is None:
            raise ConflictError("Round state is invalid because a hand is missing its wager.")
        if hand.resolution is not None:
            return hand

        player_total = _hand_display_total(hand)
        dealer_total = _hand_display_total(dealer_hand)

        if hand.value.is_blackjack and dealer_hand.value.is_blackjack:
            resolution = HandResolution(
                outcome=HandOutcome.PUSH,
                reason=ResolutionReason.EQUAL_TOTAL,
                net_change=0,
                player_total=player_total,
                dealer_total=dealer_total,
            )
            return replace(hand, status=HandStatus.BLACKJACK, resolution=resolution)

        if hand.value.is_blackjack:
            resolution = HandResolution(
                outcome=HandOutcome.WIN,
                reason=ResolutionReason.BLACKJACK,
                net_change=_payout_amount(hand.wager.amount, rules),
                player_total=player_total,
                dealer_total=dealer_total,
            )
            return replace(hand, status=HandStatus.BLACKJACK, resolution=resolution)

        if hand.value.is_bust:
            resolution = HandResolution(
                outcome=HandOutcome.LOSS,
                reason=ResolutionReason.PLAYER_BUST,
                net_change=-hand.wager.amount,
                player_total=player_total,
                dealer_total=dealer_total,
            )
            return replace(hand, status=HandStatus.BUSTED, resolution=resolution)

        if dealer_hand.value.is_blackjack:
            resolution = HandResolution(
                outcome=HandOutcome.LOSS,
                reason=ResolutionReason.BLACKJACK,
                net_change=-hand.wager.amount,
                player_total=player_total,
                dealer_total=dealer_total,
            )
            return replace(hand, status=HandStatus.COMPLETE, resolution=resolution)

        if dealer_hand.value.is_bust:
            resolution = HandResolution(
                outcome=HandOutcome.WIN,
                reason=ResolutionReason.DEALER_BUST,
                net_change=hand.wager.amount,
                player_total=player_total,
                dealer_total=dealer_total,
            )
            return replace(hand, status=HandStatus.COMPLETE, resolution=resolution)

        player_best_total = hand.value.best_total
        dealer_best_total = dealer_hand.value.best_total
        if player_best_total is None or dealer_best_total is None:
            raise ConflictError("Round state is invalid because settlement totals are unavailable.")

        if player_best_total > dealer_best_total:
            outcome = HandOutcome.WIN
            reason = ResolutionReason.HIGHER_TOTAL
            net_change = hand.wager.amount
        elif player_best_total < dealer_best_total:
            outcome = HandOutcome.LOSS
            reason = ResolutionReason.LOWER_TOTAL
            net_change = -hand.wager.amount
        else:
            outcome = HandOutcome.PUSH
            reason = ResolutionReason.EQUAL_TOTAL
            net_change = 0

        return replace(
            hand,
            status=HandStatus.COMPLETE,
            resolution=HandResolution(
                outcome=outcome,
                reason=reason,
                net_change=net_change,
                player_total=player_total,
                dealer_total=dealer_total,
            ),
        )

    def _sync_table_seats(self, round_record: RoundRecord, *, clear_active_hands: bool = False) -> None:
        table = self._table(round_record.table_id)
        for participant in round_record.participants:
            seat = table.seats[participant.seat_number]
            seat.ready_for_next_round = False if not clear_active_hands else seat.ready_for_next_round
            seat.active_hand_ids = [] if clear_active_hands else [hand.hand_id for hand in participant.hands]

