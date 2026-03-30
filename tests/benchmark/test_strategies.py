from __future__ import annotations

from typing import Any

import pytest

from blackjack_ai.benchmark.strategies import ActionContext
from blackjack_ai.benchmark.strategies import BetContext
from blackjack_ai.benchmark.strategies import list_builtin_strategies
from blackjack_ai.benchmark.strategies import resolve_strategy


def _action_context(
    *,
    dealer_rank: str,
    hand_ranks: tuple[str, str],
    best_total: int,
    legal_actions: tuple[str, ...],
    is_soft: bool = False,
    hard_total: int | None = None,
    deck_count: int = 6,
    other_player_hands: tuple[tuple[str, ...], ...] = (),
    round_index: int = 1,
    cards_remaining: int | None = None,
    decks_remaining: float | None = None,
    shuffle_count: int = 1,
) -> ActionContext:
    hand = {
        "hand_id": "hand-1",
        "cards": [{"rank": rank, "suit": "spades"} for rank in hand_ranks],
        "value": {
            "best_total": best_total,
            "hard_total": best_total if hard_total is None else hard_total,
            "is_soft": is_soft,
        },
    }
    participant = {
        "player_id": "player-1",
        "available_bankroll": 100,
        "hands": [hand],
    }
    participants = [participant]
    for index, ranks in enumerate(other_player_hands, start=2):
        participants.append(
            {
                "player_id": f"player-{index}",
                "available_bankroll": 100,
                "hands": [
                    {
                        "hand_id": f"hand-{index}",
                        "cards": [{"rank": rank, "suit": "clubs"} for rank in ranks],
                        "value": {
                            "best_total": 10,
                            "hard_total": 10,
                            "is_soft": False,
                        },
                    }
                ],
            }
        )

    return ActionContext(
        round_index=round_index,
        player_id="player-1",
        participant=participant,
        hand=hand,
        round_state={
            "rules": {"deck_count": deck_count, "minimum_bet": 10, "maximum_bet": 100},
            "shoe_state": {
                "cards_remaining": max(deck_count, 1) * 52 if cards_remaining is None else cards_remaining,
                "decks_remaining": round(
                    ((max(deck_count, 1) * 52) if cards_remaining is None else cards_remaining) / 52,
                    4,
                )
                if decks_remaining is None
                else decks_remaining,
                "shuffle_count": shuffle_count,
            },
            "participants": participants,
            "dealer": {
                "cards": [
                    {"rank": dealer_rank, "suit": "hearts", "is_hidden": False},
                    {"rank": "X", "suit": "clubs", "is_hidden": True},
                ]
            }
        },
        legal_actions=legal_actions,
    )


def _bet_context(
    *,
    round_index: int = 1,
    available_bankroll: int = 100,
    deck_count: int = 6,
    minimum_bet: int = 10,
    maximum_bet: int = 100,
    cards_remaining: int | None = None,
    decks_remaining: float | None = None,
    shuffle_count: int = 1,
) -> BetContext:
    participant = {
        "player_id": "player-1",
        "available_bankroll": available_bankroll,
        "hands": [],
    }
    return BetContext(
        round_index=round_index,
        player_id="player-1",
        participant=participant,
        round_state={
            "rules": {
                "deck_count": deck_count,
                "minimum_bet": minimum_bet,
                "maximum_bet": maximum_bet,
            },
            "shoe_state": {
                "cards_remaining": max(deck_count, 1) * 52 if cards_remaining is None else cards_remaining,
                "decks_remaining": round(
                    ((max(deck_count, 1) * 52) if cards_remaining is None else cards_remaining) / 52,
                    4,
                )
                if decks_remaining is None
                else decks_remaining,
                "shuffle_count": shuffle_count,
            },
            "participants": [participant],
            "dealer": {"cards": []},
        },
    )


def test_basic_and_counting_strategies_are_registered() -> None:
    assert "basic" in {strategy.name for strategy in list_builtin_strategies()}
    assert resolve_strategy("basic").name == "basic"
    assert "counting" in {strategy.name for strategy in list_builtin_strategies()}
    assert resolve_strategy("counting").name == "counting"


