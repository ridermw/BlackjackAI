from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Sequence

from .models import (
    ActionType,
    Bet,
    Card,
    CardRank,
    DealerState,
    HandOutcome,
    HandResolution,
    HandState,
    HandStatus,
    HandValue,
    ParticipantType,
    ResolutionReason,
    RoundParticipantState,
    RoundPhase,
    RoundState,
    RuleConfig,
    TurnActor,
    TurnState,
)
from .shoe import Shoe


class RoundFlowError(ValueError):
    """Raised when the round cannot advance from the current state."""


class IllegalActionError(RoundFlowError):
    """Raised when an action is attempted outside the legal move set."""


@dataclass(frozen=True, slots=True)
class RoundPlayerInput:
    player_id: str
    display_name: str
    seat_number: int
    bankroll: int
    wager: Bet
    participant_type: ParticipantType = ParticipantType.HUMAN

    def __post_init__(self) -> None:
        if self.seat_number <= 0:
            raise ValueError("Seat number must be positive.")
        if self.bankroll <= 0:
            raise ValueError("Bankroll must be positive.")


@dataclass(frozen=True, slots=True)
class RoundUpdate:
    round_state: RoundState
    shoe: Shoe


class RoundService:
    def start_round(
        self,
        *,
        round_id: str,
        table_id: str,
        players: Sequence[RoundPlayerInput],
        shoe: Shoe,
        rules: RuleConfig | None = None,
    ) -> RoundUpdate:
        if not players:
            raise RoundFlowError("A round requires at least one player.")

        effective_rules = rules or RuleConfig()
        ordered_players = tuple(sorted(players, key=lambda player: player.seat_number))
        self._validate_round_inputs(ordered_players, effective_rules)

        participants = tuple(
            RoundParticipantState(
                player_id=player.player_id,
                display_name=player.display_name,
                participant_type=player.participant_type,
                seat_number=player.seat_number,
                bankroll_before_round=player.bankroll,
                hands=(
                    HandState(
                        hand_id=f"{round_id}-seat-{player.seat_number}-hand-1",
                        player_id=player.player_id,
                        seat_number=player.seat_number,
                        wager=player.wager,
                        status=HandStatus.PENDING,
                    ),
                ),
            )
            for player in ordered_players
        )
        dealer = DealerState(
            hand=HandState(
                hand_id=f"{round_id}-dealer-hand",
                player_id="dealer",
                seat_number=0,
                status=HandStatus.PENDING,
            )
        )
        round_state = RoundState(
            round_id=round_id,
            table_id=table_id,
            phase=RoundPhase.DEALING,
            rules=effective_rules,
            participants=participants,
            dealer=dealer,
        )
        return self._deal_initial_cards(round_state, shoe)

    def apply_action(
        self,
        *,
        round_state: RoundState,
        shoe: Shoe,
        player_id: str,
        hand_id: str,
        action: ActionType | str,
    ) -> RoundUpdate:
        selected_action = ActionType(action)
        if (
            round_state.phase is not RoundPhase.PLAYER_TURNS
            or round_state.turn is None
            or round_state.turn.actor is not TurnActor.PLAYER
        ):
            raise RoundFlowError("Round is not waiting for a player action.")
        if round_state.turn.player_id != player_id or round_state.turn.hand_id != hand_id:
            raise IllegalActionError("Action is not for the current player hand.")

        participant_index, participant = self._participant_index(round_state, player_id)
        hand_index, hand = self._hand_index(participant, hand_id)
        legal_actions = hand.legal_actions(
            round_state.rules,
            participant.available_bankroll,
            dealer_up_card=_dealer_up_card(round_state.dealer),
        )
        if selected_action not in legal_actions:
            raise IllegalActionError(
                f"Action '{selected_action.value}' is not legal for hand '{hand_id}'."
            )

        updated_participant, updated_shoe = self._apply_player_action(
            participant=participant,
            hand_index=hand_index,
            action=selected_action,
            shoe=shoe,
        )
        participants = list(round_state.participants)
        participants[participant_index] = updated_participant
        updated_state = replace(
            round_state,
            participants=tuple(participants),
            action_count=round_state.action_count + 1,
            version=round_state.version + 1,
            turn=None,
        )
        return self._advance_round(updated_state, updated_shoe)

    def _validate_round_inputs(
        self,
        players: Sequence[RoundPlayerInput],
        rules: RuleConfig,
    ) -> None:
        seen_player_ids: set[str] = set()
        seen_seats: set[int] = set()
        for player in players:
            if player.player_id in seen_player_ids:
                raise RoundFlowError(f"Duplicate player id '{player.player_id}'.")
            if player.seat_number in seen_seats:
                raise RoundFlowError(f"Duplicate seat number '{player.seat_number}'.")
            player.wager.validate(rules, player.bankroll)
            seen_player_ids.add(player.player_id)
            seen_seats.add(player.seat_number)

    def _deal_initial_cards(self, round_state: RoundState, shoe: Shoe) -> RoundUpdate:
        dealt_cards: dict[str, list[Card]] = {participant.player_id: [] for participant in round_state.participants}
        dealer_cards: list[Card] = []
        updated_shoe = shoe

        for _ in range(2):
            for participant in round_state.participants:
                card, updated_shoe = updated_shoe.draw()
                dealt_cards[participant.player_id].append(card)
            card, updated_shoe = updated_shoe.draw()
            dealer_cards.append(card)

        participants: list[RoundParticipantState] = []
        for participant in round_state.participants:
            hand = participant.hands[0]
            cards = tuple(dealt_cards[participant.player_id])
            participants.append(
                replace(
                    participant,
                    hands=(
                        replace(
                            hand,
                            cards=cards,
                            status=_terminal_or_active_status(cards, counts_as_blackjack=True),
                        ),
                    ),
                )
            )

        dealer_hand = replace(
            round_state.dealer.hand,
            cards=tuple(dealer_cards),
            status=_terminal_or_active_status(tuple(dealer_cards), counts_as_blackjack=True),
        )
        if dealer_hand.status is HandStatus.BLACKJACK and _dealer_shows_ace_from_cards(dealer_hand.cards):
            dealer_hand = replace(dealer_hand, status=HandStatus.ACTIVE)
        dealt_state = replace(
            round_state,
            phase=RoundPhase.PLAYER_TURNS,
            participants=tuple(participants),
            dealer=replace(round_state.dealer, hand=dealer_hand, hole_card_revealed=False),
            turn=None,
        )
        return self._advance_round(dealt_state, updated_shoe)

    def _advance_round(self, round_state: RoundState, shoe: Shoe) -> RoundUpdate:
        current_state = round_state
        current_shoe = shoe

        while True:
            if current_state.phase is RoundPhase.PLAYER_TURNS:
                if current_state.dealer.hand.status is HandStatus.BLACKJACK:
                    current_state = replace(
                        current_state,
                        phase=RoundPhase.SETTLEMENT,
                        dealer=replace(current_state.dealer, hole_card_revealed=True),
                        turn=None,
                    )
                    continue

                next_turn = self._next_player_turn(current_state)
                if next_turn is not None:
                    return RoundUpdate(round_state=replace(current_state, turn=next_turn), shoe=current_shoe)

                current_state = replace(
                    current_state,
                    phase=RoundPhase.DEALER_TURN,
                    turn=TurnState.for_dealer(reason="all_player_hands_resolved"),
                )
                continue

            if current_state.phase is RoundPhase.DEALER_TURN:
                current_state, current_shoe = self._run_dealer_turn(current_state, current_shoe)
                current_state = replace(current_state, phase=RoundPhase.SETTLEMENT, turn=None)
                continue

            if current_state.phase is RoundPhase.SETTLEMENT:
                settled_state = self._settle_round(current_state)
                return RoundUpdate(
                    round_state=replace(settled_state, phase=RoundPhase.COMPLETE, turn=None),
                    shoe=current_shoe,
                )

            if current_state.phase is RoundPhase.COMPLETE:
                return RoundUpdate(round_state=current_state, shoe=current_shoe)

            raise RoundFlowError(f"Cannot advance round from phase '{current_state.phase.value}'.")

    def _next_player_turn(self, round_state: RoundState) -> TurnState | None:
        dealer_up_card = _dealer_up_card(round_state.dealer)
        for participant in round_state.participants:
            for hand in participant.hands:
                if hand.status is not HandStatus.ACTIVE:
                    continue
                actions = hand.legal_actions(
                    round_state.rules,
                    participant.available_bankroll,
                    dealer_up_card=dealer_up_card,
                )
                if actions:
                    return TurnState.for_player_hand(
                        participant,
                        hand,
                        round_state.rules,
                        dealer_up_card=dealer_up_card,
                    )
        return None

    def _run_dealer_turn(self, round_state: RoundState, shoe: Shoe) -> tuple[RoundState, Shoe]:
        dealer = replace(round_state.dealer, hole_card_revealed=True)
        dealer_hand = dealer.hand
        action_count = round_state.action_count
        updated_shoe = shoe

        if dealer_hand.status is HandStatus.BLACKJACK:
            return replace(round_state, dealer=dealer, turn=None), updated_shoe

        if not _dealer_should_play(round_state.participants):
            dealer_hand = replace(
                dealer_hand,
                status=_terminal_or_standing_status(dealer_hand.cards, counts_as_blackjack=True),
            )
            return replace(round_state, dealer=replace(dealer, hand=dealer_hand), turn=None), updated_shoe

        while dealer.should_hit(round_state.rules):
            card, updated_shoe = updated_shoe.draw()
            cards = dealer_hand.cards + (card,)
            dealer_hand = replace(
                dealer_hand,
                cards=cards,
                status=_terminal_or_active_status(cards, counts_as_blackjack=True),
            )
            dealer = replace(dealer, hand=dealer_hand, hole_card_revealed=True)
            action_count += 1

        if dealer_hand.status is HandStatus.ACTIVE:
            dealer_hand = replace(
                dealer_hand,
                status=_terminal_or_standing_status(dealer_hand.cards, counts_as_blackjack=True),
            )
            dealer = replace(dealer, hand=dealer_hand, hole_card_revealed=True)

        return replace(round_state, dealer=dealer, action_count=action_count, turn=None), updated_shoe

    def _settle_round(self, round_state: RoundState) -> RoundState:
        settled_participants: list[RoundParticipantState] = []
        for participant in round_state.participants:
            settled_hands: list[HandState] = []
            net_change = _insurance_net_change(participant, round_state.dealer.hand)
            for hand in participant.hands:
                resolution = _resolve_hand(hand=hand, dealer_hand=round_state.dealer.hand, rules=round_state.rules)
                settled_hands.append(replace(hand, resolution=resolution))
                net_change += resolution.net_change

            settled_participants.append(
                replace(
                    participant,
                    hands=tuple(settled_hands),
                    bankroll_after_round=participant.bankroll_before_round + net_change,
                )
            )

        return replace(
            round_state,
            participants=tuple(settled_participants),
            dealer=replace(round_state.dealer, hole_card_revealed=True),
            turn=None,
        )

    def _apply_player_action(
        self,
        *,
        participant: RoundParticipantState,
        hand_index: int,
        action: ActionType,
        shoe: Shoe,
    ) -> tuple[RoundParticipantState, Shoe]:
        hands = list(participant.hands)
        hand = hands[hand_index]

        if action is ActionType.STAND:
            hands[hand_index] = replace(hand, status=HandStatus.STANDING)
            return replace(participant, hands=tuple(hands)), shoe

        if action is ActionType.HIT:
            card, updated_shoe = shoe.draw()
            cards = hand.cards + (card,)
            hands[hand_index] = replace(
                hand,
                cards=cards,
                status=_terminal_or_active_status(cards, counts_as_blackjack=not hand.is_from_split),
            )
            return replace(participant, hands=tuple(hands)), updated_shoe

        if action is ActionType.DOUBLE:
            if hand.wager is None:
                raise RoundFlowError("Cannot double a hand without a wager.")

            card, updated_shoe = shoe.draw()
            cards = hand.cards + (card,)
            status = _terminal_or_active_status(cards, counts_as_blackjack=not hand.is_from_split)
            hands[hand_index] = replace(
                hand,
                cards=cards,
                wager=Bet(amount=hand.wager.amount * 2, currency=hand.wager.currency),
                doubled_down=True,
                status=HandStatus.BUSTED if status is HandStatus.BUSTED else HandStatus.STANDING,
            )
            return replace(participant, hands=tuple(hands)), updated_shoe

        if action is ActionType.SURRENDER:
            if hand.wager is None:
                raise RoundFlowError("Cannot surrender a hand without a wager.")

            hands[hand_index] = replace(
                hand,
                status=HandStatus.COMPLETE,
                resolution=HandResolution(
                    outcome=HandOutcome.LOSS,
                    reason=ResolutionReason.SURRENDER,
                    net_change=-(hand.wager.amount // 2),
                    player_total=hand.value.best_total,
                    dealer_total=None,
                ),
            )
            return replace(participant, hands=tuple(hands)), shoe

        if action is ActionType.INSURANCE:
            if hand.wager is None:
                raise RoundFlowError("Cannot insure a hand without a wager.")
            hands[hand_index] = replace(
                hand,
                insurance_wager=Bet(amount=hand.wager.amount // 2, currency=hand.wager.currency),
            )
            return replace(participant, hands=tuple(hands)), shoe

        if action is ActionType.SPLIT:
            if hand.wager is None:
                raise RoundFlowError("Cannot split a hand without a wager.")

            first_draw, shoe_after_first = shoe.draw()
            second_draw, updated_shoe = shoe_after_first.draw()
            first_source, second_source = hand.cards
            split_depth = hand.split_depth + 1
            replacement_hands = [
                HandState(
                    hand_id=_split_hand_id(hand.hand_id, 1),
                    player_id=hand.player_id,
                    seat_number=hand.seat_number,
                    cards=(first_source, first_draw),
                    wager=hand.wager,
                    insurance_wager=hand.insurance_wager,
                    status=_terminal_or_active_status(
                        (first_source, first_draw),
                        counts_as_blackjack=False,
                    ),
                    split_depth=split_depth,
                    parent_hand_id=hand.hand_id,
                ),
                HandState(
                    hand_id=_split_hand_id(hand.hand_id, 2),
                    player_id=hand.player_id,
                    seat_number=hand.seat_number,
                    cards=(second_source, second_draw),
                    wager=hand.wager,
                    status=_terminal_or_active_status(
                        (second_source, second_draw),
                        counts_as_blackjack=False,
                    ),
                    split_depth=split_depth,
                    parent_hand_id=hand.hand_id,
                ),
            ]
            hands[hand_index : hand_index + 1] = replacement_hands
            return replace(participant, hands=tuple(hands)), updated_shoe

        raise IllegalActionError(f"Unsupported action '{action.value}'.")

    def _participant_index(
        self,
        round_state: RoundState,
        player_id: str,
    ) -> tuple[int, RoundParticipantState]:
        for index, participant in enumerate(round_state.participants):
            if participant.player_id == player_id:
                return index, participant
        raise RoundFlowError(f"Unknown player '{player_id}'.")

    def _hand_index(
        self,
        participant: RoundParticipantState,
        hand_id: str,
    ) -> tuple[int, HandState]:
        for index, hand in enumerate(participant.hands):
            if hand.hand_id == hand_id:
                return index, hand
        raise RoundFlowError(f"Unknown hand '{hand_id}' for player '{participant.player_id}'.")


def _dealer_should_play(participants: Sequence[RoundParticipantState]) -> bool:
    return any(
        hand.resolution is None and hand.status not in {HandStatus.BUSTED, HandStatus.BLACKJACK, HandStatus.COMPLETE}
        for participant in participants
        for hand in participant.hands
    )


def _split_hand_id(parent_hand_id: str, branch_index: int) -> str:
    return f"{parent_hand_id}:{branch_index}"


def _dealer_up_card(dealer: DealerState) -> Card | None:
    visible_cards = dealer.visible_cards()
    return visible_cards[0] if visible_cards else None


def _dealer_shows_ace(dealer: DealerState) -> bool:
    up_card = _dealer_up_card(dealer)
    return up_card is not None and up_card.rank is CardRank.ACE


def _dealer_shows_ace_from_cards(cards: Sequence[Card]) -> bool:
    return bool(cards) and cards[0].rank is CardRank.ACE


def _insurance_net_change(participant: RoundParticipantState, dealer_hand: HandState) -> int:
    insurance_wager = next(
        (hand.insurance_wager for hand in participant.hands if hand.insurance_wager is not None),
        None,
    )
    if insurance_wager is None:
        return 0
    return insurance_wager.amount * 2 if dealer_hand.value.is_blackjack else -insurance_wager.amount


def _resolve_hand(
    *,
    hand: HandState,
    dealer_hand: HandState,
    rules: RuleConfig,
) -> HandResolution:
    if hand.wager is None:
        raise RoundFlowError("Cannot settle a hand without a wager.")
    if hand.resolution is not None:
        return hand.resolution

    player_value = hand.value
    dealer_value = dealer_hand.value
    wager_amount = hand.wager.amount
    player_total = player_value.best_total
    dealer_total = dealer_value.best_total

    if player_value.is_bust:
        return HandResolution(
            outcome=HandOutcome.LOSS,
            reason=ResolutionReason.PLAYER_BUST,
            net_change=-wager_amount,
            player_total=player_total,
            dealer_total=dealer_total,
        )

    if player_value.is_blackjack:
        if dealer_value.is_blackjack:
            return HandResolution(
                outcome=HandOutcome.PUSH,
                reason=ResolutionReason.EQUAL_TOTAL,
                net_change=0,
                player_total=player_total,
                dealer_total=dealer_total,
            )
        return HandResolution(
            outcome=HandOutcome.WIN,
            reason=ResolutionReason.BLACKJACK,
            net_change=(wager_amount * rules.blackjack_payout.numerator)
            // rules.blackjack_payout.denominator,
            player_total=player_total,
            dealer_total=dealer_total,
        )

    if dealer_value.is_blackjack:
        return HandResolution(
            outcome=HandOutcome.LOSS,
            reason=ResolutionReason.BLACKJACK,
            net_change=-wager_amount,
            player_total=player_total,
            dealer_total=dealer_total,
        )

    if dealer_value.is_bust:
        return HandResolution(
            outcome=HandOutcome.WIN,
            reason=ResolutionReason.DEALER_BUST,
            net_change=wager_amount,
            player_total=player_total,
            dealer_total=dealer_total,
        )

    if player_total is None or dealer_total is None:
        raise RoundFlowError("Cannot settle hands without comparable totals.")

    if player_total > dealer_total:
        return HandResolution(
            outcome=HandOutcome.WIN,
            reason=ResolutionReason.HIGHER_TOTAL,
            net_change=wager_amount,
            player_total=player_total,
            dealer_total=dealer_total,
        )

    if player_total < dealer_total:
        return HandResolution(
            outcome=HandOutcome.LOSS,
            reason=ResolutionReason.LOWER_TOTAL,
            net_change=-wager_amount,
            player_total=player_total,
            dealer_total=dealer_total,
        )

    return HandResolution(
        outcome=HandOutcome.PUSH,
        reason=ResolutionReason.EQUAL_TOTAL,
        net_change=0,
        player_total=player_total,
        dealer_total=dealer_total,
    )


def _terminal_or_active_status(
    cards: Sequence[Card],
    *,
    counts_as_blackjack: bool,
) -> HandStatus:
    terminal_status = _terminal_status(cards, counts_as_blackjack=counts_as_blackjack)
    return terminal_status if terminal_status is not None else HandStatus.ACTIVE


def _terminal_or_standing_status(
    cards: Sequence[Card],
    *,
    counts_as_blackjack: bool,
) -> HandStatus:
    terminal_status = _terminal_status(cards, counts_as_blackjack=counts_as_blackjack)
    return terminal_status if terminal_status is not None else HandStatus.STANDING


def _terminal_status(
    cards: Sequence[Card],
    *,
    counts_as_blackjack: bool,
) -> HandStatus | None:
    value = HandValue.from_cards(cards, counts_as_blackjack=counts_as_blackjack)
    if value.is_bust:
        return HandStatus.BUSTED
    if value.is_blackjack:
        return HandStatus.BLACKJACK
    if value.best_total == 21:
        return HandStatus.STANDING
    return None
