from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence


class ParticipantType(str, Enum):
    HUMAN = "human"
    AI = "ai"


class TableSessionStatus(str, Enum):
    OPEN = "open"
    ACTIVE = "active"
    CLOSED = "closed"


class SeatStatus(str, Enum):
    EMPTY = "empty"
    SEATED = "seated"
    SITTING_OUT = "sitting_out"


class RoundPhase(str, Enum):
    WAITING_FOR_BETS = "waiting_for_bets"
    DEALING = "dealing"
    PLAYER_TURNS = "player_turns"
    DEALER_TURN = "dealer_turn"
    SETTLEMENT = "settlement"
    COMPLETE = "complete"


class HandStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    STANDING = "standing"
    BUSTED = "busted"
    BLACKJACK = "blackjack"
    COMPLETE = "complete"


class ActionType(str, Enum):
    HIT = "hit"
    STAND = "stand"
    DOUBLE = "double"
    SPLIT = "split"
    SURRENDER = "surrender"
    INSURANCE = "insurance"


class TurnActor(str, Enum):
    PLAYER = "player"
    DEALER = "dealer"


class HandOutcome(str, Enum):
    WIN = "win"
    LOSS = "loss"
    PUSH = "push"


class ResolutionReason(str, Enum):
    BLACKJACK = "blackjack"
    PLAYER_BUST = "player_bust"
    DEALER_BUST = "dealer_bust"
    HIGHER_TOTAL = "higher_total"
    LOWER_TOTAL = "lower_total"
    EQUAL_TOTAL = "equal_total"
    SURRENDER = "surrender"


class CardSuit(str, Enum):
    CLUBS = "clubs"
    DIAMONDS = "diamonds"
    HEARTS = "hearts"
    SPADES = "spades"


class CardRank(str, Enum):
    ACE = "A"
    TWO = "2"
    THREE = "3"
    FOUR = "4"
    FIVE = "5"
    SIX = "6"
    SEVEN = "7"
    EIGHT = "8"
    NINE = "9"
    TEN = "10"
    JACK = "J"
    QUEEN = "Q"
    KING = "K"


_RANK_VALUES: dict[CardRank, int] = {
    CardRank.ACE: 1,
    CardRank.TWO: 2,
    CardRank.THREE: 3,
    CardRank.FOUR: 4,
    CardRank.FIVE: 5,
    CardRank.SIX: 6,
    CardRank.SEVEN: 7,
    CardRank.EIGHT: 8,
    CardRank.NINE: 9,
    CardRank.TEN: 10,
    CardRank.JACK: 10,
    CardRank.QUEEN: 10,
    CardRank.KING: 10,
}

_ACTION_ORDER: dict[ActionType, int] = {
    ActionType.HIT: 0,
    ActionType.STAND: 1,
    ActionType.DOUBLE: 2,
    ActionType.SPLIT: 3,
    ActionType.SURRENDER: 4,
    ActionType.INSURANCE: 5,
}


def _ordered_actions(actions: set[ActionType]) -> tuple[ActionType, ...]:
    return tuple(sorted(actions, key=_ACTION_ORDER.__getitem__))


@dataclass(frozen=True, slots=True)
class PayoutRatio:
    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        if self.numerator <= 0:
            raise ValueError("Payout numerator must be positive.")
        if self.denominator <= 0:
            raise ValueError("Payout denominator must be positive.")

    def to_dict(self) -> dict[str, int]:
        return {
            "numerator": self.numerator,
            "denominator": self.denominator,
        }


