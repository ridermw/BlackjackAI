import unittest

from blackjack_ai.engine import (
    ActionType,
    Bet,
    Card,
    CardRank,
    CardSuit,
    DealerState,
    HandState,
    HandStatus,
    ParticipantType,
    RoundParticipantState,
    RoundPhase,
    RoundState,
    RuleConfig,
    TurnState,
)


def make_card(rank: CardRank, suit: CardSuit) -> Card:
    return Card(rank=rank, suit=suit)


class HandValueTests(unittest.TestCase):
    def test_soft_blackjack_is_reported(self) -> None:
        hand = HandState(
            hand_id="hand-1",
            player_id="player-1",
            seat_number=1,
            cards=(
                make_card(CardRank.ACE, CardSuit.SPADES),
                make_card(CardRank.KING, CardSuit.HEARTS),
            ),
            status=HandStatus.ACTIVE,
        )

        value = hand.value

        self.assertEqual(value.hard_total, 11)
        self.assertEqual(value.totals, (11, 21))
        self.assertEqual(value.best_total, 21)
        self.assertTrue(value.is_soft)
        self.assertTrue(value.is_blackjack)
        self.assertFalse(value.is_bust)

    def test_three_card_twenty_one_is_not_blackjack_and_only_allows_stand(self) -> None:
        hand = HandState(
            hand_id="hand-1",
            player_id="player-1",
            seat_number=1,
            cards=(
                make_card(CardRank.ACE, CardSuit.SPADES),
                make_card(CardRank.FIVE, CardSuit.HEARTS),
                make_card(CardRank.FIVE, CardSuit.CLUBS),
            ),
            wager=Bet(amount=25),
            status=HandStatus.ACTIVE,
        )

        value = hand.value

        self.assertEqual(value.best_total, 21)
        self.assertFalse(value.is_blackjack)
        self.assertEqual(
            hand.legal_actions(RuleConfig(), available_bankroll=100),
            (ActionType.STAND,),
        )


