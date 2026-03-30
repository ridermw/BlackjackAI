from __future__ import annotations

from dataclasses import dataclass
from random import Random
from typing import Sequence

from .models import Card, CardRank, CardSuit


class ShoeExhaustedError(ValueError):
    """Raised when the shoe cannot satisfy a deal request."""


@dataclass(frozen=True, slots=True)
class Shoe:
    cards: tuple[Card, ...]
    next_index: int = 0

    @classmethod
    def from_cards(cls, cards: Sequence[Card]) -> Shoe:
        return cls(cards=tuple(cards))

    @classmethod
    def shuffled(cls, *, deck_count: int = 6, seed: int | None = None) -> Shoe:
        if deck_count <= 0:
            raise ValueError("Deck count must be positive.")

        cards = list(_build_deck() * deck_count)
        Random(seed).shuffle(cards)
        return cls(cards=tuple(cards))

    @property
    def remaining(self) -> int:
        return len(self.cards) - self.next_index

    def draw(self) -> tuple[Card, Shoe]:
        cards, updated_shoe = self.deal(1)
        return cards[0], updated_shoe

    def deal(self, count: int) -> tuple[tuple[Card, ...], Shoe]:
        if count < 0:
            raise ValueError("Deal count cannot be negative.")
        if self.remaining < count:
            raise ShoeExhaustedError("Shoe does not contain enough cards.")

        start = self.next_index
        end = start + count
        return self.cards[start:end], Shoe(cards=self.cards, next_index=end)

    def remaining_cards(self) -> tuple[Card, ...]:
        return self.cards[self.next_index :]


def _build_deck() -> tuple[Card, ...]:
    return tuple(Card(rank=rank, suit=suit) for suit in CardSuit for rank in CardRank)