@dataclass(frozen=True, slots=True)
class RuleConfig:
    deck_count: int = 6
    dealer_stands_on_soft_17: bool = True
    blackjack_payout: PayoutRatio = field(default_factory=lambda: PayoutRatio(3, 2))
    minimum_bet: int = 10
    maximum_bet: int = 500
    allow_double_after_split: bool = True
    maximum_split_depth: int = 3
    split_on_value_match: bool = False

    def __post_init__(self) -> None:
        if self.deck_count <= 0:
            raise ValueError("Deck count must be positive.")
        if self.minimum_bet <= 0:
            raise ValueError("Minimum bet must be positive.")
        if self.maximum_bet < self.minimum_bet:
            raise ValueError("Maximum bet must be greater than or equal to minimum bet.")
        if self.maximum_split_depth < 0:
            raise ValueError("Maximum split depth cannot be negative.")

    def validate_bet(self, amount: int, available_bankroll: int) -> None:
        if amount < self.minimum_bet:
            raise ValueError("Bet is below the minimum table limit.")
        if amount > self.maximum_bet:
            raise ValueError("Bet is above the maximum table limit.")
        if amount > available_bankroll:
            raise ValueError("Bet exceeds the player's available bankroll.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "deck_count": self.deck_count,
            "dealer_stands_on_soft_17": self.dealer_stands_on_soft_17,
            "blackjack_payout": self.blackjack_payout.to_dict(),
            "minimum_bet": self.minimum_bet,
            "maximum_bet": self.maximum_bet,
            "allow_double_after_split": self.allow_double_after_split,
            "maximum_split_depth": self.maximum_split_depth,
            "split_on_value_match": self.split_on_value_match,
            "allowed_player_actions": [action.value for action in _ordered_actions(set(ActionType))],
        }


@dataclass(frozen=True, slots=True)
class Card:
    rank: CardRank
    suit: CardSuit

    @property
    def hard_value(self) -> int:
        return _RANK_VALUES[self.rank]

    @property
    def split_value(self) -> int:
        return 10 if self.rank in {CardRank.TEN, CardRank.JACK, CardRank.QUEEN, CardRank.KING} else self.hard_value

    def to_dict(self) -> dict[str, str]:
        return {
            "rank": self.rank.value,
            "suit": self.suit.value,
        }


@dataclass(frozen=True, slots=True)
class HandValue:
    hard_total: int
    totals: tuple[int, ...]
    best_total: int | None
    is_soft: bool
    is_blackjack: bool
    is_bust: bool

    @classmethod
    def from_cards(
        cls,
        cards: Sequence[Card],
        *,
        counts_as_blackjack: bool = True,
    ) -> HandValue:
        hard_total = sum(card.hard_value for card in cards)
        ace_count = sum(1 for card in cards if card.rank is CardRank.ACE)
        candidate_totals = {hard_total + (10 * ace_adjustments) for ace_adjustments in range(ace_count + 1)}
        non_bust_totals = sorted(total for total in candidate_totals if total <= 21)
        totals = tuple(non_bust_totals) if non_bust_totals else (hard_total,)
        best_total = non_bust_totals[-1] if non_bust_totals else None
        is_soft = any(total != hard_total for total in candidate_totals if total <= 21)
        is_blackjack = counts_as_blackjack and len(cards) == 2 and best_total == 21
        is_bust = best_total is None and bool(cards)
        return cls(
            hard_total=hard_total,
            totals=totals,
            best_total=best_total,
            is_soft=is_soft,
            is_blackjack=is_blackjack,
            is_bust=is_bust,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "hard_total": self.hard_total,
            "totals": list(self.totals),
            "best_total": self.best_total,
            "is_soft": self.is_soft,
            "is_blackjack": self.is_blackjack,
            "is_bust": self.is_bust,
        }


@dataclass(frozen=True, slots=True)
class Bet:
    amount: int
    currency: str = "USD"

    def __post_init__(self) -> None:
        if self.amount <= 0:
            raise ValueError("Bet amount must be positive.")

    def validate(self, rules: RuleConfig, available_bankroll: int) -> None:
        rules.validate_bet(self.amount, available_bankroll)

    def to_dict(self) -> dict[str, Any]:
        return {
            "amount": self.amount,
            "currency": self.currency,
        }


