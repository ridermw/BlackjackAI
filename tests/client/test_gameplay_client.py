from __future__ import annotations

from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from blackjack_ai.api.app import create_app
from blackjack_ai.api.service import GameService
from blackjack_ai.benchmark.harness import local_api_client
from blackjack_ai.client import GameplayClient
from blackjack_ai.client import GameplayError
from blackjack_ai.config import Settings
from blackjack_ai.engine import Card
from blackjack_ai.engine import CardRank
from blackjack_ai.engine import CardSuit


def make_card(rank: CardRank, suit: CardSuit) -> Card:
    return Card(rank=rank, suit=suit)


def test_gameplay_client_auto_uses_stored_token_and_accepts_override() -> None:
    game_service = GameService()

    with local_api_client(game_service=game_service) as api_client:
        creator = GameplayClient(api_client)
        alice = creator.create_player("Alice", player_id="alice", participant_type="human", starting_bankroll=200)
        bob = creator.create_player("Bob", player_id="bob", participant_type="human", starting_bankroll=150)

        creator.create_table(table_id="table-auto-token", seat_count=1)
        seated_alice = creator.seat_player("table-auto-token", 1, "alice")
        assert seated_alice["seats"][0]["occupant"]["player_id"] == "alice"
        left_alice = creator.leave_seat("table-auto-token", 1, "alice")
        assert left_alice["seats"][0]["status"] == "empty"
        assert left_alice["seats"][0]["occupant"] is None
        reseated_alice = creator.seat_player("table-auto-token", 1, "alice")
        assert reseated_alice["seats"][0]["occupant"]["player_id"] == "alice"

        other_client = GameplayClient(api_client)
        other_client.create_table(table_id="table-explicit-token", seat_count=1)
        with pytest.raises(GameplayError) as exc_info:
            other_client.seat_player("table-explicit-token", 1, "bob")
        assert exc_info.value.status_code == 401

        seated_bob = other_client.seat_player(
            "table-explicit-token",
            1,
            "bob",
            player_token=bob["player_token"],
        )
        assert seated_bob["seats"][0]["occupant"]["player_id"] == "bob"
        left_bob = other_client.leave_seat(
            "table-explicit-token",
            1,
            "bob",
            player_token=bob["player_token"],
        )
        assert left_bob["seats"][0]["status"] == "empty"
        assert alice["player_token"] != bob["player_token"]


def test_gameplay_client_leave_seat_allows_reseating_by_another_player() -> None:
    game_service = GameService()

    with local_api_client(game_service=game_service) as api_client:
        client = GameplayClient(api_client)
        client.create_player("Alice", player_id="alice", participant_type="human", starting_bankroll=200)
        client.create_player("Bob", player_id="bob", participant_type="human", starting_bankroll=150)
        client.create_table(table_id="table-leave", seat_count=1)
        client.seat_player("table-leave", 1, "alice")

        left = client.leave_seat("table-leave", 1, "alice")
        assert left["seats"][0]["status"] == "empty"
        assert left["seats"][0]["occupant"] is None

        seated_bob = client.seat_player("table-leave", 1, "bob")
        assert seated_bob["seats"][0]["occupant"]["player_id"] == "bob"


