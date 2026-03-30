from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from blackjack_ai.engine import ActionType
from blackjack_ai.engine import ParticipantType
from blackjack_ai.engine import PayoutRatio
from blackjack_ai.engine import RuleConfig


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PayoutRatioRequest(StrictModel):
    numerator: int = Field(default=3, ge=1)
    denominator: int = Field(default=2, ge=1)

    def to_domain(self) -> PayoutRatio:
        return PayoutRatio(
            numerator=self.numerator,
            denominator=self.denominator,
        )


class RuleConfigRequest(StrictModel):
    deck_count: int = Field(default=6, ge=1)
    dealer_stands_on_soft_17: bool = True
    blackjack_payout: PayoutRatioRequest = Field(default_factory=PayoutRatioRequest)
    minimum_bet: int = Field(default=10, ge=1)
    maximum_bet: int = Field(default=500, ge=1)
    allow_double_after_split: bool = True
    maximum_split_depth: int = Field(default=3, ge=0)
    split_on_value_match: bool = False

    def to_domain(self) -> RuleConfig:
        return RuleConfig(
            deck_count=self.deck_count,
            dealer_stands_on_soft_17=self.dealer_stands_on_soft_17,
            blackjack_payout=self.blackjack_payout.to_domain(),
            minimum_bet=self.minimum_bet,
            maximum_bet=self.maximum_bet,
            allow_double_after_split=self.allow_double_after_split,
            maximum_split_depth=self.maximum_split_depth,
            split_on_value_match=self.split_on_value_match,
        )


class CreatePlayerRequest(StrictModel):
    player_id: str | None = None
    display_name: str = Field(min_length=1)
    participant_type: ParticipantType
    starting_bankroll: int = Field(default=1000, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CreateTableRequest(StrictModel):
    table_id: str | None = None
    seat_count: int = Field(default=5, ge=1, le=10)
    rules: RuleConfigRequest | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SeatJoinRequest(StrictModel):
    player_id: str = Field(min_length=1)
    bankroll: int | None = Field(default=None, ge=1)


class SeatLeaveRequest(StrictModel):
    player_id: str = Field(min_length=1)


class StartRoundRequest(StrictModel):
    round_id: str | None = None


class BetRequest(StrictModel):
    player_id: str = Field(min_length=1)
    amount: int = Field(gt=0)
    request_id: str | None = Field(default=None, min_length=1)
    expected_version: int | None = Field(default=None, ge=0)


class ActionRequest(StrictModel):
    player_id: str = Field(min_length=1)
    hand_id: str = Field(min_length=1)
    action: ActionType
    request_id: str | None = Field(default=None, min_length=1)
    expected_version: int | None = Field(default=None, ge=0)