@dataclass(frozen=True, slots=True)
class PlayerProfile:
    player_id: str
    display_name: str
    participant_type: ParticipantType
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "player_id": self.player_id,
            "display_name": self.display_name,
            "participant_type": self.participant_type.value,
        }

    def to_internal_dict(self) -> dict[str, Any]:
        payload = self.to_public_dict()
        payload["metadata"] = dict(self.metadata)
        return payload


@dataclass(frozen=True, slots=True)
class HandResolution:
    outcome: HandOutcome
    reason: ResolutionReason
    net_change: int
    player_total: int | None = None
    dealer_total: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "reason": self.reason.value,
            "net_change": self.net_change,
            "player_total": self.player_total,
            "dealer_total": self.dealer_total,
        }


@dataclass(frozen=True, slots=True)
class HandState:
    hand_id: str
    player_id: str
    seat_number: int
    cards: tuple[Card, ...] = ()
    wager: Bet | None = None
    insurance_wager: Bet | None = None
    status: HandStatus = HandStatus.PENDING
    split_depth: int = 0
    parent_hand_id: str | None = None
    doubled_down: bool = False
    resolution: HandResolution | None = None

    @property
    def is_from_split(self) -> bool:
        return self.parent_hand_id is not None

    @property
    def value(self) -> HandValue:
        return HandValue.from_cards(self.cards, counts_as_blackjack=not self.is_from_split)

    def can_split(self, rules: RuleConfig) -> bool:
        if len(self.cards) != 2:
            return False
        if self.split_depth >= rules.maximum_split_depth:
            return False
        first, second = self.cards
        if rules.split_on_value_match:
            return first.split_value == second.split_value
        return first.rank == second.rank

    def legal_actions(
        self,
        rules: RuleConfig,
        available_bankroll: int,
        *,
        dealer_up_card: Card | None = None,
    ) -> tuple[ActionType, ...]:
        if self.status is not HandStatus.ACTIVE:
            return ()
        if self.doubled_down or self.resolution is not None:
            return ()
        if self.wager is None or len(self.cards) < 2:
            return ()

        hand_value = self.value
        if hand_value.is_bust or hand_value.is_blackjack:
            return ()

        if hand_value.best_total == 21:
            return (ActionType.STAND,)

        actions: set[ActionType] = {ActionType.HIT, ActionType.STAND}
        opening_decision = len(self.cards) == 2 and self.wager is not None
        if opening_decision and not self.is_from_split:
            actions.add(ActionType.SURRENDER)
            insurance_amount = self.wager.amount // 2
            if (
                self.insurance_wager is None
                and dealer_up_card is not None
                and dealer_up_card.rank is CardRank.ACE
                and insurance_amount > 0
                and available_bankroll >= insurance_amount
            ):
                actions.add(ActionType.INSURANCE)
        if opening_decision and self.wager is not None and available_bankroll >= self.wager.amount:
            if not self.is_from_split or rules.allow_double_after_split:
                actions.add(ActionType.DOUBLE)
            if self.can_split(rules):
                actions.add(ActionType.SPLIT)
        return _ordered_actions(actions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hand_id": self.hand_id,
            "player_id": self.player_id,
            "seat_number": self.seat_number,
            "cards": [card.to_dict() for card in self.cards],
            "value": self.value.to_dict(),
            "wager": self.wager.to_dict() if self.wager is not None else None,
            "status": self.status.value,
            "split_depth": self.split_depth,
            "parent_hand_id": self.parent_hand_id,
            "doubled_down": self.doubled_down,
            "resolution": self.resolution.to_dict() if self.resolution is not None else None,
        }