@pytest.mark.parametrize(
    ("context_kwargs", "expected_action"),
    [
        (
            {
                "dealer_rank": "10",
                "hand_ranks": ("10", "6"),
                "best_total": 16,
                "legal_actions": ("hit", "stand", "surrender"),
            },
            "surrender",
        ),
        (
            {
                "dealer_rank": "10",
                "hand_ranks": ("10", "6"),
                "best_total": 16,
                "legal_actions": ("hit", "stand"),
            },
            "hit",
        ),
        (
            {
                "dealer_rank": "4",
                "hand_ranks": ("10", "2"),
                "best_total": 12,
                "legal_actions": ("hit", "stand"),
            },
            "stand",
        ),
        (
            {
                "dealer_rank": "6",
                "hand_ranks": ("A", "7"),
                "best_total": 18,
                "hard_total": 8,
                "is_soft": True,
                "legal_actions": ("hit", "stand", "double"),
            },
            "double",
        ),
        (
            {
                "dealer_rank": "10",
                "hand_ranks": ("8", "8"),
                "best_total": 16,
                "legal_actions": ("hit", "stand", "split", "surrender"),
            },
            "split",
        ),
    ],
)
def test_basic_strategy_uses_public_state_for_blackjack_decisions(
    context_kwargs: dict[str, Any], expected_action: str
) -> None:
    strategy = resolve_strategy("basic")

    assert strategy.choose_action(_action_context(**context_kwargs)) == expected_action


@pytest.mark.parametrize("strategy_name", [strategy.name for strategy in list_builtin_strategies()])
def test_builtin_strategies_take_positive_ev_insurance_from_public_cards(strategy_name: str) -> None:
    strategy = resolve_strategy(strategy_name)
    context = _action_context(
        dealer_rank="A",
        hand_ranks=("10", "6"),
        best_total=16,
        legal_actions=("hit", "stand", "surrender", "insurance"),
        deck_count=1,
        other_player_hands=(("2", "3"), ("4", "5"), ("6", "7"), ("8", "9")),
    )

    assert strategy.choose_action(context) == "insurance"


def test_basic_strategy_declines_negative_ev_insurance_and_keeps_normal_play() -> None:
    strategy = resolve_strategy("basic")
    context = _action_context(
        dealer_rank="A",
        hand_ranks=("10", "6"),
        best_total=16,
        legal_actions=("hit", "stand", "surrender", "insurance"),
    )

    assert strategy.choose_action(context) == "surrender"


def test_counting_strategy_carries_positive_count_into_next_round_bets() -> None:
    strategy = resolve_strategy("counting")
    strategy.choose_action(
        _action_context(
            round_index=1,
            dealer_rank="10",
            hand_ranks=("10", "6"),
            best_total=16,
            legal_actions=("hit", "stand"),
            deck_count=1,
            other_player_hands=(("2", "3"), ("4", "5")),
            cards_remaining=40,
            decks_remaining=40 / 52,
            shuffle_count=1,
        )
    )

    assert strategy.choose_bet(
        _bet_context(
            round_index=2,
            deck_count=1,
            cards_remaining=40,
            decks_remaining=1.0,
            shuffle_count=1,
        )
    ) == 30


def test_counting_strategy_uses_remaining_decks_for_bet_spread() -> None:
    strategy = resolve_strategy("counting")
    strategy.choose_action(
        _action_context(
            round_index=1,
            dealer_rank="10",
            hand_ranks=("10", "6"),
            best_total=16,
            legal_actions=("hit", "stand"),
            deck_count=1,
            other_player_hands=(("2", "3"), ("4", "5")),
            cards_remaining=40,
            decks_remaining=40 / 52,
            shuffle_count=1,
        )
    )

    assert strategy.choose_bet(
        _bet_context(
            round_index=2,
            deck_count=1,
            cards_remaining=26,
            decks_remaining=0.5,
            shuffle_count=1,
        )
    ) == 60


def test_counting_strategy_resets_running_count_after_reshuffle() -> None:
    strategy = resolve_strategy("counting")
    strategy.choose_action(
        _action_context(
            round_index=1,
            dealer_rank="10",
            hand_ranks=("10", "6"),
            best_total=16,
            legal_actions=("hit", "stand"),
            deck_count=1,
            other_player_hands=(("2", "3"), ("4", "5")),
            cards_remaining=40,
            decks_remaining=40 / 52,
            shuffle_count=1,
        )
    )

    assert strategy.choose_bet(
        _bet_context(
            round_index=2,
            deck_count=1,
            cards_remaining=52,
            decks_remaining=1.0,
            shuffle_count=2,
        )
    ) == 10


