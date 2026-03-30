from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from typing import Any
from typing import Mapping
from typing import Protocol
from typing import Sequence


ActionName = str
_LOW_VALUE_RANKS = frozenset({"2", "3", "4", "5", "6"})
_TEN_VALUE_RANKS = frozenset({"10", "J", "Q", "K"})


@dataclass(frozen=True, slots=True)
class BetContext:
    round_index: int
    player_id: str
    participant: Mapping[str, Any]
    round_state: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ActionContext:
    round_index: int
    player_id: str
    participant: Mapping[str, Any]
    hand: Mapping[str, Any]
    round_state: Mapping[str, Any]
    legal_actions: tuple[ActionName, ...]


class BenchmarkStrategy(Protocol):
    name: str
    description: str

    def choose_bet(self, context: BetContext) -> int: ...

    def choose_action(self, context: ActionContext) -> ActionName: ...


def _dealer_upcard_value(round_state: Mapping[str, Any]) -> int | None:
    dealer = round_state.get("dealer", {})
    cards = dealer.get("cards", [])
    for card in cards:
        if not isinstance(card, Mapping) or card.get("is_hidden"):
            continue
        rank = str(card.get("rank"))
        if rank == "A":
            return 11
        if rank in {"10", "J", "Q", "K"}:
            return 10
        return int(rank)
    return None


def _hand_total(hand: Mapping[str, Any]) -> int:
    value = hand.get("value", {})
    best_total = value.get("best_total")
    return int(best_total if best_total is not None else value.get("hard_total", 0))


def _pair_rank(hand: Mapping[str, Any]) -> str | None:
    cards = hand.get("cards", [])
    if not isinstance(cards, Sequence) or len(cards) != 2:
        return None
    first, second = cards
    if not isinstance(first, Mapping) or not isinstance(second, Mapping):
        return None
    first_rank = first.get("rank")
    second_rank = second.get("rank")
    if first_rank is None or first_rank != second_rank:
        return None
    return str(first_rank)


