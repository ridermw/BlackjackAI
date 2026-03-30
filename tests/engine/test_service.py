from __future__ import annotations

import unittest

from blackjack_ai.engine import (
    ActionType,
    Bet,
    Card,
    CardRank,
    CardSuit,
    HandOutcome,
    HandStatus,
    IllegalActionError,
    ParticipantType,
    ResolutionReason,
    RoundPhase,
    RoundPlayerInput,
    RoundService,
    Shoe,
)


def make_card(rank: CardRank, suit: CardSuit) -> Card:
    return Card(rank=rank, suit=suit)


class ShoeTests(unittest.TestCase):
    def test_seeded_shuffle_is_repeatable(self) -> None:
        first = Shoe.shuffled(deck_count=1, seed=17)
        second = Shoe.shuffled(deck_count=1, seed=17)
        third = Shoe.shuffled(deck_count=1, seed=18)

        self.assertEqual(first.cards, second.cards)
        self.assertNotEqual(first.cards, third.cards)
        self.assertEqual(first.remaining, 52)


class RoundServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = RoundService()

    def test_initial_deal_orders_seats_and_skips_blackjack_hands(self) -> None:
        shoe = Shoe.from_cards(
            (
                make_card(CardRank.ACE, CardSuit.SPADES),
                make_card(CardRank.NINE, CardSuit.CLUBS),
                make_card(CardRank.SIX, CardSuit.SPADES),
                make_card(CardRank.KING, CardSuit.HEARTS),
                make_card(CardRank.SEVEN, CardSuit.DIAMONDS),
                make_card(CardRank.TEN, CardSuit.CLUBS),
            )
        )
        update = self.service.start_round(
            round_id="round-1",
            table_id="table-1",
            players=(
                RoundPlayerInput(
                    player_id="alice",
                    display_name="Alice",
                    participant_type=ParticipantType.HUMAN,
                    seat_number=2,
                    bankroll=200,
                    wager=Bet(amount=20),
                ),
                RoundPlayerInput(
                    player_id="bob",
                    display_name="Bob",
                    participant_type=ParticipantType.AI,
                    seat_number=1,
                    bankroll=200,
                    wager=Bet(amount=20),
                ),
            ),
            shoe=shoe,
        )

        state = update.round_state
        bob, alice = state.participants

        self.assertEqual(state.phase, RoundPhase.PLAYER_TURNS)
        self.assertEqual([participant.player_id for participant in state.participants], ["bob", "alice"])
        self.assertEqual(bob.hands[0].status, HandStatus.BLACKJACK)
        self.assertEqual(alice.hands[0].status, HandStatus.ACTIVE)
        self.assertEqual(state.turn.player_id, "alice")
        self.assertEqual(state.turn.hand_id, alice.hands[0].hand_id)
        self.assertFalse(state.dealer.hole_card_revealed)
        self.assertEqual(update.shoe.remaining, 0)

    def test_turn_advances_between_players_before_dealer_automation(self) -> None:
        shoe = Shoe.from_cards(
            (
                make_card(CardRank.TEN, CardSuit.SPADES),
                make_card(CardRank.NINE, CardSuit.CLUBS),
                make_card(CardRank.SIX, CardSuit.SPADES),
                make_card(CardRank.SEVEN, CardSuit.HEARTS),
                make_card(CardRank.SEVEN, CardSuit.DIAMONDS),
                make_card(CardRank.TEN, CardSuit.CLUBS),
                make_card(CardRank.TWO, CardSuit.HEARTS),
            )
        )
        start = self.service.start_round(
            round_id="round-2",
            table_id="table-1",
            players=(
                RoundPlayerInput(
                    player_id="alice",
                    display_name="Alice",
                    seat_number=1,
                    bankroll=100,
                    wager=Bet(amount=20),
                ),
                RoundPlayerInput(
                    player_id="bob",
                    display_name="Bob",
                    seat_number=2,
                    bankroll=100,
                    wager=Bet(amount=20),
                ),
            ),
            shoe=shoe,
        )

        first_hand = start.round_state.participants[0].hands[0]
        after_first = self.service.apply_action(
            round_state=start.round_state,
            shoe=start.shoe,
            player_id="alice",
            hand_id=first_hand.hand_id,
            action=ActionType.STAND,
        )

        self.assertEqual(after_first.round_state.phase, RoundPhase.PLAYER_TURNS)
        self.assertEqual(after_first.round_state.turn.player_id, "bob")

        second_hand = after_first.round_state.participants[1].hands[0]
        complete = self.service.apply_action(
            round_state=after_first.round_state,
            shoe=after_first.shoe,
            player_id="bob",
            hand_id=second_hand.hand_id,
            action=ActionType.STAND,
        )

        self.assertEqual(complete.round_state.phase, RoundPhase.COMPLETE)
        self.assertIsNone(complete.round_state.turn)
        self.assertEqual(complete.round_state.dealer.hand.cards[-1], make_card(CardRank.TWO, CardSuit.HEARTS))
        self.assertTrue(complete.round_state.dealer.hole_card_revealed)
        self.assertEqual(complete.round_state.action_count, 3)
        self.assertEqual(complete.round_state.participants[0].bankroll_after_round, 80)
        self.assertEqual(complete.round_state.participants[1].bankroll_after_round, 80)

    def test_illegal_action_is_rejected(self) -> None:
        shoe = Shoe.from_cards(
            (
                make_card(CardRank.TEN, CardSuit.SPADES),
                make_card(CardRank.SIX, CardSuit.CLUBS),
                make_card(CardRank.SEVEN, CardSuit.HEARTS),
                make_card(CardRank.TEN, CardSuit.DIAMONDS),
            )
        )
        start = self.service.start_round(
            round_id="round-3",
            table_id="table-1",
            players=(
                RoundPlayerInput(
                    player_id="alice",
                    display_name="Alice",
                    seat_number=1,
                    bankroll=100,
                    wager=Bet(amount=20),
                ),
            ),
            shoe=shoe,
        )
        hand = start.round_state.participants[0].hands[0]

        with self.assertRaises(IllegalActionError):
            self.service.apply_action(
                round_state=start.round_state,
                shoe=start.shoe,
                player_id="alice",
                hand_id=hand.hand_id,
                action=ActionType.SPLIT,
            )

    def test_surrender_resolves_for_half_loss_without_dealer_draw(self) -> None:
        shoe = Shoe.from_cards(
            (
                make_card(CardRank.TEN, CardSuit.SPADES),
                make_card(CardRank.SIX, CardSuit.CLUBS),
                make_card(CardRank.SEVEN, CardSuit.HEARTS),
                make_card(CardRank.NINE, CardSuit.DIAMONDS),
                make_card(CardRank.FIVE, CardSuit.SPADES),
            )
        )
        start = self.service.start_round(
            round_id="round-surrender",
            table_id="table-1",
            players=(
                RoundPlayerInput(
                    player_id="alice",
                    display_name="Alice",
                    seat_number=1,
                    bankroll=100,
                    wager=Bet(amount=20),
                ),
            ),
            shoe=shoe,
        )
        hand = start.round_state.participants[0].hands[0]

        self.assertIn(ActionType.SURRENDER, start.round_state.turn.legal_actions)

        complete = self.service.apply_action(
            round_state=start.round_state,
            shoe=start.shoe,
            player_id="alice",
            hand_id=hand.hand_id,
            action=ActionType.SURRENDER,
        )

        state = complete.round_state
        surrendered_hand = state.participants[0].hands[0]

        self.assertEqual(state.phase, RoundPhase.COMPLETE)
        self.assertEqual(surrendered_hand.status, HandStatus.COMPLETE)
        self.assertEqual(surrendered_hand.resolution.outcome, HandOutcome.LOSS)
        self.assertEqual(surrendered_hand.resolution.reason, ResolutionReason.SURRENDER)
        self.assertEqual(surrendered_hand.resolution.net_change, -10)
        self.assertIsNone(surrendered_hand.resolution.dealer_total)
        self.assertEqual(state.participants[0].bankroll_after_round, 90)
        self.assertEqual(
            state.dealer.hand.cards,
            (
                make_card(CardRank.SIX, CardSuit.CLUBS),
                make_card(CardRank.NINE, CardSuit.DIAMONDS),
            ),
        )
        self.assertEqual(state.action_count, 1)

    def test_surrender_advances_to_next_player_without_revealing_dealer(self) -> None:
        shoe = Shoe.from_cards(
            (
                make_card(CardRank.TEN, CardSuit.SPADES),
                make_card(CardRank.NINE, CardSuit.CLUBS),
                make_card(CardRank.SIX, CardSuit.DIAMONDS),
                make_card(CardRank.SEVEN, CardSuit.HEARTS),
                make_card(CardRank.EIGHT, CardSuit.SPADES),
                make_card(CardRank.KING, CardSuit.CLUBS),
                make_card(CardRank.TWO, CardSuit.HEARTS),
            )
        )
        start = self.service.start_round(
            round_id="round-surrender-next",
            table_id="table-1",
            players=(
                RoundPlayerInput(
                    player_id="alice",
                    display_name="Alice",
                    seat_number=1,
                    bankroll=100,
                    wager=Bet(amount=20),
                ),
                RoundPlayerInput(
                    player_id="bob",
                    display_name="Bob",
                    seat_number=2,
                    bankroll=100,
                    wager=Bet(amount=20),
                ),
            ),
            shoe=shoe,
        )
        alice_hand = start.round_state.participants[0].hands[0]

        after_surrender = self.service.apply_action(
            round_state=start.round_state,
            shoe=start.shoe,
            player_id="alice",
            hand_id=alice_hand.hand_id,
            action=ActionType.SURRENDER,
        )

        state = after_surrender.round_state
        surrendered_hand = state.participants[0].hands[0]

        self.assertEqual(state.phase, RoundPhase.PLAYER_TURNS)
        self.assertEqual(state.turn.player_id, "bob")
        self.assertEqual(surrendered_hand.status, HandStatus.COMPLETE)
        self.assertEqual(surrendered_hand.resolution.reason, ResolutionReason.SURRENDER)
        self.assertFalse(state.dealer.hole_card_revealed)
        self.assertIn(ActionType.SURRENDER, state.turn.legal_actions)

    def test_insurance_action_stays_on_same_hand_and_reduces_opening_options(self) -> None:
        shoe = Shoe.from_cards(
            (
                make_card(CardRank.TEN, CardSuit.SPADES),
                make_card(CardRank.NINE, CardSuit.CLUBS),
                make_card(CardRank.ACE, CardSuit.DIAMONDS),
                make_card(CardRank.SEVEN, CardSuit.HEARTS),
                make_card(CardRank.EIGHT, CardSuit.SPADES),
                make_card(CardRank.NINE, CardSuit.CLUBS),
            )
        )
        start = self.service.start_round(
            round_id="round-insurance",
            table_id="table-1",
            players=(
                RoundPlayerInput(
                    player_id="alice",
                    display_name="Alice",
                    seat_number=1,
                    bankroll=30,
                    wager=Bet(amount=20),
                ),
                RoundPlayerInput(
                    player_id="bob",
                    display_name="Bob",
                    seat_number=2,
                    bankroll=100,
                    wager=Bet(amount=20),
                ),
            ),
            shoe=shoe,
        )

        state = start.round_state
        public_state = state.to_public_dict()
        alice_hand = state.participants[0].hands[0]

        self.assertEqual(state.phase, RoundPhase.PLAYER_TURNS)
        self.assertEqual(state.turn.player_id, "alice")
        self.assertEqual(public_state["dealer"]["cards"][1], {"is_hidden": True})
        self.assertEqual(public_state["dealer"]["status"], "active")
        self.assertNotIn("insurance", public_state["participants"][0])
        self.assertEqual(
            tuple(action.value for action in state.turn.legal_actions),
            ("hit", "stand", "surrender", "insurance"),
        )

        after_insurance = self.service.apply_action(
            round_state=state,
            shoe=start.shoe,
            player_id="alice",
            hand_id=alice_hand.hand_id,
            action=ActionType.INSURANCE,
        )

        self.assertEqual(after_insurance.round_state.phase, RoundPhase.PLAYER_TURNS)
        self.assertEqual(after_insurance.round_state.turn.player_id, "alice")
        self.assertEqual(after_insurance.round_state.turn.hand_id, alice_hand.hand_id)
        self.assertEqual(
            tuple(action.value for action in after_insurance.round_state.turn.legal_actions),
            ("hit", "stand", "surrender"),
        )
        self.assertEqual(after_insurance.round_state.participants[0].available_bankroll, 0)
        self.assertEqual(after_insurance.round_state.to_public_dict()["dealer"]["cards"][1], {"is_hidden": True})

        after_stand = self.service.apply_action(
            round_state=after_insurance.round_state,
            shoe=after_insurance.shoe,
            player_id="alice",
            hand_id=alice_hand.hand_id,
            action=ActionType.STAND,
        )

        state = after_stand.round_state

        self.assertEqual(state.phase, RoundPhase.PLAYER_TURNS)
        self.assertEqual(state.turn.player_id, "bob")
        self.assertIn(ActionType.INSURANCE, state.turn.legal_actions)
        self.assertEqual(state.to_public_dict()["dealer"]["cards"][1], {"is_hidden": True})

    def test_insurance_can_offset_a_dealer_blackjack_loss(self) -> None:
        shoe = Shoe.from_cards(
            (
                make_card(CardRank.TEN, CardSuit.SPADES),
                make_card(CardRank.ACE, CardSuit.DIAMONDS),
                make_card(CardRank.SEVEN, CardSuit.HEARTS),
                make_card(CardRank.KING, CardSuit.CLUBS),
            )
        )
        start = self.service.start_round(
            round_id="round-insurance-blackjack",
            table_id="table-1",
            players=(
                RoundPlayerInput(
                    player_id="alice",
                    display_name="Alice",
                    seat_number=1,
                    bankroll=100,
                    wager=Bet(amount=20),
                ),
            ),
            shoe=shoe,
        )

        self.assertEqual(start.round_state.phase, RoundPhase.PLAYER_TURNS)
        self.assertEqual(start.round_state.to_public_dict()["dealer"]["status"], "active")
        self.assertEqual(start.round_state.to_public_dict()["dealer"]["cards"][1], {"is_hidden": True})
        hand = start.round_state.participants[0].hands[0]
        self.assertIn(ActionType.INSURANCE, start.round_state.turn.legal_actions)

        after_insurance = self.service.apply_action(
            round_state=start.round_state,
            shoe=start.shoe,
            player_id="alice",
            hand_id=hand.hand_id,
            action=ActionType.INSURANCE,
        )
        self.assertNotIn(ActionType.INSURANCE, after_insurance.round_state.turn.legal_actions)

        complete = self.service.apply_action(
            round_state=after_insurance.round_state,
            shoe=after_insurance.shoe,
            player_id="alice",
            hand_id=hand.hand_id,
            action=ActionType.STAND,
        )

        state = complete.round_state
        participant = state.participants[0]

        self.assertEqual(state.phase, RoundPhase.COMPLETE)
        self.assertTrue(state.dealer.hole_card_revealed)
        self.assertEqual(participant.hands[0].resolution.reason, ResolutionReason.BLACKJACK)
        self.assertEqual(participant.bankroll_after_round, 100)
        self.assertEqual(state.action_count, 2)

    def test_insurance_remains_a_single_side_bet_after_a_split(self) -> None:
        shoe = Shoe.from_cards(
            (
                make_card(CardRank.TEN, CardSuit.SPADES),
                make_card(CardRank.ACE, CardSuit.DIAMONDS),
                make_card(CardRank.TEN, CardSuit.HEARTS),
                make_card(CardRank.KING, CardSuit.CLUBS),
                make_card(CardRank.EIGHT, CardSuit.SPADES),
                make_card(CardRank.EIGHT, CardSuit.HEARTS),
            )
        )
        start = self.service.start_round(
            round_id="round-insurance-split",
            table_id="table-1",
            players=(
                RoundPlayerInput(
                    player_id="alice",
                    display_name="Alice",
                    seat_number=1,
                    bankroll=100,
                    wager=Bet(amount=20),
                ),
            ),
            shoe=shoe,
        )

        opening_hand = start.round_state.participants[0].hands[0]
        self.assertIn(ActionType.INSURANCE, start.round_state.turn.legal_actions)
        self.assertIn(ActionType.SPLIT, start.round_state.turn.legal_actions)

        after_insurance = self.service.apply_action(
            round_state=start.round_state,
            shoe=start.shoe,
            player_id="alice",
            hand_id=opening_hand.hand_id,
            action=ActionType.INSURANCE,
        )
        self.assertIn(ActionType.SPLIT, after_insurance.round_state.turn.legal_actions)

        after_split = self.service.apply_action(
            round_state=after_insurance.round_state,
            shoe=after_insurance.shoe,
            player_id="alice",
            hand_id=opening_hand.hand_id,
            action=ActionType.SPLIT,
        )

        participant = after_split.round_state.participants[0]
        self.assertEqual(len(participant.hands), 2)
        self.assertEqual(sum(1 for hand in participant.hands if hand.insurance_wager is not None), 1)
        self.assertEqual(participant.total_committed, 50)
        self.assertEqual(participant.available_bankroll, 50)

        first_hand_id = participant.hands[0].hand_id
        second_hand_id = participant.hands[1].hand_id
        after_first_stand = self.service.apply_action(
            round_state=after_split.round_state,
            shoe=after_split.shoe,
            player_id="alice",
            hand_id=first_hand_id,
            action=ActionType.STAND,
        )
        complete = self.service.apply_action(
            round_state=after_first_stand.round_state,
            shoe=after_first_stand.shoe,
            player_id="alice",
            hand_id=second_hand_id,
            action=ActionType.STAND,
        )

        state = complete.round_state
        participant = state.participants[0]

        self.assertEqual(state.phase, RoundPhase.COMPLETE)
        self.assertEqual(participant.bankroll_after_round, 80)
        self.assertEqual(
            [hand.resolution.reason for hand in participant.hands],
            [ResolutionReason.BLACKJACK, ResolutionReason.BLACKJACK],
        )

    def test_split_twenty_one_advances_to_next_hand_and_rejects_resolved_hand_actions(self) -> None:
        shoe = Shoe.from_cards(
            (
                make_card(CardRank.ACE, CardSuit.SPADES),
                make_card(CardRank.SIX, CardSuit.CLUBS),
                make_card(CardRank.ACE, CardSuit.HEARTS),
                make_card(CardRank.NINE, CardSuit.DIAMONDS),
                make_card(CardRank.KING, CardSuit.SPADES),
                make_card(CardRank.FIVE, CardSuit.HEARTS),
            )
        )
        start = self.service.start_round(
            round_id="round-3b",
            table_id="table-1",
            players=(
                RoundPlayerInput(
                    player_id="alice",
                    display_name="Alice",
                    seat_number=1,
                    bankroll=100,
                    wager=Bet(amount=20),
                ),
            ),
            shoe=shoe,
        )
        opening_hand = start.round_state.participants[0].hands[0]

        after_split = self.service.apply_action(
            round_state=start.round_state,
            shoe=start.shoe,
            player_id="alice",
            hand_id=opening_hand.hand_id,
            action=ActionType.SPLIT,
        )
        first_hand, second_hand = after_split.round_state.participants[0].hands

        self.assertEqual(first_hand.status, HandStatus.STANDING)
        self.assertEqual(first_hand.value.best_total, 21)
        self.assertFalse(first_hand.value.is_blackjack)
        self.assertEqual(after_split.round_state.turn.hand_id, second_hand.hand_id)

        with self.assertRaises(IllegalActionError):
            self.service.apply_action(
                round_state=after_split.round_state,
                shoe=after_split.shoe,
                player_id="alice",
                hand_id=first_hand.hand_id,
                action=ActionType.STAND,
            )

    def test_double_bust_resolves_with_player_bust_without_dealer_draw(self) -> None:
        shoe = Shoe.from_cards(
            (
                make_card(CardRank.TEN, CardSuit.SPADES),
                make_card(CardRank.SIX, CardSuit.CLUBS),
                make_card(CardRank.SIX, CardSuit.HEARTS),
                make_card(CardRank.TEN, CardSuit.DIAMONDS),
                make_card(CardRank.EIGHT, CardSuit.SPADES),
            )
        )
        start = self.service.start_round(
            round_id="round-3c",
            table_id="table-1",
            players=(
                RoundPlayerInput(
                    player_id="alice",
                    display_name="Alice",
                    seat_number=1,
                    bankroll=100,
                    wager=Bet(amount=20),
                ),
            ),
            shoe=shoe,
        )
        hand = start.round_state.participants[0].hands[0]

        complete = self.service.apply_action(
            round_state=start.round_state,
            shoe=start.shoe,
            player_id="alice",
            hand_id=hand.hand_id,
            action=ActionType.DOUBLE,
        )

        state = complete.round_state
        busted_hand = state.participants[0].hands[0]

        self.assertEqual(state.phase, RoundPhase.COMPLETE)
        self.assertEqual(busted_hand.status, HandStatus.BUSTED)
        self.assertTrue(busted_hand.doubled_down)
        self.assertEqual(busted_hand.wager.amount, 40)
        self.assertTrue(busted_hand.value.is_bust)
        self.assertEqual(busted_hand.resolution.outcome, HandOutcome.LOSS)
        self.assertEqual(busted_hand.resolution.reason, ResolutionReason.PLAYER_BUST)
        self.assertEqual(busted_hand.resolution.net_change, -40)
        self.assertEqual(state.participants[0].bankroll_after_round, 60)
        self.assertEqual(state.dealer.hand.status, HandStatus.STANDING)
        self.assertTrue(state.dealer.hole_card_revealed)
        self.assertEqual(state.action_count, 1)

    def test_split_double_and_dealer_resolution_complete_the_round(self) -> None:
        shoe = Shoe.from_cards(
            (
                make_card(CardRank.EIGHT, CardSuit.SPADES),
                make_card(CardRank.SIX, CardSuit.CLUBS),
                make_card(CardRank.EIGHT, CardSuit.HEARTS),
                make_card(CardRank.TEN, CardSuit.DIAMONDS),
                make_card(CardRank.THREE, CardSuit.SPADES),
                make_card(CardRank.TEN, CardSuit.CLUBS),
                make_card(CardRank.KING, CardSuit.SPADES),
                make_card(CardRank.TWO, CardSuit.HEARTS),
            )
        )
        start = self.service.start_round(
            round_id="round-4",
            table_id="table-1",
            players=(
                RoundPlayerInput(
                    player_id="alice",
                    display_name="Alice",
                    seat_number=1,
                    bankroll=100,
                    wager=Bet(amount=20),
                ),
            ),
            shoe=shoe,
        )

        original_hand = start.round_state.participants[0].hands[0]
        after_split = self.service.apply_action(
            round_state=start.round_state,
            shoe=start.shoe,
            player_id="alice",
            hand_id=original_hand.hand_id,
            action=ActionType.SPLIT,
        )
        split_hands = after_split.round_state.participants[0].hands
        first_hand, second_hand = split_hands

        self.assertEqual(first_hand.parent_hand_id, original_hand.hand_id)
        self.assertEqual(second_hand.parent_hand_id, original_hand.hand_id)
        self.assertEqual(first_hand.split_depth, 1)
        self.assertEqual(second_hand.split_depth, 1)
        self.assertEqual(after_split.round_state.turn.hand_id, first_hand.hand_id)

        after_double = self.service.apply_action(
            round_state=after_split.round_state,
            shoe=after_split.shoe,
            player_id="alice",
            hand_id=first_hand.hand_id,
            action=ActionType.DOUBLE,
        )
        doubled_hand = after_double.round_state.participants[0].hands[0]

        self.assertEqual(doubled_hand.wager.amount, 40)
        self.assertTrue(doubled_hand.doubled_down)
        self.assertEqual(doubled_hand.status, HandStatus.STANDING)
        self.assertEqual(after_double.round_state.turn.hand_id, second_hand.hand_id)

        complete = self.service.apply_action(
            round_state=after_double.round_state,
            shoe=after_double.shoe,
            player_id="alice",
            hand_id=second_hand.hand_id,
            action=ActionType.STAND,
        )

        state = complete.round_state
        participant = state.participants[0]
        winning_hand, push_hand = participant.hands

        self.assertEqual(state.phase, RoundPhase.COMPLETE)
        self.assertTrue(state.dealer.hole_card_revealed)
        self.assertEqual(state.dealer.hand.status, HandStatus.STANDING)
        self.assertEqual(state.dealer.hand.value.best_total, 18)
        self.assertEqual(participant.bankroll_after_round, 140)
        self.assertEqual(winning_hand.resolution.outcome, HandOutcome.WIN)
        self.assertEqual(winning_hand.resolution.reason, ResolutionReason.HIGHER_TOTAL)
        self.assertEqual(winning_hand.resolution.net_change, 40)
        self.assertEqual(push_hand.resolution.outcome, HandOutcome.PUSH)
        self.assertEqual(push_hand.resolution.reason, ResolutionReason.EQUAL_TOTAL)
        self.assertEqual(push_hand.resolution.net_change, 0)
        self.assertEqual(state.action_count, 4)
        self.assertEqual(complete.shoe.remaining, 0)

    def test_dealer_blackjack_completes_the_round_immediately(self) -> None:
        shoe = Shoe.from_cards(
            (
                make_card(CardRank.TEN, CardSuit.SPADES),
                make_card(CardRank.KING, CardSuit.CLUBS),
                make_card(CardRank.NINE, CardSuit.HEARTS),
                make_card(CardRank.ACE, CardSuit.DIAMONDS),
            )
        )
        update = self.service.start_round(
            round_id="round-5",
            table_id="table-1",
            players=(
                RoundPlayerInput(
                    player_id="alice",
                    display_name="Alice",
                    seat_number=1,
                    bankroll=100,
                    wager=Bet(amount=20),
                ),
            ),
            shoe=shoe,
        )

        state = update.round_state
        player_hand = state.participants[0].hands[0]

        self.assertEqual(state.phase, RoundPhase.COMPLETE)
        self.assertIsNone(state.turn)
        self.assertEqual(state.dealer.hand.status, HandStatus.BLACKJACK)
        self.assertTrue(state.dealer.hole_card_revealed)
        self.assertEqual(player_hand.resolution.outcome, HandOutcome.LOSS)
        self.assertEqual(player_hand.resolution.reason, ResolutionReason.BLACKJACK)
        self.assertEqual(state.participants[0].bankroll_after_round, 80)


if __name__ == "__main__":
    unittest.main()