def test_gameplay_client_drives_multiplayer_round_from_local_api_client() -> None:
    game_service = GameService()
    game_service.queue_test_shoe(
        [
            make_card(CardRank.TEN, CardSuit.SPADES),
            make_card(CardRank.NINE, CardSuit.CLUBS),
            make_card(CardRank.SIX, CardSuit.DIAMONDS),
            make_card(CardRank.EIGHT, CardSuit.HEARTS),
            make_card(CardRank.SEVEN, CardSuit.SPADES),
            make_card(CardRank.TEN, CardSuit.CLUBS),
            make_card(CardRank.EIGHT, CardSuit.DIAMONDS),
        ]
    )

    with local_api_client(game_service=game_service) as api_client:
        client = GameplayClient(api_client)
        client.create_player(
            "Alice",
            player_id="alice",
            participant_type="human",
            starting_bankroll=200,
        )
        client.create_player(
            "Bot One",
            player_id="bot-1",
            participant_type="ai",
            starting_bankroll=300,
            metadata={"strategy": "baseline"},
        )
        client.create_table(table_id="table-main", seat_count=2)
        client.seat_player("table-main", 1, "alice")
        client.seat_player("table-main", 2, "bot-1")

        round_session = client.start_round("table-main", round_id="round-1")
        assert round_session.state["next_request"]["pending_player_ids"] == ["alice", "bot-1"]

        round_session.bet(player_id="alice", amount=20)
        assert round_session.state["betting"]["pending_player_ids"] == ["bot-1"]

        round_session.bet(player_id="bot-1", amount=30)
        assert round_session.current_player_id == "alice"
        assert round_session.current_hand_id is not None

        round_session.stand()
        assert round_session.current_player_id == "bot-1"

        round_session.stand()
        assert round_session.state["phase"] == "complete"
        assert round_session.state["dealer"]["hole_card_revealed"] is True

        table_state = client.get_table("table-main")
        assert table_state["active_round_id"] is None
        assert table_state["seats"][0]["bankroll"] == 220
        assert table_state["seats"][1]["bankroll"] == 330

        leaderboard = client.get_leaderboard()
        assert [entry["player_id"] for entry in leaderboard["entries"]] == ["bot-1", "alice"]


def test_round_session_supports_retrying_bets_and_actions_with_request_ids() -> None:
    game_service = GameService()
    game_service.queue_test_shoe(
        [
            make_card(CardRank.TEN, CardSuit.SPADES),
            make_card(CardRank.NINE, CardSuit.CLUBS),
            make_card(CardRank.SIX, CardSuit.DIAMONDS),
            make_card(CardRank.EIGHT, CardSuit.HEARTS),
            make_card(CardRank.SEVEN, CardSuit.SPADES),
            make_card(CardRank.TEN, CardSuit.CLUBS),
            make_card(CardRank.EIGHT, CardSuit.DIAMONDS),
        ]
    )

    with local_api_client(game_service=game_service) as api_client:
        client = GameplayClient(api_client)
        client.create_player("Alice", player_id="alice", participant_type="human", starting_bankroll=200)
        client.create_player("Bot One", player_id="bot-1", participant_type="ai", starting_bankroll=300)
        client.create_table(table_id="table-idempotent", seat_count=2)
        client.seat_player("table-idempotent", 1, "alice")
        client.seat_player("table-idempotent", 2, "bot-1")

        round_session = client.start_round("table-idempotent", round_id="round-idempotent")
        assert round_session.version == 0

        round_session.bet(player_id="alice", amount=20, request_id="alice-bet-1", expected_version=0)
        first_bet_state = deepcopy(round_session.state)
        assert round_session.version == 1

        round_session.bet(player_id="bot-1", amount=30, request_id="bot-bet-1", expected_version=1)
        replayed_bet = client.bet(
            "round-idempotent",
            player_id="alice",
            amount=20,
            request_id="alice-bet-1",
            expected_version=999,
        )
        assert replayed_bet.state == first_bet_state
        assert replayed_bet.version == 1

        alice_hand_id = round_session.current_hand_id
        assert alice_hand_id is not None

        action_version = round_session.version
        round_session.stand(request_id="alice-stand-1", expected_version=action_version)
        first_action_state = deepcopy(round_session.state)
        assert round_session.version == action_version + 1

        round_session.stand(request_id="bot-stand-1", expected_version=round_session.version)
        replayed_action = client.action(
            "round-idempotent",
            player_id="alice",
            hand_id=alice_hand_id,
            action="stand",
            request_id="alice-stand-1",
            expected_version=999,
        )
        assert replayed_action.state == first_action_state
        assert replayed_action.version == action_version + 1