@dataclass(frozen=True, slots=True)
class DealerState:
    hand: HandState
    hole_card_index: int | None = 1
    hole_card_revealed: bool = False

    def visible_cards(self, *, reveal_hidden: bool = False) -> tuple[Card, ...]:
        if reveal_hidden or self.hole_card_revealed or self.hole_card_index is None:
            return self.hand.cards
        return tuple(
            card
            for index, card in enumerate(self.hand.cards)
            if index != self.hole_card_index
        )

    def visible_value(self, *, reveal_hidden: bool = False) -> HandValue:
        cards = self.visible_cards(reveal_hidden=reveal_hidden)
        return HandValue.from_cards(cards, counts_as_blackjack=False)

    def should_hit(self, rules: RuleConfig) -> bool:
        value = self.hand.value
        if value.is_bust or value.best_total is None:
            return False
        if value.best_total < 17:
            return True
        if value.best_total > 17:
            return False
        return value.is_soft and not rules.dealer_stands_on_soft_17

    def to_internal_dict(self) -> dict[str, Any]:
        return {
            "hand": self.hand.to_dict(),
            "hole_card_index": self.hole_card_index,
            "hole_card_revealed": self.hole_card_revealed,
        }

    def to_public_dict(self, *, reveal_hidden: bool = False) -> dict[str, Any]:
        reveal = reveal_hidden or self.hole_card_revealed or self.hole_card_index is None
        cards: list[dict[str, Any]] = []
        for index, card in enumerate(self.hand.cards):
            if reveal or index != self.hole_card_index:
                cards.append(card.to_dict())
            else:
                cards.append({"is_hidden": True})

        return {
            "hand_id": self.hand.hand_id,
            "cards": cards,
            "value": self.visible_value(reveal_hidden=reveal).to_dict(),
            "status": (
                self.hand.status.value
                if reveal
                else (HandStatus.ACTIVE.value if self.hand.cards else HandStatus.PENDING.value)
            ),
            "hole_card_revealed": reveal,
        }


@dataclass(frozen=True, slots=True)
class SeatState:
    seat_number: int
    status: SeatStatus = SeatStatus.EMPTY
    occupant: PlayerProfile | None = None
    bankroll: int = 0
    ready_for_next_round: bool = False
    active_hand_ids: tuple[str, ...] = ()

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "seat_number": self.seat_number,
            "status": self.status.value,
            "occupant": self.occupant.to_public_dict() if self.occupant is not None else None,
            "bankroll": self.bankroll,
            "ready_for_next_round": self.ready_for_next_round,
            "active_hand_ids": list(self.active_hand_ids),
        }

    def to_internal_dict(self) -> dict[str, Any]:
        payload = self.to_public_dict()
        payload["occupant"] = self.occupant.to_internal_dict() if self.occupant is not None else None
        return payload


@dataclass(frozen=True, slots=True)
class ShoeState:
    cards_remaining: int = 0
    decks_remaining: float = 0.0
    shuffle_count: int = 0

    @classmethod
    def from_cards_remaining(cls, cards_remaining: int, *, shuffle_count: int) -> ShoeState:
        return cls(
            cards_remaining=cards_remaining,
            decks_remaining=round(cards_remaining / 52, 4),
            shuffle_count=shuffle_count,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "cards_remaining": self.cards_remaining,
            "decks_remaining": self.decks_remaining,
            "shuffle_count": self.shuffle_count,
        }


@dataclass(frozen=True, slots=True)
class RoundParticipantState:
    player_id: str
    display_name: str
    participant_type: ParticipantType
    seat_number: int
    bankroll_before_round: int
    hands: tuple[HandState, ...] = ()
    bankroll_after_round: int | None = None

    @property
    def total_committed(self) -> int:
        return sum(
            hand.wager.amount + (hand.insurance_wager.amount if hand.insurance_wager is not None else 0)
            for hand in self.hands
            if hand.wager is not None
        )

    @property
    def available_bankroll(self) -> int:
        return max(self.bankroll_before_round - self.total_committed, 0)

    def hand_by_id(self, hand_id: str) -> HandState | None:
        for hand in self.hands:
            if hand.hand_id == hand_id:
                return hand
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "player_id": self.player_id,
            "display_name": self.display_name,
            "participant_type": self.participant_type.value,
            "seat_number": self.seat_number,
            "bankroll_before_round": self.bankroll_before_round,
            "bankroll_after_round": self.bankroll_after_round,
            "total_committed": self.total_committed,
            "available_bankroll": self.available_bankroll,
            "hands": [hand.to_dict() for hand in self.hands],
        }


