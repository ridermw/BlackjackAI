from __future__ import annotations

import unittest
from random import Random

from fastapi.testclient import TestClient as FastApiTestClient

from blackjack_ai.api.app import create_app
from blackjack_ai.api.service import GameService
from blackjack_ai.config import Settings
from blackjack_ai.engine import Card
from blackjack_ai.engine import CardRank
from blackjack_ai.engine import CardSuit


def make_card(rank: CardRank, suit: CardSuit) -> Card:
    return Card(rank=rank, suit=suit)


class AuthenticatedTestClient:
    _known_player_tokens: dict[str, str] = {}

    def __init__(self, app, **kwargs) -> None:
        self._client = FastApiTestClient(app, **kwargs)
        self.player_tokens = self._known_player_tokens

    def __getattr__(self, name: str):
        return getattr(self._client, name)

    def __enter__(self) -> AuthenticatedTestClient:
        self._client.__enter__()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._client.__exit__(exc_type, exc, traceback)

    def post(
        self,
        url: str,
        *,
        auto_auth: bool = True,
        headers: dict[str, str] | None = None,
        **kwargs,
    ):
        resolved_headers = dict(headers or {})
        if auto_auth and "X-Player-Token" not in resolved_headers and url.endswith(("/join", "/leave", "/bets", "/actions")):
            payload = kwargs.get("json")
            if isinstance(payload, dict):
                player_id = payload.get("player_id")
                if isinstance(player_id, str):
                    player_token = self.player_tokens.get(player_id)
                    if player_token is not None:
                        resolved_headers["X-Player-Token"] = player_token

        response = self._client.post(url, headers=resolved_headers or None, **kwargs)
        if url == "/players" and response.status_code == 201:
            payload = response.json()
            player_id = payload.get("player_id")
            player_token = payload.get("player_token")
            if isinstance(player_id, str) and isinstance(player_token, str):
                self.player_tokens[player_id] = player_token
        return response


def TestClient(app, **kwargs) -> AuthenticatedTestClient:
    return AuthenticatedTestClient(app, **kwargs)