class LegalActionTests(unittest.TestCase):
    def test_opening_pair_can_hit_stand_double_split_and_surrender(self) -> None:
        rules = RuleConfig()
        hand = HandState(
            hand_id="hand-1",
            player_id="player-1",
            seat_number=1,
            cards=(
                make_card(CardRank.EIGHT, CardSuit.SPADES),
                make_card(CardRank.EIGHT, CardSuit.HEARTS),
            ),
            wager=Bet(amount=50),
            status=HandStatus.ACTIVE,
        )

        actions = hand.legal_actions(rules, available_bankroll=200)

        self.assertEqual(
            actions,
            (ActionType.HIT, ActionType.STAND, ActionType.DOUBLE, ActionType.SPLIT, ActionType.SURRENDER),
        )

    def test_split_hand_respects_double_after_split_rule(self) -> None:
        rules = RuleConfig(allow_double_after_split=False)
        hand = HandState(
            hand_id="hand-1",
            player_id="player-1",
            seat_number=1,
            cards=(
                make_card(CardRank.SIX, CardSuit.SPADES),
                make_card(CardRank.FIVE, CardSuit.HEARTS),
            ),
            wager=Bet(amount=40),
            status=HandStatus.ACTIVE,
            parent_hand_id="parent-hand",
            split_depth=1,
        )

        actions = hand.legal_actions(rules, available_bankroll=100)

        self.assertEqual(actions, (ActionType.HIT, ActionType.STAND))

    def test_bankroll_limits_double_and_split(self) -> None:
        rules = RuleConfig()
        hand = HandState(
            hand_id="hand-1",
            player_id="player-1",
            seat_number=1,
            cards=(
                make_card(CardRank.NINE, CardSuit.CLUBS),
                make_card(CardRank.NINE, CardSuit.DIAMONDS),
            ),
            wager=Bet(amount=100),
            status=HandStatus.ACTIVE,
        )

        actions = hand.legal_actions(rules, available_bankroll=50)

        self.assertEqual(actions, (ActionType.HIT, ActionType.STAND, ActionType.SURRENDER))

    def test_value_match_rule_allows_splitting_mixed_ten_value_cards(self) -> None:
        hand = HandState(
            hand_id="hand-1",
            player_id="player-1",
            seat_number=1,
            cards=(
                make_card(CardRank.TEN, CardSuit.SPADES),
                make_card(CardRank.KING, CardSuit.HEARTS),
            ),
            wager=Bet(amount=20),
            status=HandStatus.ACTIVE,
        )

        self.assertEqual(
            hand.legal_actions(RuleConfig(), available_bankroll=100),
            (ActionType.HIT, ActionType.STAND, ActionType.DOUBLE, ActionType.SURRENDER),
        )
        self.assertEqual(
            hand.legal_actions(RuleConfig(split_on_value_match=True), available_bankroll=100),
            (ActionType.HIT, ActionType.STAND, ActionType.DOUBLE, ActionType.SPLIT, ActionType.SURRENDER),
        )

    def test_split_depth_limit_removes_split_action_but_keeps_other_opening_actions(self) -> None:
        hand = HandState(
            hand_id="hand-1",
            player_id="player-1",
            seat_number=1,
            cards=(
                make_card(CardRank.NINE, CardSuit.CLUBS),
                make_card(CardRank.NINE, CardSuit.DIAMONDS),
            ),
            wager=Bet(amount=20),
            status=HandStatus.ACTIVE,
            parent_hand_id="parent-hand",
            split_depth=1,
        )

        actions = hand.legal_actions(RuleConfig(maximum_split_depth=1), available_bankroll=100)

        self.assertEqual(actions, (ActionType.HIT, ActionType.STAND, ActionType.DOUBLE))

    def test_split_hands_do_not_offer_surrender(self) -> None:
        hand = HandState(
            hand_id="hand-1",
            player_id="player-1",
            seat_number=1,
            cards=(
                make_card(CardRank.SIX, CardSuit.CLUBS),
                make_card(CardRank.FIVE, CardSuit.DIAMONDS),
            ),
            wager=Bet(amount=20),
            status=HandStatus.ACTIVE,
            split_depth=1,
            parent_hand_id="parent-hand",
        )

        actions = hand.legal_actions(RuleConfig(), available_bankroll=100)

        self.assertEqual(actions, (ActionType.HIT, ActionType.STAND, ActionType.DOUBLE))

    def test_three_card_hand_cannot_surrender(self) -> None:
        hand = HandState(
            hand_id="hand-1",
            player_id="player-1",
            seat_number=1,
            cards=(
                make_card(CardRank.TEN, CardSuit.CLUBS),
                make_card(CardRank.THREE, CardSuit.DIAMONDS),
                make_card(CardRank.THREE, CardSuit.SPADES),
            ),
            wager=Bet(amount=20),
            status=HandStatus.ACTIVE,
        )

        self.assertEqual(
            hand.legal_actions(RuleConfig(), available_bankroll=100),
            (ActionType.HIT, ActionType.STAND),
        )

    def test_insurance_is_only_offered_with_ace_up_card_on_opening_hand(self) -> None:
        hand = HandState(
            hand_id="hand-1",
            player_id="player-1",
            seat_number=1,
            cards=(
                make_card(CardRank.TEN, CardSuit.CLUBS),
                make_card(CardRank.THREE, CardSuit.DIAMONDS),
            ),
            wager=Bet(amount=20),
            status=HandStatus.ACTIVE,
        )

        self.assertEqual(
            hand.legal_actions(
                RuleConfig(),
                available_bankroll=100,
                dealer_up_card=make_card(CardRank.ACE, CardSuit.SPADES),
            ),
            (ActionType.HIT, ActionType.STAND, ActionType.DOUBLE, ActionType.SURRENDER, ActionType.INSURANCE),
        )
        self.assertEqual(
            hand.legal_actions(
                RuleConfig(),
                available_bankroll=100,
                dealer_up_card=make_card(CardRank.KING, CardSuit.SPADES),
            ),
            (ActionType.HIT, ActionType.STAND, ActionType.DOUBLE, ActionType.SURRENDER),
        )

    def test_taken_insurance_removes_repeat_offer_and_can_limit_double(self) -> None:
        hand = HandState(
            hand_id="hand-1",
            player_id="player-1",
            seat_number=1,
            cards=(
                make_card(CardRank.TEN, CardSuit.CLUBS),
                make_card(CardRank.THREE, CardSuit.DIAMONDS),
            ),
            wager=Bet(amount=20),
            insurance_wager=Bet(amount=10),
            status=HandStatus.ACTIVE,
        )

        self.assertEqual(
            hand.legal_actions(
                RuleConfig(),
                available_bankroll=0,
                dealer_up_card=make_card(CardRank.ACE, CardSuit.SPADES),
            ),
            (ActionType.HIT, ActionType.STAND, ActionType.SURRENDER),
        )