def test_round_session_events_support_sequence_cursor_polling() -> None:
    game_service = GameService()
    game_service.queue_test_shoe(
        [
            make_card(CardRank.TEN, CardSuit.SPADES),
            make_card(CardRank.SIX, CardSuit.CLUBS),
            make_card(CardRank.SEVEN, CardSuit.DIAMONDS),
            make_card(CardRank.TEN, CardSuit.HEARTS),
            make_card(CardRank.EIGHT, CardSuit.CLUBS),
        ]
    )

    with local_api_client(game_service=game_service) as api_client:
        client = GameplayClient(api_client)
        client.create_player("Alice", player_id="alice", participant_type="human", starting_bankroll=200)
        client.create_table(table_id="table-events", seat_count=1)
        client.seat_player("table-events", 1, "alice")

        round_session = client.start_round("table-events", round_id="round-events")
        round_session.bet(amount=20)

        initial_events = round_session.events()
        assert [event["event_type"] for event in initial_events["events"]] == [
            "round_started",
            "bet_placed",
            "initial_cards_dealt",
        ]
        assert initial_events["after_sequence"] == 0
        assert initial_events["last_sequence"] == 3
        assert initial_events["next_after_sequence"] == 3
        assert initial_events["has_more"] is False

        tail_events = round_session.events(limit=2)
        assert [event["sequence"] for event in tail_events["events"]] == [2, 3]
        assert tail_events["next_after_sequence"] == 3

        round_session.stand()

        incremental_events = round_session.events(after_sequence=initial_events["next_after_sequence"], limit=1)
        assert [event["sequence"] for event in incremental_events["events"]] == [4]
        assert [event["event_type"] for event in incremental_events["events"]] == ["player_action"]
        assert incremental_events["after_sequence"] == 3
        assert incremental_events["last_sequence"] == 7
        assert incremental_events["next_after_sequence"] == 4
        assert incremental_events["has_more"] is True

        remaining_events = round_session.events(after_sequence=incremental_events["next_after_sequence"])
        assert [event["event_type"] for event in remaining_events["events"]] == [
            "dealer_revealed",
            "dealer_hit",
            "round_settled",
        ]
        assert remaining_events["after_sequence"] == 4
        assert remaining_events["last_sequence"] == 7
        assert remaining_events["next_after_sequence"] == 7
        assert remaining_events["has_more"] is False


def test_gameplay_client_supports_split_and_double_with_testclient_transport() -> None:
    game_service = GameService()
    game_service.queue_test_shoe(
        [
            make_card(CardRank.EIGHT, CardSuit.SPADES),
            make_card(CardRank.SIX, CardSuit.CLUBS),
            make_card(CardRank.EIGHT, CardSuit.HEARTS),
            make_card(CardRank.TEN, CardSuit.DIAMONDS),
            make_card(CardRank.THREE, CardSuit.SPADES),
            make_card(CardRank.TEN, CardSuit.CLUBS),
            make_card(CardRank.KING, CardSuit.SPADES),
            make_card(CardRank.TWO, CardSuit.HEARTS),
        ]
    )
    app = create_app(
        Settings(
            environment="test",
            database_url="sqlite:///:memory:",
        ),
        game_service=game_service,
    )

    with TestClient(app, base_url="http://gameplay.local") as transport:
        client = GameplayClient.from_transport(transport)
        client.create_player(
            "Alice",
            player_id="alice",
            participant_type="human",
            starting_bankroll=200,
        )
        client.create_table(table_id="table-split", seat_count=1)
        client.seat_player("table-split", 1, "alice")

        round_session = client.start_round("table-split", round_id="round-split")
        round_session.bet(player_id="alice", amount=20)
        opening_hand_id = round_session.current_hand_id

        round_session.split()
        hands_after_split = round_session.participant("alice")["hands"]
        assert len(hands_after_split) == 2
        assert round_session.current_player_id == "alice"
        assert round_session.current_hand_id == hands_after_split[0]["hand_id"]
        assert opening_hand_id != hands_after_split[0]["hand_id"]

        round_session.double()
        hands_after_double = round_session.participant("alice")["hands"]
        assert hands_after_double[0]["doubled_down"] is True
        assert hands_after_double[0]["wager"]["amount"] == 40
        assert round_session.current_hand_id == hands_after_double[1]["hand_id"]

        round_session.stand()
        assert round_session.state["phase"] == "complete"
        assert round_session.state["dealer"]["hole_card_revealed"] is True

        restored = client.get_round("round-split")
        assert restored.state["phase"] == "complete"