@dataclass(frozen=True, slots=True)
class TurnState:
    actor: TurnActor
    legal_actions: tuple[ActionType, ...] = ()
    seat_number: int | None = None
    player_id: str | None = None
    hand_id: str | None = None
    reason: str | None = None

    @classmethod
    def for_player_hand(
        cls,
        participant: RoundParticipantState,
        hand: HandState,
        rules: RuleConfig,
        *,
        dealer_up_card: Card | None = None,
    ) -> TurnState:
        return cls(
            actor=TurnActor.PLAYER,
            legal_actions=hand.legal_actions(
                rules,
                participant.available_bankroll,
                dealer_up_card=dealer_up_card,
            ),
            seat_number=participant.seat_number,
            player_id=participant.player_id,
            hand_id=hand.hand_id,
        )

    @classmethod
    def for_dealer(cls, *, reason: str | None = None) -> TurnState:
        return cls(actor=TurnActor.DEALER, reason=reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor": self.actor.value,
            "legal_actions": [action.value for action in self.legal_actions],
            "seat_number": self.seat_number,
            "player_id": self.player_id,
            "hand_id": self.hand_id,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class RoundState:
    round_id: str
    table_id: str
    phase: RoundPhase
    rules: RuleConfig
    participants: tuple[RoundParticipantState, ...]
    dealer: DealerState
    shoe_state: ShoeState = field(default_factory=ShoeState)
    turn: TurnState | None = None
    action_count: int = 0
    version: int = 0

    def participant_by_id(self, player_id: str) -> RoundParticipantState | None:
        for participant in self.participants:
            if participant.player_id == player_id:
                return participant
        return None

    def to_internal_dict(self) -> dict[str, Any]:
        return {
            "round_id": self.round_id,
            "table_id": self.table_id,
            "phase": self.phase.value,
            "rules": self.rules.to_dict(),
            "participants": [participant.to_dict() for participant in self.participants],
            "dealer": self.dealer.to_internal_dict(),
            "shoe_state": self.shoe_state.to_dict(),
            "turn": self.turn.to_dict() if self.turn is not None else None,
            "action_count": self.action_count,
            "version": self.version,
        }

    def to_public_dict(self) -> dict[str, Any]:
        reveal_dealer = self.phase in {RoundPhase.SETTLEMENT, RoundPhase.COMPLETE}
        return {
            "round_id": self.round_id,
            "table_id": self.table_id,
            "phase": self.phase.value,
            "rules": self.rules.to_dict(),
            "participants": [participant.to_dict() for participant in self.participants],
            "dealer": self.dealer.to_public_dict(reveal_hidden=reveal_dealer),
            "shoe_state": self.shoe_state.to_dict(),
            "turn": self.turn.to_dict() if self.turn is not None else None,
            "action_count": self.action_count,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class TableSessionState:
    table_id: str
    status: TableSessionStatus
    rules: RuleConfig
    seats: tuple[SeatState, ...]
    shoe_state: ShoeState = field(default_factory=ShoeState)
    active_round_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def occupied_seats(self) -> tuple[SeatState, ...]:
        return tuple(seat for seat in self.seats if seat.occupant is not None)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "table_id": self.table_id,
            "status": self.status.value,
            "rules": self.rules.to_dict(),
            "seats": [seat.to_public_dict() for seat in self.seats],
            "shoe_state": self.shoe_state.to_dict(),
            "active_round_id": self.active_round_id,
        }

    def to_internal_dict(self) -> dict[str, Any]:
        payload = self.to_public_dict()
        payload["seats"] = [seat.to_internal_dict() for seat in self.seats]
        payload["metadata"] = dict(self.metadata)
        return payload