def test_counting_strategy_does_not_double_count_repeated_public_states() -> None:
    strategy = resolve_strategy("counting")
    context = _action_context(
        round_index=1,
        dealer_rank="10",
        hand_ranks=("10", "6"),
        best_total=16,
        legal_actions=("hit", "stand"),
        deck_count=1,
        other_player_hands=(("2", "3"), ("4", "5")),
        cards_remaining=40,
        decks_remaining=40 / 52,
        shuffle_count=1,
    )

    strategy.choose_action(context)
    strategy.choose_action(context)

    assert strategy.choose_bet(
        _bet_context(
            round_index=2,
            deck_count=1,
            cards_remaining=40,
            decks_remaining=1.0,
            shuffle_count=1,
        )
    ) == 30


def test_counting_strategy_does_not_recount_persistent_cards_after_same_round_shrink() -> None:
    strategy = resolve_strategy("counting")
    strategy.choose_action(
        _action_context(
            round_index=1,
            dealer_rank="9",
            hand_ranks=("10", "6"),
            best_total=16,
            legal_actions=("hit", "stand"),
            deck_count=1,
            other_player_hands=(("10", "2"), ("7",)),
            cards_remaining=40,
            decks_remaining=40 / 52,
            shuffle_count=1,
        )
    )
    strategy.choose_action(
        _action_context(
            round_index=1,
            dealer_rank="9",
            hand_ranks=("6", "2"),
            best_total=8,
            legal_actions=("hit", "stand"),
            deck_count=1,
            other_player_hands=(("5",), ("J",)),
            cards_remaining=40,
            decks_remaining=40 / 52,
            shuffle_count=1,
        )
    )

    assert strategy.choose_bet(
        _bet_context(
            round_index=2,
            deck_count=1,
            cards_remaining=26,
            decks_remaining=0.5,
            shuffle_count=1,
        )
    ) == 10


def test_counting_strategy_falls_back_to_round_index_when_round_id_is_none() -> None:
    strategy = resolve_strategy("counting")
    first_context = _action_context(
        round_index=1,
        dealer_rank="3",
        hand_ranks=("2", "9"),
        best_total=11,
        legal_actions=("hit", "stand"),
        deck_count=1,
        other_player_hands=(("4",),),
        cards_remaining=40,
        decks_remaining=40 / 52,
        shuffle_count=1,
    )
    first_context.round_state["round_id"] = None
    strategy.choose_action(first_context)

    second_context = _action_context(
        round_index=2,
        dealer_rank="3",
        hand_ranks=("5", "9"),
        best_total=14,
        legal_actions=("hit", "stand"),
        deck_count=1,
        cards_remaining=37,
        decks_remaining=37 / 52,
        shuffle_count=1,
    )
    second_context.round_state["round_id"] = None
    strategy.choose_action(second_context)

    assert strategy._running_count_total == 5


@pytest.mark.parametrize(
    ("hand_ranks", "best_total"),
    [
        (("10", "6"), 16),
        (("10", "5"), 15),
    ],
)
def test_counting_strategy_carries_public_running_count_into_next_round_deviations(
    hand_ranks: tuple[str, str], best_total: int
) -> None:
    strategy = resolve_strategy("counting")
    strategy.choose_action(
        _action_context(
            round_index=1,
            dealer_rank="10",
            hand_ranks=("10", "6"),
            best_total=16,
            legal_actions=("hit", "stand"),
            deck_count=1,
            other_player_hands=(("2", "3"), ("4", "5")),
            cards_remaining=40,
            decks_remaining=40 / 52,
            shuffle_count=1,
        )
    )

    assert strategy.choose_action(
        _action_context(
            round_index=2,
            dealer_rank="10",
            hand_ranks=hand_ranks,
            best_total=best_total,
            legal_actions=("hit", "stand"),
            deck_count=1,
            cards_remaining=26,
            decks_remaining=0.5,
            shuffle_count=1,
        )
    ) == "stand"


def test_counting_strategy_uses_positive_count_deviation_on_hard_16() -> None:
    strategy = resolve_strategy("counting")
    context = _action_context(
        dealer_rank="10",
        hand_ranks=("10", "6"),
        best_total=16,
        legal_actions=("hit", "stand"),
        deck_count=1,
        other_player_hands=(("2", "3"), ("4", "5"), ("6", "2"), ("3", "4")),
    )

    assert strategy.choose_action(context) == "stand"