class DealerStateTests(unittest.TestCase):
    def test_public_round_state_hides_hole_card_until_settlement(self) -> None:
        rules = RuleConfig()
        player_hand = HandState(
            hand_id="hand-1",
            player_id="player-1",
            seat_number=1,
            cards=(
                make_card(CardRank.TEN, CardSuit.SPADES),
                make_card(CardRank.SEVEN, CardSuit.HEARTS),
            ),
            wager=Bet(amount=25),
            status=HandStatus.ACTIVE,
        )
        participant = RoundParticipantState(
            player_id="player-1",
            display_name="Alice",
            participant_type=ParticipantType.HUMAN,
            seat_number=1,
            bankroll_before_round=500,
            hands=(player_hand,),
        )
        dealer = DealerState(
            hand=HandState(
                hand_id="dealer-hand",
                player_id="dealer",
                seat_number=0,
                cards=(
                    make_card(CardRank.TEN, CardSuit.CLUBS),
                    make_card(CardRank.SIX, CardSuit.SPADES),
                ),
                status=HandStatus.ACTIVE,
            ),
            hole_card_revealed=False,
        )
        round_state = RoundState(
            round_id="round-1",
            table_id="table-1",
            phase=RoundPhase.PLAYER_TURNS,
            rules=rules,
            participants=(participant,),
            dealer=dealer,
            turn=TurnState.for_player_hand(participant, player_hand, rules),
        )

        public_state = round_state.to_public_dict()
        dealer_state = public_state["dealer"]

        self.assertEqual(dealer_state["cards"][0]["rank"], "10")
        self.assertEqual(dealer_state["cards"][1], {"is_hidden": True})
        self.assertEqual(dealer_state["value"]["best_total"], 10)
        self.assertFalse(dealer_state["hole_card_revealed"])

        settled_state = RoundState(
            round_id=round_state.round_id,
            table_id=round_state.table_id,
            phase=RoundPhase.SETTLEMENT,
            rules=round_state.rules,
            participants=round_state.participants,
            dealer=round_state.dealer,
            turn=round_state.turn,
        ).to_public_dict()

        self.assertTrue(settled_state["dealer"]["hole_card_revealed"])
        self.assertEqual(settled_state["dealer"]["value"]["best_total"], 16)

    def test_dealer_soft_seventeen_rule_is_explicit(self) -> None:
        dealer = DealerState(
            hand=HandState(
                hand_id="dealer-hand",
                player_id="dealer",
                seat_number=0,
                cards=(
                    make_card(CardRank.ACE, CardSuit.CLUBS),
                    make_card(CardRank.SIX, CardSuit.SPADES),
                ),
                status=HandStatus.ACTIVE,
            ),
        )

        self.assertFalse(dealer.should_hit(RuleConfig(dealer_stands_on_soft_17=True)))
        self.assertTrue(dealer.should_hit(RuleConfig(dealer_stands_on_soft_17=False)))


if __name__ == "__main__":
    unittest.main()