class RestApiContractTests(unittest.TestCase):
    def create_client(self, *, randomizer: Random | None = None) -> tuple[GameService, TestClient]:
        game_service = GameService(randomizer=randomizer)
        app = create_app(
            Settings(
                environment="test",
                database_url="sqlite:///:memory:",
            ),
            game_service=game_service,
        )
        return game_service, TestClient(app)

    def test_create_player_returns_token_but_public_reads_do_not_expose_it(self) -> None:
        _, client = self.create_client()

        with client:
            created = client.post(
                "/players",
                json={
                    "player_id": "alice",
                    "display_name": "Alice",
                    "participant_type": "human",
                    "starting_bankroll": 200,
                },
            )
            self.assertEqual(created.status_code, 201)
            player_token = created.json()["player_token"]
            self.assertEqual(client.player_tokens["alice"], player_token)

            read_player = client.get("/players/alice")
            self.assertEqual(read_player.status_code, 200)
            self.assertNotIn("player_token", read_player.json())
            self.assertNotIn(player_token, str(read_player.json()))

            self.assertEqual(client.post("/tables", json={"table_id": "table-auth", "seat_count": 1}).status_code, 201)
            self.assertEqual(client.post("/tables/table-auth/seats/1/join", json={"player_id": "alice"}).status_code, 200)
            self.assertEqual(client.post("/tables/table-auth/rounds", json={"round_id": "round-auth"}).status_code, 201)

            stats = client.get("/players/alice/stats")
            table = client.get("/tables/table-auth")
            round_snapshot = client.get("/rounds/round-auth")
            leaderboard = client.get("/leaderboard")
            for response in (stats, table, round_snapshot, leaderboard):
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("player_token", str(response.json()))
                self.assertNotIn(player_token, str(response.json()))

    def test_player_owned_mutations_require_valid_player_token(self) -> None:
        game_service, client = self.create_client()
        game_service.queue_test_shoe(
            [
                make_card(CardRank.TEN, CardSuit.SPADES),
                make_card(CardRank.SIX, CardSuit.CLUBS),
                make_card(CardRank.SEVEN, CardSuit.DIAMONDS),
                make_card(CardRank.TEN, CardSuit.HEARTS),
                make_card(CardRank.EIGHT, CardSuit.CLUBS),
            ]
        )

        with client:
            created = client.post(
                "/players",
                json={
                    "player_id": "alice",
                    "display_name": "Alice",
                    "participant_type": "human",
                    "starting_bankroll": 200,
                },
            )
            self.assertEqual(created.status_code, 201)
            player_token = created.json()["player_token"]

            self.assertEqual(client.post("/tables", json={"table_id": "table-auth", "seat_count": 1}).status_code, 201)

            missing_join = client.post(
                "/tables/table-auth/seats/1/join",
                json={"player_id": "alice"},
                auto_auth=False,
            )
            self.assertEqual(missing_join.status_code, 401)
            self.assertIn("X-Player-Token", missing_join.json()["detail"])

            wrong_join = client.post(
                "/tables/table-auth/seats/1/join",
                json={"player_id": "alice"},
                headers={"X-Player-Token": "wrong-token"},
                auto_auth=False,
            )
            self.assertEqual(wrong_join.status_code, 403)
            self.assertIn("player token", wrong_join.json()["detail"].lower())

            correct_join = client.post(
                "/tables/table-auth/seats/1/join",
                json={"player_id": "alice"},
                headers={"X-Player-Token": player_token},
                auto_auth=False,
            )
            self.assertEqual(correct_join.status_code, 200)
            self.assertEqual(client.post("/tables/table-auth/rounds", json={"round_id": "round-auth"}).status_code, 201)

            missing_bet = client.post(
                "/rounds/round-auth/bets",
                json={"player_id": "alice", "amount": 20},
                auto_auth=False,
            )
            self.assertEqual(missing_bet.status_code, 401)

            wrong_bet = client.post(
                "/rounds/round-auth/bets",
                json={"player_id": "alice", "amount": 20},
                headers={"X-Player-Token": "wrong-token"},
                auto_auth=False,
            )
            self.assertEqual(wrong_bet.status_code, 403)

            correct_bet = client.post(
                "/rounds/round-auth/bets",
                json={"player_id": "alice", "amount": 20},
                headers={"X-Player-Token": player_token},
                auto_auth=False,
            )
            self.assertEqual(correct_bet.status_code, 200)
            hand_id = correct_bet.json()["participants"][0]["hands"][0]["hand_id"]

            missing_action = client.post(
                "/rounds/round-auth/actions",
                json={"player_id": "alice", "hand_id": hand_id, "action": "stand"},
                auto_auth=False,
            )
            self.assertEqual(missing_action.status_code, 401)

            wrong_action = client.post(
                "/rounds/round-auth/actions",
                json={"player_id": "alice", "hand_id": hand_id, "action": "stand"},
                headers={"X-Player-Token": "wrong-token"},
                auto_auth=False,
            )
            self.assertEqual(wrong_action.status_code, 403)

            correct_action = client.post(
                "/rounds/round-auth/actions",
                json={"player_id": "alice", "hand_id": hand_id, "action": "stand"},
                headers={"X-Player-Token": player_token},
                auto_auth=False,
            )
            self.assertEqual(correct_action.status_code, 200)
            self.assertEqual(correct_action.json()["phase"], "complete")

    def test_leave_table_seat_requires_valid_player_token_and_returns_empty_seat(self) -> None:
        _, client = self.create_client()

        with client:
            created = client.post(
                "/players",
                json={
                    "player_id": "alice",
                    "display_name": "Alice",
                    "participant_type": "human",
                    "starting_bankroll": 200,
                },
            )
            self.assertEqual(created.status_code, 201)

            self.assertEqual(client.post("/tables", json={"table_id": "table-leave-auth", "seat_count": 1}).status_code, 201)
            self.assertEqual(client.post("/tables/table-leave-auth/seats/1/join", json={"player_id": "alice"}).status_code, 200)

            missing_leave = client.post(
                "/tables/table-leave-auth/seats/1/leave",
                json={"player_id": "alice"},
                auto_auth=False,
            )
            self.assertEqual(missing_leave.status_code, 401)
            self.assertIn("X-Player-Token", missing_leave.json()["detail"])

            wrong_leave = client.post(
                "/tables/table-leave-auth/seats/1/leave",
                json={"player_id": "alice"},
                headers={"X-Player-Token": "wrong-token"},
                auto_auth=False,
            )
            self.assertEqual(wrong_leave.status_code, 403)
            self.assertIn("player token", wrong_leave.json()["detail"].lower())

            left = client.post(
                "/tables/table-leave-auth/seats/1/leave",
                json={"player_id": "alice"},
            )
            self.assertEqual(left.status_code, 200)
            self.assertEqual(left.json()["seats"][0]["status"], "empty")
            self.assertIsNone(left.json()["seats"][0]["occupant"])

            table = client.get("/tables/table-leave-auth")
            self.assertEqual(table.status_code, 200)
            self.assertEqual(table.json()["seats"][0]["status"], "empty")
            self.assertIsNone(table.json()["seats"][0]["occupant"])

            repeat_leave = client.post(
                "/tables/table-leave-auth/seats/1/leave",
                json={"player_id": "alice"},
            )
            self.assertEqual(repeat_leave.status_code, 409)
            self.assertIn("empty", repeat_leave.json()["detail"].lower())

    def test_leave_table_seat_rejects_active_round(self) -> None:
        _, client = self.create_client()

        with client:
            self.assertEqual(
                client.post(
                    "/players",
                    json={
                        "player_id": "alice",
                        "display_name": "Alice",
                        "participant_type": "human",
                        "starting_bankroll": 200,
                    },
                ).status_code,
                201,
            )
            self.assertEqual(client.post("/tables", json={"table_id": "table-leave-active", "seat_count": 1}).status_code, 201)
            self.assertEqual(client.post("/tables/table-leave-active/seats/1/join", json={"player_id": "alice"}).status_code, 200)
            self.assertEqual(client.post("/tables/table-leave-active/rounds", json={"round_id": "round-leave-active"}).status_code, 201)

            leave = client.post(
                "/tables/table-leave-active/seats/1/leave",
                json={"player_id": "alice"},
            )
            self.assertEqual(leave.status_code, 409)
            self.assertIn("active", leave.json()["detail"].lower())

            table = client.get("/tables/table-leave-active")
            self.assertEqual(table.status_code, 200)
            self.assertEqual(table.json()["seats"][0]["occupant"]["player_id"], "alice")

    def test_leave_table_seat_rejects_different_player(self) -> None:
        _, client = self.create_client()

        with client:
            self.assertEqual(
                client.post(
                    "/players",
                    json={
                        "player_id": "alice",
                        "display_name": "Alice",
                        "participant_type": "human",
                        "starting_bankroll": 200,
                    },
                ).status_code,
                201,
            )
            self.assertEqual(
                client.post(
                    "/players",
                    json={
                        "player_id": "bob",
                        "display_name": "Bob",
                        "participant_type": "human",
                        "starting_bankroll": 150,
                    },
                ).status_code,
                201,
            )
            self.assertEqual(client.post("/tables", json={"table_id": "table-leave-owner", "seat_count": 1}).status_code, 201)
            self.assertEqual(client.post("/tables/table-leave-owner/seats/1/join", json={"player_id": "alice"}).status_code, 200)

            wrong_player_leave = client.post(
                "/tables/table-leave-owner/seats/1/leave",
                json={"player_id": "bob"},
            )
            self.assertEqual(wrong_player_leave.status_code, 403)
            self.assertIn("alice", wrong_player_leave.json()["detail"])

            table = client.get("/tables/table-leave-owner")
            self.assertEqual(table.status_code, 200)
            self.assertEqual(table.json()["seats"][0]["occupant"]["player_id"], "alice")

    def test_player_can_join_vacated_seat_after_leave(self) -> None:
        _, client = self.create_client()

        with client:
            self.assertEqual(
                client.post(
                    "/players",
                    json={
                        "player_id": "alice",
                        "display_name": "Alice",
                        "participant_type": "human",
                        "starting_bankroll": 200,
                    },
                ).status_code,
                201,
            )
            self.assertEqual(
                client.post(
                    "/players",
                    json={
                        "player_id": "bob",
                        "display_name": "Bob",
                        "participant_type": "human",
                        "starting_bankroll": 150,
                    },
                ).status_code,
                201,
            )
            self.assertEqual(client.post("/tables", json={"table_id": "table-leave-rejoin", "seat_count": 1}).status_code, 201)
            self.assertEqual(client.post("/tables/table-leave-rejoin/seats/1/join", json={"player_id": "alice"}).status_code, 200)

            leave = client.post(
                "/tables/table-leave-rejoin/seats/1/leave",
                json={"player_id": "alice"},
            )
            self.assertEqual(leave.status_code, 200)
            self.assertEqual(leave.json()["seats"][0]["status"], "empty")

            join_bob = client.post(
                "/tables/table-leave-rejoin/seats/1/join",
                json={"player_id": "bob"},
            )
            self.assertEqual(join_bob.status_code, 200)
            self.assertEqual(join_bob.json()["seats"][0]["occupant"]["player_id"], "bob")

    def test_multiplayer_round_flow_hides_hole_card_and_updates_leaderboard(self) -> None:
        game_service, client = self.create_client()
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

        with client:
            alice = client.post(
                "/players",
                json={
                    "player_id": "alice",
                    "display_name": "Alice",
                    "participant_type": "human",
                    "starting_bankroll": 200,
                },
            )
            self.assertEqual(alice.status_code, 201)

            bot = client.post(
                "/players",
                json={
                    "player_id": "bot-1",
                    "display_name": "Bot One",
                    "participant_type": "ai",
                    "starting_bankroll": 300,
                    "metadata": {"strategy": "baseline"},
                },
            )
            self.assertEqual(bot.status_code, 201)
            self.assertEqual(bot.json()["metadata"], {"strategy": "baseline"})

            table = client.post(
                "/tables",
                json={
                    "table_id": "table-main",
                    "seat_count": 2,
                },
            )
            self.assertEqual(table.status_code, 201)
            self.assertEqual(table.json()["status"], "open")

            join_alice = client.post(
                "/tables/table-main/seats/1/join",
                json={"player_id": "alice"},
            )
            self.assertEqual(join_alice.status_code, 200)

            join_bot = client.post(
                "/tables/table-main/seats/2/join",
                json={"player_id": "bot-1"},
            )
            self.assertEqual(join_bot.status_code, 200)

            player_response = client.get("/players/bot-1")
            self.assertEqual(player_response.status_code, 200)
            self.assertEqual(player_response.json()["participant_type"], "ai")

            table_state = client.get("/tables/table-main")
            self.assertEqual(table_state.status_code, 200)
            self.assertEqual(table_state.json()["seats"][0]["occupant"]["player_id"], "alice")
            self.assertEqual(table_state.json()["seats"][1]["occupant"]["player_id"], "bot-1")

            round_response = client.post(
                "/tables/table-main/rounds",
                json={"round_id": "round-1"},
            )
            self.assertEqual(round_response.status_code, 201)
            self.assertEqual(round_response.json()["phase"], "waiting_for_bets")
            self.assertEqual(round_response.json()["next_request"]["pending_player_ids"], ["alice", "bot-1"])

            alice_bet = client.post(
                "/rounds/round-1/bets",
                json={"player_id": "alice", "amount": 20},
            )
            self.assertEqual(alice_bet.status_code, 200)
            self.assertEqual(alice_bet.json()["betting"]["pending_player_ids"], ["bot-1"])

            bot_bet = client.post(
                "/rounds/round-1/bets",
                json={"player_id": "bot-1", "amount": 30},
            )
            self.assertEqual(bot_bet.status_code, 200)

            round_state = bot_bet.json()
            self.assertEqual(round_state["phase"], "player_turns")
            self.assertEqual(round_state["next_request"]["player_id"], "alice")
            self.assertEqual(round_state["dealer"]["cards"][1], {"is_hidden": True})
            self.assertCountEqual(round_state["next_request"]["legal_actions"], ["hit", "stand", "double", "surrender"])

            mid_round_events = client.get("/rounds/round-1/events")
            self.assertEqual(mid_round_events.status_code, 200)
            initial_deal_event = next(
                event
                for event in mid_round_events.json()["events"]
                if event["event_type"] == "initial_cards_dealt"
            )
            self.assertEqual(initial_deal_event["payload"]["round"]["dealer"]["cards"][1], {"is_hidden": True})

            alice_hand_id = round_state["participants"][0]["hands"][0]["hand_id"]
            bot_hand_id = round_state["participants"][1]["hands"][0]["hand_id"]

            alice_action = client.post(
                "/rounds/round-1/actions",
                json={"player_id": "alice", "hand_id": alice_hand_id, "action": "stand"},
            )
            self.assertEqual(alice_action.status_code, 200)
            self.assertEqual(alice_action.json()["next_request"]["player_id"], "bot-1")

            bot_action = client.post(
                "/rounds/round-1/actions",
                json={"player_id": "bot-1", "hand_id": bot_hand_id, "action": "stand"},
            )
            self.assertEqual(bot_action.status_code, 200)

            completed_round = bot_action.json()
            self.assertEqual(completed_round["phase"], "complete")
            self.assertEqual(completed_round["next_request"], {"type": "round_complete"})
            self.assertTrue(completed_round["dealer"]["hole_card_revealed"])
            self.assertTrue(completed_round["dealer"]["value"]["is_bust"])
            self.assertEqual(completed_round["dealer"]["value"]["hard_total"], 24)

            round_snapshot = client.get("/rounds/round-1")
            self.assertEqual(round_snapshot.status_code, 200)
            self.assertEqual(round_snapshot.json()["phase"], "complete")

            player_stats = client.get("/players/alice/stats")
            self.assertEqual(player_stats.status_code, 200)
            self.assertEqual(player_stats.json()["stats"]["wins"], 1)

            final_table = client.get("/tables/table-main")
            self.assertEqual(final_table.status_code, 200)
            self.assertIsNone(final_table.json()["active_round_id"])
            self.assertEqual(final_table.json()["seats"][0]["bankroll"], 220)
            self.assertEqual(final_table.json()["seats"][1]["bankroll"], 330)

            leaderboard = client.get("/leaderboard")
            self.assertEqual(leaderboard.status_code, 200)
            entries = leaderboard.json()["entries"]
            self.assertEqual(entries[0]["player_id"], "bot-1")
            self.assertEqual(entries[0]["stats"]["bankroll_delta"], 30)
            self.assertEqual(entries[1]["player_id"], "alice")
            self.assertEqual(entries[1]["stats"]["bankroll_delta"], 20)

            ai_only = client.get("/leaderboard", params={"participant_type": "ai"})
            self.assertEqual(ai_only.status_code, 200)
            self.assertEqual(ai_only.json()["entries"][0]["player_id"], "bot-1")
            self.assertEqual(ai_only.json()["total_players"], 1)

    def test_round_events_support_incremental_polling_with_sequence_cursor(self) -> None:
        game_service, client = self.create_client()
        game_service.queue_test_shoe(
            [
                make_card(CardRank.TEN, CardSuit.SPADES),
                make_card(CardRank.SIX, CardSuit.CLUBS),
                make_card(CardRank.SEVEN, CardSuit.DIAMONDS),
                make_card(CardRank.TEN, CardSuit.HEARTS),
                make_card(CardRank.EIGHT, CardSuit.CLUBS),
            ]
        )

        with client:
            self.assertEqual(
                client.post(
                    "/players",
                    json={
                        "player_id": "alice",
                        "display_name": "Alice",
                        "participant_type": "human",
                        "starting_bankroll": 200,
                    },
                ).status_code,
                201,
            )
            self.assertEqual(client.post("/tables", json={"table_id": "table-events", "seat_count": 1}).status_code, 201)
            self.assertEqual(client.post("/tables/table-events/seats/1/join", json={"player_id": "alice"}).status_code, 200)
            self.assertEqual(client.post("/tables/table-events/rounds", json={"round_id": "round-events"}).status_code, 201)

            bet_response = client.post(
                "/rounds/round-events/bets",
                json={"player_id": "alice", "amount": 20},
            )
            self.assertEqual(bet_response.status_code, 200)
            hand_id = bet_response.json()["participants"][0]["hands"][0]["hand_id"]

            initial_events = client.get("/rounds/round-events/events")
            self.assertEqual(initial_events.status_code, 200)
            self.assertEqual(
                [event["event_type"] for event in initial_events.json()["events"]],
                ["round_started", "bet_placed", "initial_cards_dealt"],
            )
            self.assertEqual(initial_events.json()["after_sequence"], 0)
            self.assertEqual(initial_events.json()["last_sequence"], 3)
            self.assertEqual(initial_events.json()["next_after_sequence"], 3)
            self.assertFalse(initial_events.json()["has_more"])

            tail_events = client.get("/rounds/round-events/events", params={"limit": 2})
            self.assertEqual(tail_events.status_code, 200)
            self.assertEqual([event["sequence"] for event in tail_events.json()["events"]], [2, 3])
            self.assertEqual(tail_events.json()["count"], 2)
            self.assertEqual(tail_events.json()["total_count"], 3)
            self.assertEqual(tail_events.json()["next_after_sequence"], 3)
            self.assertFalse(tail_events.json()["has_more"])

            stand_response = client.post(
                "/rounds/round-events/actions",
                json={"player_id": "alice", "hand_id": hand_id, "action": "stand"},
            )
            self.assertEqual(stand_response.status_code, 200)
            self.assertEqual(stand_response.json()["phase"], "complete")

            incremental_events = client.get(
                "/rounds/round-events/events",
                params={"after_sequence": 3, "limit": 2},
            )
            self.assertEqual(incremental_events.status_code, 200)
            self.assertEqual([event["sequence"] for event in incremental_events.json()["events"]], [4, 5])
            self.assertEqual(
                [event["event_type"] for event in incremental_events.json()["events"]],
                ["player_action", "dealer_revealed"],
            )
            self.assertEqual(incremental_events.json()["after_sequence"], 3)
            self.assertEqual(incremental_events.json()["count"], 2)
            self.assertEqual(incremental_events.json()["total_count"], 7)
            self.assertEqual(incremental_events.json()["last_sequence"], 7)
            self.assertEqual(incremental_events.json()["next_after_sequence"], 5)
            self.assertTrue(incremental_events.json()["has_more"])

            settled_events = client.get(
                "/rounds/round-events/events",
                params={"after_sequence": incremental_events.json()["next_after_sequence"]},
            )
            self.assertEqual(settled_events.status_code, 200)
            self.assertEqual([event["sequence"] for event in settled_events.json()["events"]], [6, 7])
            self.assertEqual(
                [event["event_type"] for event in settled_events.json()["events"]],
                ["dealer_hit", "round_settled"],
            )
            self.assertEqual(settled_events.json()["after_sequence"], 5)
            self.assertEqual(settled_events.json()["last_sequence"], 7)
            self.assertEqual(settled_events.json()["next_after_sequence"], 7)
            self.assertFalse(settled_events.json()["has_more"])

            caught_up = client.get(
                "/rounds/round-events/events",
                params={"after_sequence": settled_events.json()["next_after_sequence"]},
            )
            self.assertEqual(caught_up.status_code, 200)
            self.assertEqual(caught_up.json()["events"], [])
            self.assertEqual(caught_up.json()["count"], 0)
            self.assertEqual(caught_up.json()["after_sequence"], 7)
            self.assertEqual(caught_up.json()["last_sequence"], 7)
            self.assertEqual(caught_up.json()["next_after_sequence"], 7)
            self.assertFalse(caught_up.json()["has_more"])

    def test_surrender_keeps_dealer_hidden_for_remaining_players_and_updates_stats(self) -> None:
        game_service, client = self.create_client()
        game_service.queue_test_shoe(
            [
                make_card(CardRank.TEN, CardSuit.SPADES),
                make_card(CardRank.NINE, CardSuit.CLUBS),
                make_card(CardRank.SIX, CardSuit.DIAMONDS),
                make_card(CardRank.SEVEN, CardSuit.HEARTS),
                make_card(CardRank.EIGHT, CardSuit.SPADES),
                make_card(CardRank.KING, CardSuit.CLUBS),
                make_card(CardRank.TWO, CardSuit.HEARTS),
            ]
        )

        with client:
            for player_id, display_name, participant_type, bankroll in (
                ("alice", "Alice", "human", 200),
                ("bot-1", "Bot One", "ai", 300),
            ):
                response = client.post(
                    "/players",
                    json={
                        "player_id": player_id,
                        "display_name": display_name,
                        "participant_type": participant_type,
                        "starting_bankroll": bankroll,
                    },
                )
                self.assertEqual(response.status_code, 201)

            self.assertEqual(client.post("/tables", json={"table_id": "table-surrender", "seat_count": 2}).status_code, 201)
            self.assertEqual(client.post("/tables/table-surrender/seats/1/join", json={"player_id": "alice"}).status_code, 200)
            self.assertEqual(client.post("/tables/table-surrender/seats/2/join", json={"player_id": "bot-1"}).status_code, 200)
            self.assertEqual(client.post("/tables/table-surrender/rounds", json={"round_id": "round-surrender"}).status_code, 201)
            self.assertEqual(client.post("/rounds/round-surrender/bets", json={"player_id": "alice", "amount": 20}).status_code, 200)

            round_response = client.post(
                "/rounds/round-surrender/bets",
                json={"player_id": "bot-1", "amount": 30},
            )
            self.assertEqual(round_response.status_code, 200)

            round_state = round_response.json()
            alice_hand_id = round_state["participants"][0]["hands"][0]["hand_id"]
            bot_hand_id = round_state["participants"][1]["hands"][0]["hand_id"]

            self.assertIn("surrender", round_state["next_request"]["legal_actions"])

            surrender = client.post(
                "/rounds/round-surrender/actions",
                json={"player_id": "alice", "hand_id": alice_hand_id, "action": "surrender"},
            )
            self.assertEqual(surrender.status_code, 200)

            after_surrender = surrender.json()
            alice_hand = after_surrender["participants"][0]["hands"][0]

            self.assertEqual(after_surrender["phase"], "player_turns")
            self.assertEqual(after_surrender["next_request"]["player_id"], "bot-1")
            self.assertEqual(after_surrender["dealer"]["cards"][1], {"is_hidden": True})
            self.assertEqual(alice_hand["status"], "complete")
            self.assertEqual(alice_hand["resolution"]["reason"], "surrender")
            self.assertEqual(alice_hand["resolution"]["net_change"], -10)
            self.assertIsNone(alice_hand["resolution"]["dealer_total"])

            complete = client.post(
                "/rounds/round-surrender/actions",
                json={"player_id": "bot-1", "hand_id": bot_hand_id, "action": "stand"},
            )
            self.assertEqual(complete.status_code, 200)

            completed_round = complete.json()
            self.assertEqual(completed_round["phase"], "complete")
            self.assertTrue(completed_round["dealer"]["hole_card_revealed"])

            player_stats = client.get("/players/alice/stats")
            self.assertEqual(player_stats.status_code, 200)
            self.assertEqual(player_stats.json()["stats"]["losses"], 1)
            self.assertEqual(player_stats.json()["stats"]["bankroll_delta"], -10)
            self.assertEqual(player_stats.json()["stats"]["action_counts"]["surrender"], 1)

            leaderboard = client.get("/leaderboard")
            self.assertEqual(leaderboard.status_code, 200)
            entries = leaderboard.json()["entries"]
            self.assertEqual(entries[0]["player_id"], "alice")
            self.assertEqual(entries[0]["stats"]["bankroll_delta"], -10)
            self.assertEqual(entries[1]["player_id"], "bot-1")
            self.assertEqual(entries[1]["stats"]["bankroll_delta"], -30)

    def test_round_responses_keep_a_persistent_public_shoe_state_until_cut_card(self) -> None:
        game_service, client = self.create_client(randomizer=Random(7))
        persistent_shoe = [
            make_card(CardRank.TEN, CardSuit.SPADES),
            make_card(CardRank.NINE, CardSuit.CLUBS),
            make_card(CardRank.SEVEN, CardSuit.HEARTS),
            make_card(CardRank.EIGHT, CardSuit.DIAMONDS),
            make_card(CardRank.SIX, CardSuit.SPADES),
            make_card(CardRank.SEVEN, CardSuit.CLUBS),
            make_card(CardRank.NINE, CardSuit.HEARTS),
            make_card(CardRank.TEN, CardSuit.DIAMONDS),
            make_card(CardRank.ACE, CardSuit.CLUBS),
            make_card(CardRank.TWO, CardSuit.CLUBS),
            make_card(CardRank.THREE, CardSuit.CLUBS),
            make_card(CardRank.FOUR, CardSuit.CLUBS),
            make_card(CardRank.FIVE, CardSuit.CLUBS),
            make_card(CardRank.SIX, CardSuit.CLUBS),
            make_card(CardRank.SEVEN, CardSuit.SPADES),
            make_card(CardRank.EIGHT, CardSuit.SPADES),
            make_card(CardRank.NINE, CardSuit.SPADES),
            make_card(CardRank.TEN, CardSuit.CLUBS),
            make_card(CardRank.JACK, CardSuit.CLUBS),
            make_card(CardRank.QUEEN, CardSuit.CLUBS),
            make_card(CardRank.KING, CardSuit.CLUBS),
            make_card(CardRank.ACE, CardSuit.DIAMONDS),
            make_card(CardRank.TWO, CardSuit.DIAMONDS),
            make_card(CardRank.THREE, CardSuit.DIAMONDS),
            make_card(CardRank.FOUR, CardSuit.DIAMONDS),
            make_card(CardRank.FIVE, CardSuit.DIAMONDS),
        ]
        game_service.queue_test_shoe(persistent_shoe)

        with client:
            self.assertEqual(
                client.post(
                    "/players",
                    json={
                        "player_id": "solo",
                        "display_name": "Solo",
                        "participant_type": "human",
                        "starting_bankroll": 100,
                    },
                ).status_code,
                201,
            )
            table = client.post(
                "/tables",
                json={
                    "table_id": "table-shoe",
                    "seat_count": 1,
                    "rules": {"deck_count": 1},
                },
            )
            self.assertEqual(table.status_code, 201)
            self.assertEqual(client.post("/tables/table-shoe/seats/1/join", json={"player_id": "solo"}).status_code, 200)

            start_round_one = client.post("/tables/table-shoe/rounds", json={"round_id": "round-shoe-1"})
            self.assertEqual(start_round_one.status_code, 201)
            self.assertEqual(start_round_one.json()["shoe_state"]["cards_remaining"], len(persistent_shoe))
            self.assertEqual(start_round_one.json()["shoe_state"]["shuffle_count"], 1)

            round_one = client.post(
                "/rounds/round-shoe-1/bets",
                json={"player_id": "solo", "amount": 10},
            )
            self.assertEqual(round_one.status_code, 200)
            round_one_hand_id = round_one.json()["participants"][0]["hands"][0]["hand_id"]

            complete_round_one = client.post(
                "/rounds/round-shoe-1/actions",
                json={"player_id": "solo", "hand_id": round_one_hand_id, "action": "stand"},
            )
            self.assertEqual(complete_round_one.status_code, 200)
            self.assertEqual(complete_round_one.json()["phase"], "complete")
            self.assertEqual(complete_round_one.json()["shoe_state"]["cards_remaining"], len(persistent_shoe) - 4)
            self.assertEqual(complete_round_one.json()["shoe_state"]["shuffle_count"], 1)

            table_after_round_one = client.get("/tables/table-shoe")
            self.assertEqual(table_after_round_one.status_code, 200)
            self.assertEqual(table_after_round_one.json()["shoe_state"]["cards_remaining"], len(persistent_shoe) - 4)
            self.assertEqual(table_after_round_one.json()["shoe_state"]["shuffle_count"], 1)

            start_round_two = client.post("/tables/table-shoe/rounds", json={"round_id": "round-shoe-2"})
            self.assertEqual(start_round_two.status_code, 201)
            self.assertEqual(start_round_two.json()["shoe_state"]["cards_remaining"], len(persistent_shoe) - 4)
            self.assertEqual(start_round_two.json()["shoe_state"]["shuffle_count"], 1)

            round_two = client.post(
                "/rounds/round-shoe-2/bets",
                json={"player_id": "solo", "amount": 10},
            )
            self.assertEqual(round_two.status_code, 200)
            self.assertEqual(
                [card["rank"] for card in round_two.json()["participants"][0]["hands"][0]["cards"]],
                ["6", "9"],
            )
            self.assertEqual(round_two.json()["dealer"]["cards"][0]["rank"], "7")
            self.assertEqual(round_two.json()["dealer"]["cards"][1], {"is_hidden": True})
            self.assertEqual(round_two.json()["shoe_state"]["cards_remaining"], len(persistent_shoe) - 8)
            self.assertEqual(round_two.json()["shoe_state"]["shuffle_count"], 1)

    def test_round_events_emit_shoe_reshuffled_only_when_a_new_shoe_starts(self) -> None:
        game_service, client = self.create_client(randomizer=Random(11))
        first_shoe = [
            make_card(CardRank.TEN, CardSuit.SPADES),
            make_card(CardRank.NINE, CardSuit.CLUBS),
            make_card(CardRank.SEVEN, CardSuit.HEARTS),
            make_card(CardRank.EIGHT, CardSuit.DIAMONDS),
            make_card(CardRank.TWO, CardSuit.SPADES),
            make_card(CardRank.THREE, CardSuit.CLUBS),
        ]
        second_shoe = [
            make_card(CardRank.FIVE, CardSuit.SPADES),
            make_card(CardRank.TEN, CardSuit.CLUBS),
            make_card(CardRank.SIX, CardSuit.HEARTS),
            make_card(CardRank.NINE, CardSuit.DIAMONDS),
            make_card(CardRank.TWO, CardSuit.HEARTS),
            make_card(CardRank.THREE, CardSuit.HEARTS),
            make_card(CardRank.FOUR, CardSuit.HEARTS),
            make_card(CardRank.FIVE, CardSuit.HEARTS),
        ]
        game_service.queue_test_shoe(first_shoe)
        game_service.queue_test_shoe(second_shoe)

        with client:
            self.assertEqual(
                client.post(
                    "/players",
                    json={
                        "player_id": "solo",
                        "display_name": "Solo",
                        "participant_type": "human",
                        "starting_bankroll": 100,
                    },
                ).status_code,
                201,
            )
            self.assertEqual(
                client.post(
                    "/tables",
                    json={
                        "table_id": "table-reshuffle-events",
                        "seat_count": 1,
                        "rules": {"deck_count": 1},
                    },
                ).status_code,
                201,
            )
            self.assertEqual(
                client.post("/tables/table-reshuffle-events/seats/1/join", json={"player_id": "solo"}).status_code,
                200,
            )

            self.assertEqual(
                client.post("/tables/table-reshuffle-events/rounds", json={"round_id": "round-cut-1"}).status_code,
                201,
            )
            initial_round_events = client.get("/rounds/round-cut-1/events")
            self.assertEqual(initial_round_events.status_code, 200)
            self.assertEqual(
                [event["event_type"] for event in initial_round_events.json()["events"]],
                ["round_started"],
            )

            round_one = client.post(
                "/rounds/round-cut-1/bets",
                json={"player_id": "solo", "amount": 10},
            )
            self.assertEqual(round_one.status_code, 200)
            round_one_hand_id = round_one.json()["participants"][0]["hands"][0]["hand_id"]

            complete_round_one = client.post(
                "/rounds/round-cut-1/actions",
                json={"player_id": "solo", "hand_id": round_one_hand_id, "action": "stand"},
            )
            self.assertEqual(complete_round_one.status_code, 200)

            start_round_two = client.post("/tables/table-reshuffle-events/rounds", json={"round_id": "round-cut-2"})
            self.assertEqual(start_round_two.status_code, 201)

            reshuffle_events = client.get("/rounds/round-cut-2/events")
            self.assertEqual(reshuffle_events.status_code, 200)
            self.assertEqual(
                [event["event_type"] for event in reshuffle_events.json()["events"]],
                ["round_started", "shoe_reshuffled"],
            )
            shoe_reshuffled_event = reshuffle_events.json()["events"][1]
            self.assertEqual(shoe_reshuffled_event["sequence"], 2)
            self.assertEqual(shoe_reshuffled_event["payload"]["reason"], "cut_card_reached")
            self.assertEqual(
                shoe_reshuffled_event["payload"]["shoe_state"],
                {
                    "cards_remaining": len(second_shoe),
                    "decks_remaining": round(len(second_shoe) / 52, 4),
                    "shuffle_count": 2,
                },
            )
            self.assertEqual(shoe_reshuffled_event["payload"]["shoe_state"], start_round_two.json()["shoe_state"])

            round_two = client.post(
                "/rounds/round-cut-2/bets",
                json={"player_id": "solo", "amount": 10},
            )
            self.assertEqual(round_two.status_code, 200)

            round_two_events = client.get("/rounds/round-cut-2/events")
            self.assertEqual(round_two_events.status_code, 200)
            self.assertEqual(
                [event["event_type"] for event in round_two_events.json()["events"]],
                ["round_started", "shoe_reshuffled", "bet_placed", "initial_cards_dealt"],
            )

    def test_round_events_emit_shoe_reshuffled_when_draw_replaces_an_empty_shoe(self) -> None:
        game_service, client = self.create_client()
        first_shoe = [
            make_card(CardRank.FIVE, CardSuit.SPADES),
            make_card(CardRank.TEN, CardSuit.CLUBS),
            make_card(CardRank.SIX, CardSuit.HEARTS),
            make_card(CardRank.NINE, CardSuit.DIAMONDS),
        ]
        second_shoe = [
            make_card(CardRank.TEN, CardSuit.HEARTS),
            make_card(CardRank.TWO, CardSuit.CLUBS),
        ]
        game_service.queue_test_shoe(first_shoe)
        game_service.queue_test_shoe(second_shoe)

        with client:
            self.assertEqual(
                client.post(
                    "/players",
                    json={
                        "player_id": "solo",
                        "display_name": "Solo",
                        "participant_type": "human",
                        "starting_bankroll": 100,
                    },
                ).status_code,
                201,
            )
            self.assertEqual(
                client.post("/tables", json={"table_id": "table-draw-reshuffle", "seat_count": 1}).status_code,
                201,
            )
            self.assertEqual(client.post("/tables/table-draw-reshuffle/seats/1/join", json={"player_id": "solo"}).status_code, 200)
            self.assertEqual(client.post("/tables/table-draw-reshuffle/rounds", json={"round_id": "round-draw-reshuffle"}).status_code, 201)

            round_state = client.post(
                "/rounds/round-draw-reshuffle/bets",
                json={"player_id": "solo", "amount": 10},
            )
            self.assertEqual(round_state.status_code, 200)
            self.assertEqual(round_state.json()["shoe_state"]["cards_remaining"], 0)
            hand_id = round_state.json()["participants"][0]["hands"][0]["hand_id"]

            initial_events = client.get("/rounds/round-draw-reshuffle/events")
            self.assertEqual(initial_events.status_code, 200)
            self.assertEqual(
                [event["event_type"] for event in initial_events.json()["events"]],
                ["round_started", "bet_placed", "initial_cards_dealt"],
            )

            hit_response = client.post(
                "/rounds/round-draw-reshuffle/actions",
                json={"player_id": "solo", "hand_id": hand_id, "action": "hit"},
            )
            self.assertEqual(hit_response.status_code, 200)
            self.assertEqual(hit_response.json()["phase"], "complete")
            self.assertEqual(hit_response.json()["shoe_state"]["cards_remaining"], len(second_shoe) - 1)
            self.assertEqual(hit_response.json()["shoe_state"]["shuffle_count"], 2)

            post_draw_events = client.get(
                "/rounds/round-draw-reshuffle/events",
                params={"after_sequence": initial_events.json()["next_after_sequence"]},
            )
            self.assertEqual(post_draw_events.status_code, 200)
            self.assertEqual(
                [event["event_type"] for event in post_draw_events.json()["events"]],
                ["shoe_reshuffled", "player_action", "dealer_revealed", "round_settled"],
            )
            self.assertEqual([event["sequence"] for event in post_draw_events.json()["events"][:2]], [4, 5])
            shoe_reshuffled_event = post_draw_events.json()["events"][0]
            self.assertEqual(shoe_reshuffled_event["payload"]["reason"], "shoe_empty")
            self.assertEqual(
                shoe_reshuffled_event["payload"]["shoe_state"],
                {
                    "cards_remaining": len(second_shoe),
                    "decks_remaining": round(len(second_shoe) / 52, 4),
                    "shuffle_count": 2,
                },
            )
            self.assertEqual(
                sum(1 for event in post_draw_events.json()["events"] if event["event_type"] == "shoe_reshuffled"),
                1,
            )

    def test_round_start_reshuffles_after_cut_card_and_advances_shuffle_count(self) -> None:
        game_service, client = self.create_client(randomizer=Random(11))
        first_shoe = [
            make_card(CardRank.TEN, CardSuit.SPADES),
            make_card(CardRank.NINE, CardSuit.CLUBS),
            make_card(CardRank.SEVEN, CardSuit.HEARTS),
            make_card(CardRank.EIGHT, CardSuit.DIAMONDS),
            make_card(CardRank.TWO, CardSuit.SPADES),
            make_card(CardRank.THREE, CardSuit.CLUBS),
        ]
        second_shoe = [
            make_card(CardRank.FIVE, CardSuit.SPADES),
            make_card(CardRank.TEN, CardSuit.CLUBS),
            make_card(CardRank.SIX, CardSuit.HEARTS),
            make_card(CardRank.NINE, CardSuit.DIAMONDS),
            make_card(CardRank.TWO, CardSuit.HEARTS),
            make_card(CardRank.THREE, CardSuit.HEARTS),
            make_card(CardRank.FOUR, CardSuit.HEARTS),
            make_card(CardRank.FIVE, CardSuit.HEARTS),
        ]
        game_service.queue_test_shoe(first_shoe)
        game_service.queue_test_shoe(second_shoe)

        with client:
            self.assertEqual(
                client.post(
                    "/players",
                    json={
                        "player_id": "solo",
                        "display_name": "Solo",
                        "participant_type": "human",
                        "starting_bankroll": 100,
                    },
                ).status_code,
                201,
            )
            self.assertEqual(
                client.post(
                    "/tables",
                    json={
                        "table_id": "table-cut-card",
                        "seat_count": 1,
                        "rules": {"deck_count": 1},
                    },
                ).status_code,
                201,
            )
            self.assertEqual(client.post("/tables/table-cut-card/seats/1/join", json={"player_id": "solo"}).status_code, 200)
            self.assertEqual(client.post("/tables/table-cut-card/rounds", json={"round_id": "round-cut-1"}).status_code, 201)

            round_one = client.post(
                "/rounds/round-cut-1/bets",
                json={"player_id": "solo", "amount": 10},
            )
            self.assertEqual(round_one.status_code, 200)
            round_one_hand_id = round_one.json()["participants"][0]["hands"][0]["hand_id"]

            complete_round_one = client.post(
                "/rounds/round-cut-1/actions",
                json={"player_id": "solo", "hand_id": round_one_hand_id, "action": "stand"},
            )
            self.assertEqual(complete_round_one.status_code, 200)
            self.assertEqual(complete_round_one.json()["shoe_state"]["cards_remaining"], len(first_shoe) - 4)
            self.assertEqual(complete_round_one.json()["shoe_state"]["shuffle_count"], 1)

            table_before_reshuffle = client.get("/tables/table-cut-card")
            self.assertEqual(table_before_reshuffle.status_code, 200)
            self.assertEqual(table_before_reshuffle.json()["shoe_state"]["cards_remaining"], len(first_shoe) - 4)
            self.assertEqual(table_before_reshuffle.json()["shoe_state"]["shuffle_count"], 1)

            start_round_two = client.post("/tables/table-cut-card/rounds", json={"round_id": "round-cut-2"})
            self.assertEqual(start_round_two.status_code, 201)
            self.assertEqual(start_round_two.json()["shoe_state"]["cards_remaining"], len(second_shoe))
            self.assertEqual(start_round_two.json()["shoe_state"]["shuffle_count"], 2)

            round_two = client.post(
                "/rounds/round-cut-2/bets",
                json={"player_id": "solo", "amount": 10},
            )
            self.assertEqual(round_two.status_code, 200)
            self.assertEqual(
                [card["rank"] for card in round_two.json()["participants"][0]["hands"][0]["cards"]],
                ["5", "6"],
            )
            self.assertEqual(round_two.json()["shoe_state"]["cards_remaining"], len(second_shoe) - 4)
            self.assertEqual(round_two.json()["shoe_state"]["shuffle_count"], 2)

    def test_insurance_is_an_opening_action_and_preserves_hidden_dealer_information(self) -> None:
        game_service, client = self.create_client()
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

        with client:
            self.assertEqual(
                client.post(
                    "/players",
                    json={
                        "player_id": "alice",
                        "display_name": "Alice",
                        "participant_type": "human",
                        "starting_bankroll": 30,
                    },
                ).status_code,
                201,
            )
            self.assertEqual(
                client.post(
                    "/players",
                    json={
                        "player_id": "bot-1",
                        "display_name": "Bot One",
                        "participant_type": "ai",
                        "starting_bankroll": 100,
                    },
                ).status_code,
                201,
            )
            self.assertEqual(client.post("/tables", json={"table_id": "table-insurance", "seat_count": 2}).status_code, 201)
            self.assertEqual(client.post("/tables/table-insurance/seats/1/join", json={"player_id": "alice"}).status_code, 200)
            self.assertEqual(client.post("/tables/table-insurance/seats/2/join", json={"player_id": "bot-1"}).status_code, 200)
            self.assertEqual(client.post("/tables/table-insurance/rounds", json={"round_id": "round-insurance"}).status_code, 201)
            self.assertEqual(client.post("/rounds/round-insurance/bets", json={"player_id": "alice", "amount": 20}).status_code, 200)

            round_response = client.post(
                "/rounds/round-insurance/bets",
                json={"player_id": "bot-1", "amount": 20},
            )
            self.assertEqual(round_response.status_code, 200)

            round_state = round_response.json()
            alice_hand_id = round_state["participants"][0]["hands"][0]["hand_id"]
            bot_hand_id = round_state["participants"][1]["hands"][0]["hand_id"]

            self.assertEqual(round_state["phase"], "player_turns")
            self.assertEqual(round_state["next_request"]["type"], "action")
            self.assertEqual(round_state["next_request"]["player_id"], "alice")
            self.assertEqual(round_state["next_request"]["hand_id"], alice_hand_id)
            self.assertEqual(round_state["dealer"]["cards"][1], {"is_hidden": True})
            self.assertEqual(round_state["dealer"]["status"], "active")
            self.assertIn("insurance", round_state["next_request"]["legal_actions"])
            self.assertNotIn("insurance", round_state["participants"][0])

            alice_insurance = client.post(
                "/rounds/round-insurance/actions",
                json={"player_id": "alice", "hand_id": alice_hand_id, "action": "insurance"},
            )
            self.assertEqual(alice_insurance.status_code, 200)
            self.assertEqual(alice_insurance.json()["phase"], "player_turns")
            self.assertEqual(alice_insurance.json()["next_request"]["player_id"], "alice")
            self.assertEqual(alice_insurance.json()["next_request"]["hand_id"], alice_hand_id)
            self.assertEqual(alice_insurance.json()["dealer"]["cards"][1], {"is_hidden": True})
            self.assertNotIn("insurance", alice_insurance.json()["next_request"]["legal_actions"])
            self.assertNotIn("double", alice_insurance.json()["next_request"]["legal_actions"])

            events = client.get("/rounds/round-insurance/events")
            self.assertEqual(events.status_code, 200)
            self.assertEqual(events.json()["events"][-1]["event_type"], "player_action")
            self.assertEqual(events.json()["events"][-1]["payload"]["action"], "insurance")

            self.assertEqual(
                client.post(
                    "/rounds/round-insurance/actions",
                    json={"player_id": "alice", "hand_id": alice_hand_id, "action": "stand"},
                ).status_code,
                200,
            )
            complete = client.post(
                "/rounds/round-insurance/actions",
                json={"player_id": "bot-1", "hand_id": bot_hand_id, "action": "stand"},
            )
            self.assertEqual(complete.status_code, 200)
            self.assertEqual(complete.json()["phase"], "complete")
            self.assertTrue(complete.json()["dealer"]["hole_card_revealed"])

            stats = client.get("/players/alice/stats")
            self.assertEqual(stats.status_code, 200)
            self.assertEqual(stats.json()["stats"]["losses"], 1)
            self.assertEqual(stats.json()["stats"]["bankroll_delta"], -30)
            self.assertEqual(stats.json()["stats"]["action_counts"]["insurance"], 1)

    def test_insurance_stays_a_single_side_bet_after_split(self) -> None:
        game_service, client = self.create_client()
        game_service.queue_test_shoe(
            [
                make_card(CardRank.TEN, CardSuit.SPADES),
                make_card(CardRank.ACE, CardSuit.DIAMONDS),
                make_card(CardRank.TEN, CardSuit.HEARTS),
                make_card(CardRank.KING, CardSuit.CLUBS),
                make_card(CardRank.EIGHT, CardSuit.SPADES),
                make_card(CardRank.EIGHT, CardSuit.HEARTS),
            ]
        )

        with client:
            self.assertEqual(
                client.post(
                    "/players",
                    json={
                        "player_id": "alice",
                        "display_name": "Alice",
                        "participant_type": "human",
                        "starting_bankroll": 100,
                    },
                ).status_code,
                201,
            )
            self.assertEqual(
                client.post("/tables", json={"table_id": "table-insurance-split", "seat_count": 1}).status_code,
                201,
            )
            self.assertEqual(
                client.post("/tables/table-insurance-split/seats/1/join", json={"player_id": "alice"}).status_code,
                200,
            )
            self.assertEqual(
                client.post("/tables/table-insurance-split/rounds", json={"round_id": "round-insurance-split"}).status_code,
                201,
            )

            round_response = client.post(
                "/rounds/round-insurance-split/bets",
                json={"player_id": "alice", "amount": 20},
            )
            self.assertEqual(round_response.status_code, 200)

            round_state = round_response.json()
            alice_hand_id = round_state["participants"][0]["hands"][0]["hand_id"]
            self.assertIn("insurance", round_state["next_request"]["legal_actions"])
            self.assertIn("split", round_state["next_request"]["legal_actions"])

            after_insurance = client.post(
                "/rounds/round-insurance-split/actions",
                json={"player_id": "alice", "hand_id": alice_hand_id, "action": "insurance"},
            )
            self.assertEqual(after_insurance.status_code, 200)
            self.assertIn("split", after_insurance.json()["next_request"]["legal_actions"])

            after_split = client.post(
                "/rounds/round-insurance-split/actions",
                json={"player_id": "alice", "hand_id": alice_hand_id, "action": "split"},
            )
            self.assertEqual(after_split.status_code, 200)
            split_payload = after_split.json()
            self.assertEqual(len(split_payload["participants"][0]["hands"]), 2)
            self.assertEqual(split_payload["participants"][0]["total_committed"], 50)
            self.assertEqual(split_payload["participants"][0]["available_bankroll"], 50)

            first_hand_id = split_payload["participants"][0]["hands"][0]["hand_id"]
            second_hand_id = split_payload["participants"][0]["hands"][1]["hand_id"]
            self.assertEqual(
                client.post(
                    "/rounds/round-insurance-split/actions",
                    json={"player_id": "alice", "hand_id": first_hand_id, "action": "stand"},
                ).status_code,
                200,
            )
            complete = client.post(
                "/rounds/round-insurance-split/actions",
                json={"player_id": "alice", "hand_id": second_hand_id, "action": "stand"},
            )
            self.assertEqual(complete.status_code, 200)
            self.assertEqual(complete.json()["phase"], "complete")
            self.assertEqual(complete.json()["participants"][0]["bankroll_after_round"], 80)

            stats = client.get("/players/alice/stats")
            self.assertEqual(stats.status_code, 200)
            self.assertEqual(stats.json()["stats"]["bankroll_delta"], -20)
            self.assertEqual(stats.json()["stats"]["losses"], 2)
            self.assertEqual(stats.json()["stats"]["action_counts"]["insurance"], 1)
            self.assertEqual(stats.json()["stats"]["action_counts"]["split"], 1)

    def test_round_can_progress_without_queued_test_shoe(self) -> None:
        _, client = self.create_client(randomizer=Random(7))

        with client:
            self.assertEqual(
                client.post(
                    "/players",
                    json={
                        "player_id": "solo",
                        "display_name": "Solo",
                        "participant_type": "human",
                        "starting_bankroll": 100,
                    },
                ).status_code,
                201,
            )
            self.assertEqual(
                client.post("/tables", json={"table_id": "table-random", "seat_count": 1}).status_code,
                201,
            )
            self.assertEqual(
                client.post("/tables/table-random/seats/1/join", json={"player_id": "solo"}).status_code,
                200,
            )
            self.assertEqual(
                client.post("/tables/table-random/rounds", json={"round_id": "round-random"}).status_code,
                201,
            )

            response = client.post(
                "/rounds/round-random/bets",
                json={"player_id": "solo", "amount": 10},
            )
            self.assertEqual(response.status_code, 200)

            payload = response.json()
            self.assertEqual(payload["round_id"], "round-random")
            self.assertIn(payload["phase"], {"player_turns", "complete"})
            self.assertEqual(len(payload["participants"]), 1)

    def test_invalid_bets_and_out_of_turn_actions_are_rejected_cleanly(self) -> None:
        game_service, client = self.create_client()
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

        with client:
            for player_id, participant_type in (("alice", "human"), ("bot-1", "ai")):
                response = client.post(
                    "/players",
                    json={
                        "player_id": player_id,
                        "display_name": player_id.title(),
                        "participant_type": participant_type,
                        "starting_bankroll": 200,
                    },
                )
                self.assertEqual(response.status_code, 201)

            response = client.post("/tables", json={"table_id": "table-main", "seat_count": 2})
            self.assertEqual(response.status_code, 201)
            self.assertEqual(client.post("/tables/table-main/seats/1/join", json={"player_id": "alice"}).status_code, 200)
            self.assertEqual(client.post("/tables/table-main/seats/2/join", json={"player_id": "bot-1"}).status_code, 200)
            self.assertEqual(client.post("/tables/table-main/rounds", json={"round_id": "round-2"}).status_code, 201)

            invalid_bet = client.post(
                "/rounds/round-2/bets",
                json={"player_id": "alice", "amount": 5},
            )
            self.assertEqual(invalid_bet.status_code, 400)
            self.assertIn("minimum table limit", invalid_bet.json()["detail"])

            self.assertEqual(
                client.post("/rounds/round-2/bets", json={"player_id": "alice", "amount": 20}).status_code,
                200,
            )

            duplicate_bet = client.post(
                "/rounds/round-2/bets",
                json={"player_id": "alice", "amount": 20},
            )
            self.assertEqual(duplicate_bet.status_code, 409)
            self.assertIn("already placed", duplicate_bet.json()["detail"])

            bot_bet = client.post(
                "/rounds/round-2/bets",
                json={"player_id": "bot-1", "amount": 30},
            )
            self.assertEqual(bot_bet.status_code, 200)
            bot_hand_id = bot_bet.json()["participants"][1]["hands"][0]["hand_id"]

            out_of_turn = client.post(
                "/rounds/round-2/actions",
                json={"player_id": "bot-1", "hand_id": bot_hand_id, "action": "stand"},
            )
            self.assertEqual(out_of_turn.status_code, 409)
            self.assertIn("not this player's turn", out_of_turn.json()["detail"])

    def test_round_version_is_exposed_and_can_guard_bets_and_actions(self) -> None:
        game_service, client = self.create_client()
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

        with client:
            for player_id, participant_type in (("alice", "human"), ("bot-1", "ai")):
                response = client.post(
                    "/players",
                    json={
                        "player_id": player_id,
                        "display_name": player_id.title(),
                        "participant_type": participant_type,
                        "starting_bankroll": 200,
                    },
                )
                self.assertEqual(response.status_code, 201)

            self.assertEqual(client.post("/tables", json={"table_id": "table-versioned", "seat_count": 2}).status_code, 201)
            self.assertEqual(client.post("/tables/table-versioned/seats/1/join", json={"player_id": "alice"}).status_code, 200)
            self.assertEqual(client.post("/tables/table-versioned/seats/2/join", json={"player_id": "bot-1"}).status_code, 200)

            round_response = client.post("/tables/table-versioned/rounds", json={"round_id": "round-versioned"})
            self.assertEqual(round_response.status_code, 201)
            self.assertEqual(round_response.json()["version"], 0)

            first_bet = client.post(
                "/rounds/round-versioned/bets",
                json={"player_id": "alice", "amount": 20, "expected_version": 0},
            )
            self.assertEqual(first_bet.status_code, 200)
            self.assertEqual(first_bet.json()["version"], 1)

            stale_bet = client.post(
                "/rounds/round-versioned/bets",
                json={"player_id": "bot-1", "amount": 30, "expected_version": 0},
            )
            self.assertEqual(stale_bet.status_code, 409)
            self.assertIn("expected version 0", stale_bet.json()["detail"])
            self.assertIn("found 1", stale_bet.json()["detail"])

            second_bet = client.post(
                "/rounds/round-versioned/bets",
                json={"player_id": "bot-1", "amount": 30, "expected_version": 1},
            )
            self.assertEqual(second_bet.status_code, 200)
            self.assertEqual(second_bet.json()["version"], 2)

            alice_hand_id = second_bet.json()["participants"][0]["hands"][0]["hand_id"]

            stale_action = client.post(
                "/rounds/round-versioned/actions",
                json={
                    "player_id": "alice",
                    "hand_id": alice_hand_id,
                    "action": "stand",
                    "expected_version": 1,
                },
            )
            self.assertEqual(stale_action.status_code, 409)
            self.assertIn("expected version 1", stale_action.json()["detail"])
            self.assertIn("found 2", stale_action.json()["detail"])

            first_action = client.post(
                "/rounds/round-versioned/actions",
                json={
                    "player_id": "alice",
                    "hand_id": alice_hand_id,
                    "action": "stand",
                    "expected_version": 2,
                },
            )
            self.assertEqual(first_action.status_code, 200)
            self.assertEqual(first_action.json()["version"], 3)

    def test_bet_request_ids_replay_original_response_and_reject_mismatched_payloads(self) -> None:
        game_service, client = self.create_client()
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

        with client:
            for player_id, participant_type in (("alice", "human"), ("bot-1", "ai")):
                response = client.post(
                    "/players",
                    json={
                        "player_id": player_id,
                        "display_name": player_id.title(),
                        "participant_type": participant_type,
                        "starting_bankroll": 200,
                    },
                )
                self.assertEqual(response.status_code, 201)

            self.assertEqual(client.post("/tables", json={"table_id": "table-idempotent", "seat_count": 2}).status_code, 201)
            self.assertEqual(client.post("/tables/table-idempotent/seats/1/join", json={"player_id": "alice"}).status_code, 200)
            self.assertEqual(client.post("/tables/table-idempotent/seats/2/join", json={"player_id": "bot-1"}).status_code, 200)
            self.assertEqual(client.post("/tables/table-idempotent/rounds", json={"round_id": "round-idempotent-bet"}).status_code, 201)

            first_bet = client.post(
                "/rounds/round-idempotent-bet/bets",
                json={"player_id": "alice", "amount": 20, "request_id": "alice-bet-1", "expected_version": 0},
            )
            self.assertEqual(first_bet.status_code, 200)
            self.assertEqual(first_bet.json()["phase"], "waiting_for_bets")
            self.assertEqual(first_bet.json()["version"], 1)

            bot_bet = client.post(
                "/rounds/round-idempotent-bet/bets",
                json={"player_id": "bot-1", "amount": 30, "expected_version": 1},
            )
            self.assertEqual(bot_bet.status_code, 200)
            self.assertEqual(bot_bet.json()["phase"], "player_turns")
            self.assertEqual(bot_bet.json()["version"], 2)

            current_round = client.get("/rounds/round-idempotent-bet")
            self.assertEqual(current_round.status_code, 200)
            self.assertEqual(current_round.json()["phase"], "player_turns")
            self.assertEqual(current_round.json()["version"], 2)

            replayed_bet = client.post(
                "/rounds/round-idempotent-bet/bets",
                json={"player_id": "alice", "amount": 20, "request_id": "alice-bet-1", "expected_version": 999},
            )
            self.assertEqual(replayed_bet.status_code, 200)
            self.assertEqual(replayed_bet.json(), first_bet.json())
            self.assertNotEqual(replayed_bet.json()["phase"], current_round.json()["phase"])
            self.assertEqual(replayed_bet.json()["version"], 1)

            mismatched_retry = client.post(
                "/rounds/round-idempotent-bet/bets",
                json={"player_id": "alice", "amount": 25, "request_id": "alice-bet-1", "expected_version": 999},
            )
            self.assertEqual(mismatched_retry.status_code, 409)
            self.assertIn("alice-bet-1", mismatched_retry.json()["detail"])
            self.assertIn("different payload", mismatched_retry.json()["detail"])

    def test_action_request_ids_replay_original_response_and_reject_mismatched_payloads(self) -> None:
        game_service, client = self.create_client()
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

        with client:
            for player_id, participant_type in (("alice", "human"), ("bot-1", "ai")):
                response = client.post(
                    "/players",
                    json={
                        "player_id": player_id,
                        "display_name": player_id.title(),
                        "participant_type": participant_type,
                        "starting_bankroll": 200,
                    },
                )
                self.assertEqual(response.status_code, 201)

            self.assertEqual(client.post("/tables", json={"table_id": "table-idempotent-action", "seat_count": 2}).status_code, 201)
            self.assertEqual(client.post("/tables/table-idempotent-action/seats/1/join", json={"player_id": "alice"}).status_code, 200)
            self.assertEqual(client.post("/tables/table-idempotent-action/seats/2/join", json={"player_id": "bot-1"}).status_code, 200)
            self.assertEqual(
                client.post("/tables/table-idempotent-action/rounds", json={"round_id": "round-idempotent-action"}).status_code,
                201,
            )

            self.assertEqual(
                client.post("/rounds/round-idempotent-action/bets", json={"player_id": "alice", "amount": 20}).status_code,
                200,
            )
            round_state = client.post(
                "/rounds/round-idempotent-action/bets",
                json={"player_id": "bot-1", "amount": 30},
            )
            self.assertEqual(round_state.status_code, 200)
            alice_hand_id = round_state.json()["participants"][0]["hands"][0]["hand_id"]
            bot_hand_id = round_state.json()["participants"][1]["hands"][0]["hand_id"]

            first_action = client.post(
                "/rounds/round-idempotent-action/actions",
                json={
                    "player_id": "alice",
                    "hand_id": alice_hand_id,
                    "action": "stand",
                    "request_id": "alice-stand-1",
                    "expected_version": 2,
                },
            )
            self.assertEqual(first_action.status_code, 200)
            self.assertEqual(first_action.json()["phase"], "player_turns")
            self.assertEqual(first_action.json()["next_request"]["player_id"], "bot-1")
            self.assertEqual(first_action.json()["version"], 3)

            complete_round = client.post(
                "/rounds/round-idempotent-action/actions",
                json={"player_id": "bot-1", "hand_id": bot_hand_id, "action": "stand", "expected_version": 3},
            )
            self.assertEqual(complete_round.status_code, 200)
            self.assertEqual(complete_round.json()["phase"], "complete")
            self.assertEqual(complete_round.json()["version"], 4)

            current_round = client.get("/rounds/round-idempotent-action")
            self.assertEqual(current_round.status_code, 200)
            self.assertEqual(current_round.json()["phase"], "complete")
            self.assertEqual(current_round.json()["version"], 4)

            replayed_action = client.post(
                "/rounds/round-idempotent-action/actions",
                json={
                    "player_id": "alice",
                    "hand_id": alice_hand_id,
                    "action": "stand",
                    "request_id": "alice-stand-1",
                    "expected_version": 999,
                },
            )
            self.assertEqual(replayed_action.status_code, 200)
            self.assertEqual(replayed_action.json(), first_action.json())
            self.assertNotEqual(replayed_action.json()["phase"], current_round.json()["phase"])
            self.assertEqual(replayed_action.json()["version"], 3)

            mismatched_retry = client.post(
                "/rounds/round-idempotent-action/actions",
                json={
                    "player_id": "alice",
                    "hand_id": alice_hand_id,
                    "action": "hit",
                    "request_id": "alice-stand-1",
                    "expected_version": 999,
                },
            )
            self.assertEqual(mismatched_retry.status_code, 409)
            self.assertIn("alice-stand-1", mismatched_retry.json()["detail"])
            self.assertIn("different payload", mismatched_retry.json()["detail"])

    def test_hitting_to_twenty_one_auto_stands_and_settles_the_round(self) -> None:
        game_service, client = self.create_client()
        game_service.queue_test_shoe(
            [
                make_card(CardRank.TEN, CardSuit.SPADES),
                make_card(CardRank.SIX, CardSuit.CLUBS),
                make_card(CardRank.SEVEN, CardSuit.HEARTS),
                make_card(CardRank.NINE, CardSuit.DIAMONDS),
                make_card(CardRank.FOUR, CardSuit.SPADES),
                make_card(CardRank.TEN, CardSuit.HEARTS),
            ]
        )

        with client:
            self.assertEqual(
                client.post(
                    "/players",
                    json={
                        "player_id": "alice",
                        "display_name": "Alice",
                        "participant_type": "human",
                        "starting_bankroll": 200,
                    },
                ).status_code,
                201,
            )
            self.assertEqual(client.post("/tables", json={"table_id": "table-21", "seat_count": 1}).status_code, 201)
            self.assertEqual(client.post("/tables/table-21/seats/1/join", json={"player_id": "alice"}).status_code, 200)
            self.assertEqual(client.post("/tables/table-21/rounds", json={"round_id": "round-21"}).status_code, 201)

            bet_response = client.post(
                "/rounds/round-21/bets",
                json={"player_id": "alice", "amount": 20},
            )
            self.assertEqual(bet_response.status_code, 200)
            hand_id = bet_response.json()["participants"][0]["hands"][0]["hand_id"]

            hit_response = client.post(
                "/rounds/round-21/actions",
                json={"player_id": "alice", "hand_id": hand_id, "action": "hit"},
            )
            self.assertEqual(hit_response.status_code, 200)

            payload = hit_response.json()
            player_hand = payload["participants"][0]["hands"][0]

            self.assertEqual(payload["phase"], "complete")
            self.assertEqual(payload["next_request"], {"type": "round_complete"})
            self.assertTrue(payload["dealer"]["hole_card_revealed"])
            self.assertEqual(player_hand["value"]["best_total"], 21)
            self.assertFalse(player_hand["value"]["is_blackjack"])
            self.assertEqual(player_hand["status"], "complete")
            self.assertEqual(player_hand["resolution"]["reason"], "dealer_bust")

            events = client.get("/rounds/round-21/events")
            self.assertEqual(events.status_code, 200)
            self.assertEqual(events.json()["events"][-1]["event_type"], "round_settled")
            self.assertTrue(events.json()["events"][-1]["payload"]["round"]["dealer"]["hole_card_revealed"])
            self.assertNotEqual(
                events.json()["events"][-1]["payload"]["round"]["dealer"]["cards"][1],
                {"is_hidden": True},
            )

    def test_split_flow_rejects_non_current_hand_actions_and_reveals_dealer_at_settlement(self) -> None:
        game_service, client = self.create_client()
        game_service.queue_test_shoe(
            [
                make_card(CardRank.ACE, CardSuit.SPADES),
                make_card(CardRank.SIX, CardSuit.CLUBS),
                make_card(CardRank.ACE, CardSuit.HEARTS),
                make_card(CardRank.NINE, CardSuit.DIAMONDS),
                make_card(CardRank.KING, CardSuit.SPADES),
                make_card(CardRank.FIVE, CardSuit.HEARTS),
                make_card(CardRank.TEN, CardSuit.CLUBS),
            ]
        )

        with client:
            self.assertEqual(
                client.post(
                    "/players",
                    json={
                        "player_id": "alice",
                        "display_name": "Alice",
                        "participant_type": "human",
                        "starting_bankroll": 200,
                    },
                ).status_code,
                201,
            )
            self.assertEqual(client.post("/tables", json={"table_id": "table-split", "seat_count": 1}).status_code, 201)
            self.assertEqual(client.post("/tables/table-split/seats/1/join", json={"player_id": "alice"}).status_code, 200)
            self.assertEqual(client.post("/tables/table-split/rounds", json={"round_id": "round-split"}).status_code, 201)

            bet_response = client.post(
                "/rounds/round-split/bets",
                json={"player_id": "alice", "amount": 20},
            )
            self.assertEqual(bet_response.status_code, 200)
            opening_hand_id = bet_response.json()["participants"][0]["hands"][0]["hand_id"]

            split_response = client.post(
                "/rounds/round-split/actions",
                json={"player_id": "alice", "hand_id": opening_hand_id, "action": "split"},
            )
            self.assertEqual(split_response.status_code, 200)

            payload = split_response.json()
            first_hand, second_hand = payload["participants"][0]["hands"]

            self.assertEqual(payload["dealer"]["cards"][1], {"is_hidden": True})
            self.assertEqual(first_hand["status"], "standing")
            self.assertEqual(first_hand["value"]["best_total"], 21)
            self.assertFalse(first_hand["value"]["is_blackjack"])
            self.assertEqual(payload["next_request"]["hand_id"], second_hand["hand_id"])

            wrong_hand = client.post(
                "/rounds/round-split/actions",
                json={"player_id": "alice", "hand_id": first_hand["hand_id"], "action": "stand"},
            )
            self.assertEqual(wrong_hand.status_code, 409)
            self.assertIn("not this player's turn", wrong_hand.json()["detail"])

            stand_response = client.post(
                "/rounds/round-split/actions",
                json={"player_id": "alice", "hand_id": second_hand["hand_id"], "action": "stand"},
            )
            self.assertEqual(stand_response.status_code, 200)

            complete = stand_response.json()
            self.assertEqual(complete["phase"], "complete")
            self.assertTrue(complete["dealer"]["hole_card_revealed"])
            self.assertEqual(complete["participants"][0]["hands"][0]["resolution"]["outcome"], "win")

            events = client.get("/rounds/round-split/events")
            self.assertEqual(events.status_code, 200)
            event_types = [event["event_type"] for event in events.json()["events"]]
            self.assertEqual(event_types[-1], "round_settled")
            self.assertIn("dealer_revealed", event_types)
            self.assertTrue(events.json()["events"][-1]["payload"]["round"]["dealer"]["hole_card_revealed"])
            self.assertNotEqual(
                events.json()["events"][-1]["payload"]["round"]["dealer"]["cards"][1],
                {"is_hidden": True},
            )

    def test_round_flow_works_without_a_seeded_test_shoe(self) -> None:
        _, client = self.create_client()

        with client:
            self.assertEqual(
                client.post(
                    "/players",
                    json={
                        "player_id": "alice",
                        "display_name": "Alice",
                        "participant_type": "human",
                        "starting_bankroll": 200,
                    },
                ).status_code,
                201,
            )
            self.assertEqual(client.post("/tables", json={"table_id": "table-main", "seat_count": 1}).status_code, 201)
            self.assertEqual(client.post("/tables/table-main/seats/1/join", json={"player_id": "alice"}).status_code, 200)
            self.assertEqual(client.post("/tables/table-main/rounds", json={"round_id": "round-3"}).status_code, 201)

            response = client.post(
                "/rounds/round-3/bets",
                json={"player_id": "alice", "amount": 20},
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn(response.json()["phase"], {"player_turns", "complete"})
            self.assertEqual(response.json()["round_id"], "round-3")
            self.assertEqual(len(response.json()["dealer"]["cards"]), 2)


if __name__ == "__main__":
    unittest.main()