def test_round_session_supports_surrender_via_high_level_method() -> None:
    game_service = GameService()
    game_service.queue_test_shoe(
        [
            make_card(CardRank.TEN, CardSuit.SPADES),
            make_card(CardRank.NINE, CardSuit.CLUBS),
            make_card(CardRank.SIX, CardSuit.DIAMONDS),
            make_card(CardRank.SEVEN, CardSuit.HEARTS),
            make_card(CardRank.SEVEN, CardSuit.SPADES),
            make_card(CardRank.TEN, CardSuit.CLUBS),
            make_card(CardRank.EIGHT, CardSuit.DIAMONDS),
        ]
    )

    with local_api_client(game_service=game_service) as api_client:
        client = GameplayClient(api_client)
        client.create_player("Alice", player_id="alice", participant_type="human", starting_bankroll=200)
        client.create_player("Bot One", player_id="bot-1", participant_type="ai", starting_bankroll=300)
        client.create_table(table_id="table-surrender", seat_count=2)
        client.seat_player("table-surrender", 1, "alice")
        client.seat_player("table-surrender", 2, "bot-1")

        round_session = client.start_round("table-surrender", round_id="round-surrender")
        round_session.bet(player_id="alice", amount=20)
        round_session.bet(player_id="bot-1", amount=30)

        assert "surrender" in round_session.legal_actions
        round_session.surrender()

        alice_hand = round_session.participant("alice")["hands"][0]
        assert alice_hand["resolution"]["reason"] == "surrender"
        assert alice_hand["resolution"]["net_change"] == -10
        assert round_session.current_player_id == "bot-1"
        assert round_session.state["dealer"]["cards"][1] == {"is_hidden": True}

        round_session.stand()
        assert round_session.state["phase"] == "complete"


def test_round_session_supports_insure_method() -> None:
    game_service = GameService()
    game_service.queue_test_shoe(
        [
            make_card(CardRank.TEN, CardSuit.SPADES),
            make_card(CardRank.NINE, CardSuit.CLUBS),
            make_card(CardRank.ACE, CardSuit.DIAMONDS),
            make_card(CardRank.SEVEN, CardSuit.HEARTS),
            make_card(CardRank.EIGHT, CardSuit.SPADES),
            make_card(CardRank.NINE, CardSuit.CLUBS),
        ]
    )

    with local_api_client(game_service=game_service) as api_client:
        client = GameplayClient(api_client)
        client.create_player("Alice", player_id="alice", participant_type="human", starting_bankroll=30)
        client.create_player("Bot One", player_id="bot-1", participant_type="ai", starting_bankroll=100)
        client.create_table(table_id="table-insurance", seat_count=2)
        client.seat_player("table-insurance", 1, "alice")
        client.seat_player("table-insurance", 2, "bot-1")

        round_session = client.start_round("table-insurance", round_id="round-insurance")
        round_session.bet(player_id="alice", amount=20)
        round_session.bet(player_id="bot-1", amount=20)

        assert round_session.state["phase"] == "player_turns"
        assert round_session.state["next_request"]["player_id"] == "alice"
        assert "insurance" in round_session.legal_actions

        round_session.insure()
        assert round_session.state["phase"] == "player_turns"
        assert round_session.state["next_request"]["player_id"] == "alice"
        assert round_session.state["dealer"]["cards"][1] == {"is_hidden": True}
        assert "insurance" not in round_session.legal_actions
        assert "double" not in round_session.legal_actions
        assert "insurance" not in round_session.participant("alice")
        assert round_session.state["dealer"]["cards"][1] == {"is_hidden": True}

        round_session.stand()
        assert round_session.current_player_id == "bot-1"
        assert "insurance" in round_session.legal_actions
        round_session.stand()
        assert round_session.state["phase"] == "complete"