def _iter_visible_cards(round_state: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    visible_cards: list[Mapping[str, Any]] = []
    dealer = round_state.get("dealer", {})

    for card in dealer.get("cards", []):
        if isinstance(card, Mapping) and not card.get("is_hidden") and card.get("rank") is not None:
            visible_cards.append(card)

    for participant in round_state.get("participants", []):
        if not isinstance(participant, Mapping):
            continue
        for hand in participant.get("hands", []):
            if not isinstance(hand, Mapping):
                continue
            for card in hand.get("cards", []):
                if isinstance(card, Mapping) and not card.get("is_hidden") and card.get("rank") is not None:
                    visible_cards.append(card)

    return tuple(visible_cards)


def _visible_card_ranks(round_state: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(card["rank"]) for card in _iter_visible_cards(round_state))


def _hi_lo_rank_value(rank: str) -> int:
    if rank in _LOW_VALUE_RANKS:
        return 1
    if rank == "A" or rank in _TEN_VALUE_RANKS:
        return -1
    return 0


def _bet_amount(
    *,
    rules: Mapping[str, Any],
    participant: Mapping[str, Any],
    bet_units: int,
    bankroll_divisor: int,
    flat_bet: bool,
) -> int:
    minimum_bet = int(rules.get("minimum_bet", 10))
    maximum_bet = int(rules.get("maximum_bet", minimum_bet))
    available_bankroll = int(participant.get("available_bankroll", minimum_bet))

    if flat_bet:
        target_bet = minimum_bet * bet_units
    else:
        target_bet = max(minimum_bet * bet_units, available_bankroll // bankroll_divisor)

    return max(minimum_bet, min(maximum_bet, available_bankroll, target_bet))


_ALL_UPCARDS = frozenset(range(2, 12))
_PAIR_SPLIT_UPCARDS: dict[str, frozenset[int]] = {
    "A": _ALL_UPCARDS,
    "2": frozenset({2, 3, 4, 5, 6, 7}),
    "3": frozenset({2, 3, 4, 5, 6, 7}),
    "4": frozenset({5, 6}),
    "6": frozenset({2, 3, 4, 5, 6}),
    "7": frozenset({2, 3, 4, 5, 6, 7}),
    "8": _ALL_UPCARDS,
    "9": frozenset({2, 3, 4, 5, 6, 8, 9}),
}
_HARD_SURRENDER_UPCARDS: dict[int, frozenset[int]] = {
    15: frozenset({10}),
    16: frozenset({9, 10, 11}),
}
_HARD_DOUBLE_UPCARDS: dict[int, frozenset[int]] = {
    9: frozenset({3, 4, 5, 6}),
    10: frozenset({2, 3, 4, 5, 6, 7, 8, 9}),
    11: _ALL_UPCARDS,
}
_SOFT_DOUBLE_UPCARDS: dict[int, frozenset[int]] = {
    13: frozenset({5, 6}),
    14: frozenset({5, 6}),
    15: frozenset({4, 5, 6}),
    16: frozenset({4, 5, 6}),
    17: frozenset({3, 4, 5, 6}),
    18: frozenset({3, 4, 5, 6}),
}


def _fallback_action(*, hand_total: int, is_soft: bool, legal_actions: Sequence[ActionName]) -> ActionName:
    legal_action_set = set(legal_actions)
    hit_threshold = 18 if is_soft else 17
    if hand_total < hit_threshold and "hit" in legal_action_set:
        return "hit"
    if "stand" in legal_action_set:
        return "stand"
    return legal_actions[0]


def _should_split_pair(pair_rank: str | None, dealer_upcard: int | None) -> bool:
    if pair_rank is None:
        return False
    if dealer_upcard is None:
        return pair_rank in {"A", "8"}
    return dealer_upcard in _PAIR_SPLIT_UPCARDS.get(pair_rank, frozenset())


def _should_surrender(*, hand_total: int, dealer_upcard: int | None, is_soft: bool) -> bool:
    if is_soft or dealer_upcard is None:
        return False
    return dealer_upcard in _HARD_SURRENDER_UPCARDS.get(hand_total, frozenset())


def _should_double(*, hand_total: int, dealer_upcard: int | None, is_soft: bool) -> bool:
    if dealer_upcard is None:
        return hand_total == 11 and not is_soft
    table = _SOFT_DOUBLE_UPCARDS if is_soft else _HARD_DOUBLE_UPCARDS
    return dealer_upcard in table.get(hand_total, frozenset())


def _should_take_insurance(round_state: Mapping[str, Any]) -> bool:
    if _dealer_upcard_value(round_state) != 11:
        return False

    deck_count = int(round_state.get("rules", {}).get("deck_count", 6))
    visible_ranks = _visible_card_ranks(round_state)
    unseen_cards = (deck_count * 52) - len(visible_ranks)
    if unseen_cards <= 0:
        return False

    visible_tens = sum(1 for rank in visible_ranks if rank in _TEN_VALUE_RANKS)
    remaining_tens = (deck_count * 16) - visible_tens
    if remaining_tens <= 0:
        return False

    return (remaining_tens * 3) > unseen_cards


def _basic_post_double_action(*, hand_total: int, dealer_upcard: int | None, is_soft: bool) -> ActionName:
    if dealer_upcard is None:
        return _fallback_action(hand_total=hand_total, is_soft=is_soft, legal_actions=("hit", "stand"))

    if is_soft:
        if hand_total <= 17:
            return "hit"
        if hand_total == 18 and dealer_upcard in {9, 10, 11}:
            return "hit"
        return "stand"

    if hand_total >= 17:
        return "stand"
    if 13 <= hand_total <= 16:
        return "stand" if dealer_upcard in {2, 3, 4, 5, 6} else "hit"
    if hand_total == 12:
        return "stand" if dealer_upcard in {4, 5, 6} else "hit"
    return "hit"


def _true_count(*, running_count: int, observed_card_count: int, deck_count: int) -> float:
    remaining_cards = max((max(deck_count, 1) * 52) - observed_card_count, 13)
    return running_count / (remaining_cards / 52)


def _running_count(round_state: Mapping[str, Any]) -> int:
    return sum(_hi_lo_rank_value(rank) for rank in _visible_card_ranks(round_state))


def _observed_card_count(round_state: Mapping[str, Any]) -> int:
    deck_count = max(int(round_state.get("rules", {}).get("deck_count", 6)), 1)
    total_cards = deck_count * 52
    shoe_state = round_state.get("shoe_state", {})
    decks_remaining = shoe_state.get("decks_remaining")
    try:
        remaining_cards = int(round(float(decks_remaining) * 52))
    except (TypeError, ValueError):
        cards_remaining = shoe_state.get("cards_remaining")
        try:
            remaining_cards = int(cards_remaining)
        except (TypeError, ValueError):
            remaining_cards = total_cards - len(_visible_card_ranks(round_state))
    remaining_cards = max(0, min(total_cards, remaining_cards))
    return total_cards - remaining_cards


def _counting_bet_units(true_count: float) -> int:
    return max(1, min(8, int(true_count)))


def _public_true_count(*, round_state: Mapping[str, Any], running_count: int) -> float:
    return _true_count(
        running_count=running_count,
        observed_card_count=_observed_card_count(round_state),
        deck_count=int(round_state.get("rules", {}).get("deck_count", 6)),
    )


def _counting_deviation_action(
    *,
    hand_total: int,
    dealer_upcard: int | None,
    is_soft: bool,
    true_count: float,
    legal_actions: Sequence[ActionName],
) -> ActionName | None:
    legal_action_set = set(legal_actions)
    if is_soft or "stand" not in legal_action_set or "hit" not in legal_action_set:
        return None

    if dealer_upcard == 10:
        if hand_total == 16 and true_count >= 0:
            return "stand"
        if hand_total == 15 and true_count >= 4:
            return "stand"

    return None


def _basic_strategy_action(
    *,
    hand: Mapping[str, Any],
    round_state: Mapping[str, Any],
    legal_actions: Sequence[ActionName],
    true_count: float | None = None,
) -> ActionName:
    legal_action_set = set(legal_actions)
    hand_total = _hand_total(hand)
    is_soft = bool(hand.get("value", {}).get("is_soft"))
    dealer_upcard = _dealer_upcard_value(round_state)
    pair_rank = _pair_rank(hand)

    if "insurance" in legal_action_set and _should_take_insurance(round_state):
        return "insurance"

    if "split" in legal_action_set and _should_split_pair(pair_rank, dealer_upcard):
        return "split"

    if "surrender" in legal_action_set and _should_surrender(
        hand_total=hand_total,
        dealer_upcard=dealer_upcard,
        is_soft=is_soft,
    ):
        return "surrender"

    if "double" in legal_action_set and _should_double(
        hand_total=hand_total,
        dealer_upcard=dealer_upcard,
        is_soft=is_soft,
    ):
        return "double"

    if true_count is not None:
        deviation = _counting_deviation_action(
            hand_total=hand_total,
            dealer_upcard=dealer_upcard,
            is_soft=is_soft,
            true_count=true_count,
            legal_actions=legal_actions,
        )
        if deviation is not None:
            return deviation

    action = _basic_post_double_action(
        hand_total=hand_total,
        dealer_upcard=dealer_upcard,
        is_soft=is_soft,
    )
    if action in legal_action_set:
        return action
    if "stand" in legal_action_set:
        return "stand"
    return legal_actions[0]


@dataclass(frozen=True, slots=True)
class ThresholdStrategy:
    name: str
    description: str
    hard_hit_below: int
    soft_hit_below: int
    double_totals: frozenset[int]
    split_ranks: frozenset[str]
    bet_units: int = 1
    bankroll_divisor: int = 12
    flat_bet: bool = False
    double_against_upcard_at_most: int | None = None

    def choose_bet(self, context: BetContext) -> int:
        return _bet_amount(
            rules=context.round_state.get("rules", {}),
            participant=context.participant,
            bet_units=self.bet_units,
            bankroll_divisor=self.bankroll_divisor,
            flat_bet=self.flat_bet,
        )

    def choose_action(self, context: ActionContext) -> ActionName:
        legal_actions = set(context.legal_actions)
        hand_total = _hand_total(context.hand)
        is_soft = bool(context.hand.get("value", {}).get("is_soft"))
        dealer_upcard = _dealer_upcard_value(context.round_state)

        if "insurance" in legal_actions and _should_take_insurance(context.round_state):
            return "insurance"

        if "split" in legal_actions and _pair_rank(context.hand) in self.split_ranks:
            return "split"

        should_double = hand_total in self.double_totals and "double" in legal_actions
        if should_double and (
            self.double_against_upcard_at_most is None
            or dealer_upcard is None
            or dealer_upcard <= self.double_against_upcard_at_most
        ):
            return "double"

        hit_threshold = self.soft_hit_below if is_soft else self.hard_hit_below
        if hand_total < hit_threshold and "hit" in legal_actions:
            return "hit"
        if "stand" in legal_actions:
            return "stand"
        return context.legal_actions[0]


@dataclass(frozen=True, slots=True)
class BasicStrategy:
    name: str
    description: str
    bet_units: int = 2
    bankroll_divisor: int = 12
    flat_bet: bool = False

    def choose_bet(self, context: BetContext) -> int:
        return _bet_amount(
            rules=context.round_state.get("rules", {}),
            participant=context.participant,
            bet_units=self.bet_units,
            bankroll_divisor=self.bankroll_divisor,
            flat_bet=self.flat_bet,
        )

    def choose_action(self, context: ActionContext) -> ActionName:
        return _basic_strategy_action(
            hand=context.hand,
            round_state=context.round_state,
            legal_actions=context.legal_actions,
        )


@dataclass(slots=True)
class CountingStrategy:
    name: str
    description: str
    _running_count_total: int = field(default=0, init=False, repr=False)
    _last_shuffle_count: int | None = field(default=None, init=False, repr=False)
    _last_round_marker: str | None = field(default=None, init=False, repr=False)
    _last_visible_rank_counts: Counter[str] = field(default_factory=Counter, init=False, repr=False)

    def _sync_public_count(self, *, round_state: Mapping[str, Any], round_marker: str) -> None:
        shoe_state = round_state.get("shoe_state", {})
        try:
            shuffle_count = int(shoe_state.get("shuffle_count", 0))
        except (TypeError, ValueError):
            shuffle_count = 0

        if shuffle_count != self._last_shuffle_count:
            self._running_count_total = 0
            self._last_visible_rank_counts = Counter()
            self._last_shuffle_count = shuffle_count
            self._last_round_marker = None

        if round_marker != self._last_round_marker:
            self._last_visible_rank_counts = Counter()
            self._last_round_marker = round_marker

        current_visible_rank_counts = Counter(_visible_card_ranks(round_state))
        new_visible_rank_counts = current_visible_rank_counts - self._last_visible_rank_counts
        self._running_count_total += sum(
            _hi_lo_rank_value(rank) * count for rank, count in new_visible_rank_counts.items()
        )
        self._last_visible_rank_counts = current_visible_rank_counts

    def choose_bet(self, context: BetContext) -> int:
        self._sync_public_count(
            round_state=context.round_state,
            round_marker=str(context.round_state.get("round_id") or context.round_index),
        )
        true_count = _public_true_count(
            round_state=context.round_state,
            running_count=self._running_count_total,
        )
        return _bet_amount(
            rules=context.round_state.get("rules", {}),
            participant=context.participant,
            bet_units=_counting_bet_units(true_count),
            bankroll_divisor=12,
            flat_bet=False,
        )

    def choose_action(self, context: ActionContext) -> ActionName:
        self._sync_public_count(
            round_state=context.round_state,
            round_marker=str(context.round_state.get("round_id") or context.round_index),
        )
        true_count = _public_true_count(
            round_state=context.round_state,
            running_count=self._running_count_total,
        )
        return _basic_strategy_action(
            hand=context.hand,
            round_state=context.round_state,
            legal_actions=context.legal_actions,
            true_count=true_count,
        )


_BUILTIN_STRATEGIES: tuple[BenchmarkStrategy, ...] = (
    ThresholdStrategy(
        name="conservative",
        description="Flat minimum bets, stands earlier, and only doubles strong totals.",
        hard_hit_below=14,
        soft_hit_below=17,
        double_totals=frozenset({11}),
        split_ranks=frozenset({"A", "8"}),
        bet_units=1,
        flat_bet=True,
        double_against_upcard_at_most=9,
    ),
    ThresholdStrategy(
        name="balanced",
        description="Moderate bankroll pressure with simple hit/stand heuristics.",
        hard_hit_below=16,
        soft_hit_below=18,
        double_totals=frozenset({10, 11}),
        split_ranks=frozenset({"A", "8"}),
        bet_units=2,
        bankroll_divisor=12,
        double_against_upcard_at_most=9,
    ),
    BasicStrategy(
        name="basic",
        description="Moderate bets with blackjack basic-strategy style splits, doubles, and surrender.",
        bet_units=2,
        bankroll_divisor=12,
    ),
    CountingStrategy(
        name="counting",
        description="Uses public-card Hi-Lo deviations and carries a public running count into conservative bet spreads.",
    ),
    ThresholdStrategy(
        name="aggressive",
        description="Larger bets, deeper hits, and more frequent doubles and splits.",
        hard_hit_below=17,
        soft_hit_below=19,
        double_totals=frozenset({9, 10, 11}),
        split_ranks=frozenset({"A", "8", "9"}),
        bet_units=3,
        bankroll_divisor=6,
        double_against_upcard_at_most=10,
    ),
)

_BUILTIN_STRATEGY_INDEX = {strategy.name: strategy for strategy in _BUILTIN_STRATEGIES}


def _clone_strategy(strategy: BenchmarkStrategy) -> BenchmarkStrategy:
    return replace(strategy)  # type: ignore[arg-type]


def list_builtin_strategies() -> tuple[BenchmarkStrategy, ...]:
    return tuple(_clone_strategy(strategy) for strategy in _BUILTIN_STRATEGIES)


def resolve_strategy(name: str) -> BenchmarkStrategy:
    normalized_name = name.strip().lower()
    try:
        return _clone_strategy(_BUILTIN_STRATEGY_INDEX[normalized_name])
    except KeyError as exc:
        available = ", ".join(strategy.name for strategy in _BUILTIN_STRATEGIES)
        raise ValueError(f"Unknown strategy '{name}'. Available strategies: {available}.") from exc
