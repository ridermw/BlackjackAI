from __future__ import annotations

import unittest
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient as FastApiTestClient

from blackjack_ai.api.app import create_app
from blackjack_ai.api.schemas import ActionRequest
from blackjack_ai.api.schemas import BetRequest
from blackjack_ai.api.schemas import CreatePlayerRequest
from blackjack_ai.api.schemas import CreateTableRequest
from blackjack_ai.api.schemas import SeatJoinRequest
from blackjack_ai.api.schemas import StartRoundRequest
from blackjack_ai.api.service import GameService
from blackjack_ai.config import Settings
from blackjack_ai.engine import Card
from blackjack_ai.engine import CardRank
from blackjack_ai.engine import CardSuit
from blackjack_ai.repository import SqliteGameRepository


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


class PersistenceTests(unittest.TestCase):
    def build_settings(self, name: str) -> Settings:
        database_path = (Path.cwd() / "tests" / f"{name}-{uuid4().hex}.sqlite3").resolve()
        self.addCleanup(self.cleanup_database_files, database_path)
        return Settings(
            environment="test",
            database_url=f"sqlite:///{database_path}",
        )

    def cleanup_database_files(self, database_path: Path) -> None:
        for candidate in (
            database_path,
            Path(f"{database_path}-journal"),
            Path(f"{database_path}-wal"),
            Path(f"{database_path}-shm"),
        ):
            if candidate.exists():
                candidate.unlink()

    def seed_two_player_shoe(self, client: TestClient) -> None:
        client.app.state.game_service.queue_test_shoe(
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

    def seed_surrender_shoe(self, client: TestClient) -> None:
        client.app.state.game_service.queue_test_shoe(
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

    def seed_insurance_shoe(self, client: TestClient) -> None:
        client.app.state.game_service.queue_test_shoe(
            [
                make_card(CardRank.TEN, CardSuit.SPADES),
                make_card(CardRank.NINE, CardSuit.CLUBS),
                make_card(CardRank.ACE, CardSuit.DIAMONDS),
                make_card(CardRank.SEVEN, CardSuit.HEARTS),
                make_card(CardRank.EIGHT, CardSuit.SPADES),
                make_card(CardRank.NINE, CardSuit.CLUBS),
            ]
        )

    def create_two_player_round(self, client: TestClient, *, table_id: str, round_id: str) -> dict[str, object]:
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
                    "player_id": "bot-1",
                    "display_name": "Bot One",
                    "participant_type": "ai",
                    "starting_bankroll": 300,
                    "metadata": {"strategy": "baseline"},
                },
            ).status_code,
            201,
        )
        self.assertEqual(client.post("/tables", json={"table_id": table_id, "seat_count": 2}).status_code, 201)
        self.assertEqual(client.post(f"/tables/{table_id}/seats/1/join", json={"player_id": "alice"}).status_code, 200)
        self.assertEqual(client.post(f"/tables/{table_id}/seats/2/join", json={"player_id": "bot-1"}).status_code, 200)
        self.assertEqual(client.post(f"/tables/{table_id}/rounds", json={"round_id": round_id}).status_code, 201)
        self.assertEqual(client.post(f"/rounds/{round_id}/bets", json={"player_id": "alice", "amount": 20}).status_code, 200)
        response = client.post(f"/rounds/{round_id}/bets", json={"player_id": "bot-1", "amount": 30})
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_player_token_persists_across_restart_for_player_owned_mutations(self) -> None:
        settings = self.build_settings("player-token")

        with TestClient(create_app(settings)) as client:
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

        with TestClient(create_app(settings)) as client:
            joined = client.post(
                "/tables/table-auth/seats/1/join",
                json={"player_id": "alice"},
                headers={"X-Player-Token": player_token},
                auto_auth=False,
            )
            self.assertEqual(joined.status_code, 200)

            player = client.get("/players/alice")
            self.assertEqual(player.status_code, 200)
            self.assertNotIn("player_token", player.json())
            self.assertNotIn(player_token, str(player.json()))

    def test_leave_table_seat_persists_across_app_restart(self) -> None:
        settings = self.build_settings("leave-seat")

        with TestClient(create_app(settings)) as client:
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
            self.assertEqual(client.post("/tables", json={"table_id": "table-leave", "seat_count": 1}).status_code, 201)
            self.assertEqual(client.post("/tables/table-leave/seats/1/join", json={"player_id": "alice"}).status_code, 200)

            left = client.post(
                "/tables/table-leave/seats/1/leave",
                json={"player_id": "alice"},
            )
            self.assertEqual(left.status_code, 200)
            self.assertEqual(left.json()["seats"][0]["status"], "empty")

        with TestClient(create_app(settings)) as client:
            table = client.get("/tables/table-leave")
            self.assertEqual(table.status_code, 200)
            self.assertEqual(table.json()["seats"][0]["status"], "empty")
            self.assertIsNone(table.json()["seats"][0]["occupant"])

            joined = client.post("/tables/table-leave/seats/1/join", json={"player_id": "bob"})
            self.assertEqual(joined.status_code, 200)
            self.assertEqual(joined.json()["seats"][0]["occupant"]["player_id"], "bob")

    def test_completed_round_and_stats_persist_across_app_restart(self) -> None:
        settings = self.build_settings("completed-round")

        with TestClient(create_app(settings)) as client:
            self.seed_two_player_shoe(client)
            round_state = self.create_two_player_round(client, table_id="table-main", round_id="round-1")
            alice_hand_id = round_state["participants"][0]["hands"][0]["hand_id"]
            bot_hand_id = round_state["participants"][1]["hands"][0]["hand_id"]

            self.assertEqual(
                client.post(
                    "/rounds/round-1/actions",
                    json={"player_id": "alice", "hand_id": alice_hand_id, "action": "stand"},
                ).status_code,
                200,
            )
            complete = client.post(
                "/rounds/round-1/actions",
                json={"player_id": "bot-1", "hand_id": bot_hand_id, "action": "stand"},
            )
            self.assertEqual(complete.status_code, 200)
            self.assertEqual(complete.json()["phase"], "complete")

        with TestClient(create_app(settings)) as client:
            player = client.get("/players/bot-1")
            self.assertEqual(player.status_code, 200)
            self.assertEqual(player.json()["metadata"], {"strategy": "baseline"})

            stats = client.get("/players/alice/stats")
            self.assertEqual(stats.status_code, 200)
            self.assertEqual(stats.json()["stats"]["wins"], 1)

            round_snapshot = client.get("/rounds/round-1")
            self.assertEqual(round_snapshot.status_code, 200)
            self.assertEqual(round_snapshot.json()["phase"], "complete")
            self.assertTrue(round_snapshot.json()["dealer"]["hole_card_revealed"])

            events = client.get("/rounds/round-1/events")
            self.assertEqual(events.status_code, 200)
            self.assertEqual(events.json()["count"], events.json()["total_count"])
            self.assertEqual(events.json()["events"][-1]["event_type"], "round_settled")

            table = client.get("/tables/table-main")
            self.assertEqual(table.status_code, 200)
            self.assertIsNone(table.json()["active_round_id"])
            self.assertEqual(table.json()["seats"][0]["bankroll"], 220)
            self.assertEqual(table.json()["seats"][1]["bankroll"], 330)

            leaderboard = client.get("/leaderboard")
            self.assertEqual(leaderboard.status_code, 200)
            self.assertEqual([entry["player_id"] for entry in leaderboard.json()["entries"][:2]], ["bot-1", "alice"])

    def test_bet_request_ids_persist_across_restart_and_replay_original_response(self) -> None:
        settings = self.build_settings("bet-request-id")

        with TestClient(create_app(settings)) as client:
            self.seed_two_player_shoe(client)
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
                        "player_id": "bot-1",
                        "display_name": "Bot One",
                        "participant_type": "ai",
                        "starting_bankroll": 300,
                    },
                ).status_code,
                201,
            )
            self.assertEqual(client.post("/tables", json={"table_id": "table-bet-id", "seat_count": 2}).status_code, 201)
            self.assertEqual(client.post("/tables/table-bet-id/seats/1/join", json={"player_id": "alice"}).status_code, 200)
            self.assertEqual(client.post("/tables/table-bet-id/seats/2/join", json={"player_id": "bot-1"}).status_code, 200)
            self.assertEqual(client.post("/tables/table-bet-id/rounds", json={"round_id": "round-bet-id"}).status_code, 201)

            first_bet = client.post(
                "/rounds/round-bet-id/bets",
                json={"player_id": "alice", "amount": 20, "request_id": "alice-bet-1", "expected_version": 0},
            )
            self.assertEqual(first_bet.status_code, 200)
            self.assertEqual(first_bet.json()["phase"], "waiting_for_bets")
            self.assertEqual(first_bet.json()["version"], 1)

            self.assertEqual(
                client.post(
                    "/rounds/round-bet-id/bets",
                    json={"player_id": "bot-1", "amount": 30, "expected_version": 1},
                ).status_code,
                200,
            )

        with TestClient(create_app(settings)) as client:
            current_round = client.get("/rounds/round-bet-id")
            self.assertEqual(current_round.status_code, 200)
            self.assertEqual(current_round.json()["phase"], "player_turns")
            self.assertEqual(current_round.json()["version"], 2)

            replayed_bet = client.post(
                "/rounds/round-bet-id/bets",
                json={"player_id": "alice", "amount": 20, "request_id": "alice-bet-1", "expected_version": 999},
            )
            self.assertEqual(replayed_bet.status_code, 200)
            self.assertEqual(replayed_bet.json(), first_bet.json())
            self.assertEqual(replayed_bet.json()["version"], 1)

            mismatched_retry = client.post(
                "/rounds/round-bet-id/bets",
                json={"player_id": "alice", "amount": 25, "request_id": "alice-bet-1", "expected_version": 999},
            )
            self.assertEqual(mismatched_retry.status_code, 409)
            self.assertIn("alice-bet-1", mismatched_retry.json()["detail"])
            self.assertIn("different payload", mismatched_retry.json()["detail"])

    def test_action_request_ids_persist_across_restart_and_replay_original_response(self) -> None:
        settings = self.build_settings("action-request-id")

        with TestClient(create_app(settings)) as client:
            self.seed_two_player_shoe(client)
            round_state = self.create_two_player_round(client, table_id="table-action-id", round_id="round-action-id")
            alice_hand_id = round_state["participants"][0]["hands"][0]["hand_id"]
            bot_hand_id = round_state["participants"][1]["hands"][0]["hand_id"]

            first_action = client.post(
                "/rounds/round-action-id/actions",
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

            complete = client.post(
                "/rounds/round-action-id/actions",
                json={"player_id": "bot-1", "hand_id": bot_hand_id, "action": "stand", "expected_version": 3},
            )
            self.assertEqual(complete.status_code, 200)
            self.assertEqual(complete.json()["phase"], "complete")
            self.assertEqual(complete.json()["version"], 4)

        with TestClient(create_app(settings)) as client:
            current_round = client.get("/rounds/round-action-id")
            self.assertEqual(current_round.status_code, 200)
            self.assertEqual(current_round.json()["phase"], "complete")
            self.assertEqual(current_round.json()["version"], 4)

            replayed_action = client.post(
                "/rounds/round-action-id/actions",
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
            self.assertEqual(replayed_action.json()["version"], 3)

            mismatched_retry = client.post(
                "/rounds/round-action-id/actions",
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

    def test_completed_round_keeps_table_shoe_across_restart_and_preserves_round_snapshot(self) -> None:
        settings = self.build_settings("completed-round-shoe")
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

        with TestClient(create_app(settings)) as client:
            client.app.state.game_service.queue_test_shoe(persistent_shoe)
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
                        "table_id": "table-shoe",
                        "seat_count": 1,
                        "rules": {"deck_count": 1},
                    },
                ).status_code,
                201,
            )
            self.assertEqual(client.post("/tables/table-shoe/seats/1/join", json={"player_id": "solo"}).status_code, 200)
            self.assertEqual(client.post("/tables/table-shoe/rounds", json={"round_id": "round-shoe-1"}).status_code, 201)

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

        with TestClient(create_app(settings)) as client:
            round_one_snapshot = client.get("/rounds/round-shoe-1")
            self.assertEqual(round_one_snapshot.status_code, 200)
            self.assertEqual(round_one_snapshot.json()["shoe_state"]["cards_remaining"], len(persistent_shoe) - 4)
            self.assertEqual(round_one_snapshot.json()["shoe_state"]["shuffle_count"], 1)

            table = client.get("/tables/table-shoe")
            self.assertEqual(table.status_code, 200)
            self.assertEqual(table.json()["shoe_state"]["cards_remaining"], len(persistent_shoe) - 4)
            self.assertEqual(table.json()["shoe_state"]["shuffle_count"], 1)

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
            self.assertEqual(round_two.json()["shoe_state"]["cards_remaining"], len(persistent_shoe) - 8)
            self.assertEqual(round_two.json()["shoe_state"]["shuffle_count"], 1)

            preserved_snapshot = client.get("/rounds/round-shoe-1")
            self.assertEqual(preserved_snapshot.status_code, 200)
            self.assertEqual(preserved_snapshot.json()["shoe_state"]["cards_remaining"], len(persistent_shoe) - 4)
            self.assertEqual(preserved_snapshot.json()["shoe_state"]["shuffle_count"], 1)

    def test_round_waiting_for_remaining_bets_restores_and_can_continue(self) -> None:
        settings = self.build_settings("pending-bets")

        with TestClient(create_app(settings)) as client:
            self.seed_two_player_shoe(client)
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
                        "player_id": "bot-1",
                        "display_name": "Bot One",
                        "participant_type": "ai",
                        "starting_bankroll": 300,
                    },
                ).status_code,
                201,
            )
            self.assertEqual(client.post("/tables", json={"table_id": "table-pending", "seat_count": 2}).status_code, 201)
            self.assertEqual(client.post("/tables/table-pending/seats/1/join", json={"player_id": "alice"}).status_code, 200)
            self.assertEqual(client.post("/tables/table-pending/seats/2/join", json={"player_id": "bot-1"}).status_code, 200)
            self.assertEqual(client.post("/tables/table-pending/rounds", json={"round_id": "round-pending"}).status_code, 201)

            alice_bet = client.post(
                "/rounds/round-pending/bets",
                json={"player_id": "alice", "amount": 20},
            )
            self.assertEqual(alice_bet.status_code, 200)

            payload = alice_bet.json()
            alice_hand_id = payload["participants"][0]["hands"][0]["hand_id"]

            self.assertEqual(payload["phase"], "waiting_for_bets")
            self.assertEqual(payload["betting"]["pending_player_ids"], ["bot-1"])

        with TestClient(create_app(settings)) as client:
            restored_round = client.get("/rounds/round-pending")
            self.assertEqual(restored_round.status_code, 200)

            payload = restored_round.json()
            self.assertEqual(payload["phase"], "waiting_for_bets")
            self.assertEqual(payload["betting"]["pending_player_ids"], ["bot-1"])
            self.assertEqual(payload["participants"][0]["hands"][0]["hand_id"], alice_hand_id)
            self.assertEqual(payload["participants"][1]["hands"], [])
            self.assertEqual(
                payload["betting"]["accepted_bets"],
                [
                    {
                        "player_id": "alice",
                        "seat_number": 1,
                        "bet": {"amount": 20, "currency": "USD"},
                    }
                ],
            )

            table = client.get("/tables/table-pending")
            self.assertEqual(table.status_code, 200)
            self.assertEqual(table.json()["active_round_id"], "round-pending")
            self.assertEqual(table.json()["seats"][0]["active_hand_ids"], [alice_hand_id])
            self.assertEqual(table.json()["seats"][1]["active_hand_ids"], [])

            events = client.get("/rounds/round-pending/events")
            self.assertEqual(events.status_code, 200)
            self.assertEqual([event["event_type"] for event in events.json()["events"]], ["round_started", "bet_placed"])

            bot_bet = client.post(
                "/rounds/round-pending/bets",
                json={"player_id": "bot-1", "amount": 30},
            )
            self.assertEqual(bot_bet.status_code, 200)
            self.assertEqual(bot_bet.json()["phase"], "player_turns")
            self.assertEqual(bot_bet.json()["dealer"]["cards"][1], {"is_hidden": True})

    def test_active_round_restores_hidden_state_and_can_continue(self) -> None:
        settings = self.build_settings("active-round")

        with TestClient(create_app(settings)) as client:
            self.seed_two_player_shoe(client)
            round_state = self.create_two_player_round(client, table_id="table-active", round_id="round-active")
            self.assertEqual(round_state["phase"], "player_turns")
            self.assertEqual(round_state["dealer"]["cards"][1], {"is_hidden": True})
            alice_hand_id = round_state["participants"][0]["hands"][0]["hand_id"]
            bot_hand_id = round_state["participants"][1]["hands"][0]["hand_id"]

        with TestClient(create_app(settings)) as client:
            restored_round = client.get("/rounds/round-active")
            self.assertEqual(restored_round.status_code, 200)
            self.assertEqual(restored_round.json()["phase"], "player_turns")
            self.assertEqual(restored_round.json()["next_request"]["player_id"], "alice")
            self.assertEqual(restored_round.json()["dealer"]["cards"][1], {"is_hidden": True})

            table = client.get("/tables/table-active")
            self.assertEqual(table.status_code, 200)
            self.assertEqual(table.json()["active_round_id"], "round-active")
            self.assertTrue(table.json()["seats"][0]["active_hand_ids"])
            self.assertTrue(table.json()["seats"][1]["active_hand_ids"])

            events = client.get("/rounds/round-active/events")
            self.assertEqual(events.status_code, 200)
            self.assertEqual(
                [event["event_type"] for event in events.json()["events"]],
                ["round_started", "bet_placed", "bet_placed", "initial_cards_dealt"],
            )
            self.assertEqual(events.json()["events"][-1]["payload"]["round"]["dealer"]["cards"][1], {"is_hidden": True})

            self.assertEqual(
                client.post(
                    "/rounds/round-active/actions",
                    json={"player_id": "alice", "hand_id": alice_hand_id, "action": "stand"},
                ).status_code,
                200,
            )
            complete = client.post(
                "/rounds/round-active/actions",
                json={"player_id": "bot-1", "hand_id": bot_hand_id, "action": "stand"},
            )
            self.assertEqual(complete.status_code, 200)
            self.assertEqual(complete.json()["phase"], "complete")

    def test_surrendered_hand_persists_mid_round_and_restores_with_hidden_dealer(self) -> None:
        settings = self.build_settings("surrender-round")

        with TestClient(create_app(settings)) as client:
            self.seed_surrender_shoe(client)
            round_state = self.create_two_player_round(client, table_id="table-surrender", round_id="round-surrender")
            alice_hand_id = round_state["participants"][0]["hands"][0]["hand_id"]

            surrender = client.post(
                "/rounds/round-surrender/actions",
                json={"player_id": "alice", "hand_id": alice_hand_id, "action": "surrender"},
            )
            self.assertEqual(surrender.status_code, 200)
            self.assertEqual(surrender.json()["phase"], "player_turns")
            self.assertEqual(surrender.json()["dealer"]["cards"][1], {"is_hidden": True})

        with TestClient(create_app(settings)) as client:
            restored_round = client.get("/rounds/round-surrender")
            self.assertEqual(restored_round.status_code, 200)

            payload = restored_round.json()
            alice_hand = payload["participants"][0]["hands"][0]
            bot_hand_id = payload["participants"][1]["hands"][0]["hand_id"]

            self.assertEqual(payload["phase"], "player_turns")
            self.assertEqual(payload["next_request"]["player_id"], "bot-1")
            self.assertEqual(payload["dealer"]["cards"][1], {"is_hidden": True})
            self.assertEqual(alice_hand["status"], "complete")
            self.assertEqual(alice_hand["resolution"]["reason"], "surrender")
            self.assertEqual(alice_hand["resolution"]["net_change"], -10)

            events = client.get("/rounds/round-surrender/events")
            self.assertEqual(events.status_code, 200)
            self.assertEqual(events.json()["events"][-1]["event_type"], "player_action")
            self.assertEqual(events.json()["events"][-1]["payload"]["action"], "surrender")
            self.assertEqual(
                events.json()["events"][-1]["payload"]["resulting_hands"][0]["resolution"]["reason"],
                "surrender",
            )

            complete = client.post(
                "/rounds/round-surrender/actions",
                json={"player_id": "bot-1", "hand_id": bot_hand_id, "action": "stand"},
            )
            self.assertEqual(complete.status_code, 200)
            self.assertEqual(complete.json()["phase"], "complete")

        with TestClient(create_app(settings)) as client:
            round_snapshot = client.get("/rounds/round-surrender")
            self.assertEqual(round_snapshot.status_code, 200)
            self.assertEqual(round_snapshot.json()["phase"], "complete")
            self.assertEqual(
                round_snapshot.json()["participants"][0]["hands"][0]["resolution"]["reason"],
                "surrender",
            )

            stats = client.get("/players/alice/stats")
            self.assertEqual(stats.status_code, 200)
            self.assertEqual(stats.json()["stats"]["losses"], 1)
            self.assertEqual(stats.json()["stats"]["action_counts"]["surrender"], 1)
            self.assertEqual(stats.json()["stats"]["bankroll_delta"], -10)

    def test_insurance_action_persists_and_restores_without_revealing_hole_card(self) -> None:
        settings = self.build_settings("insurance-round")

        with TestClient(create_app(settings)) as client:
            self.seed_insurance_shoe(client)
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
            round_state = client.post("/rounds/round-insurance/bets", json={"player_id": "bot-1", "amount": 20})
            self.assertEqual(round_state.status_code, 200)
            self.assertEqual(round_state.json()["phase"], "player_turns")
            alice_hand_id = round_state.json()["participants"][0]["hands"][0]["hand_id"]

            insurance = client.post(
                "/rounds/round-insurance/actions",
                json={"player_id": "alice", "hand_id": alice_hand_id, "action": "insurance"},
            )
            self.assertEqual(insurance.status_code, 200)
            self.assertEqual(insurance.json()["phase"], "player_turns")
            self.assertEqual(insurance.json()["next_request"]["player_id"], "alice")
            self.assertEqual(insurance.json()["next_request"]["hand_id"], alice_hand_id)
            self.assertEqual(insurance.json()["dealer"]["cards"][1], {"is_hidden": True})

        with TestClient(create_app(settings)) as client:
            restored_round = client.get("/rounds/round-insurance")
            self.assertEqual(restored_round.status_code, 200)

            payload = restored_round.json()
            alice_hand_id = payload["participants"][0]["hands"][0]["hand_id"]
            bot_hand_id = payload["participants"][1]["hands"][0]["hand_id"]

            self.assertEqual(payload["phase"], "player_turns")
            self.assertEqual(payload["next_request"]["type"], "action")
            self.assertEqual(payload["next_request"]["player_id"], "alice")
            self.assertEqual(payload["dealer"]["cards"][1], {"is_hidden": True})
            self.assertEqual(payload["next_request"]["hand_id"], alice_hand_id)
            self.assertNotIn("insurance", payload["next_request"]["legal_actions"])
            self.assertNotIn("insurance", payload["participants"][0])

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

        with TestClient(create_app(settings)) as client:
            round_snapshot = client.get("/rounds/round-insurance")
            self.assertEqual(round_snapshot.status_code, 200)
            self.assertEqual(round_snapshot.json()["phase"], "complete")
            self.assertNotIn("insurance", round_snapshot.json()["participants"][0])

            stats = client.get("/players/alice/stats")
            self.assertEqual(stats.status_code, 200)
            self.assertEqual(stats.json()["stats"]["bankroll_delta"], -30)
            self.assertEqual(stats.json()["stats"]["losses"], 1)
            self.assertEqual(stats.json()["stats"]["action_counts"]["insurance"], 1)

    def test_service_reloads_state_from_file_backed_repository(self) -> None:
        settings = self.build_settings("service-round")
        service = GameService(repository=SqliteGameRepository(settings.database_url))
        service.queue_test_shoe(
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

        service.register_player(
            CreatePlayerRequest(
                player_id="alice",
                display_name="Alice",
                participant_type="human",
                starting_bankroll=200,
            )
        )
        service.register_player(
            CreatePlayerRequest(
                player_id="bot-1",
                display_name="Bot One",
                participant_type="ai",
                starting_bankroll=300,
                metadata={"strategy": "baseline"},
            )
        )
        service.create_table(CreateTableRequest(table_id="table-service", seat_count=2))
        service.join_table_seat("table-service", 1, SeatJoinRequest(player_id="alice"))
        service.join_table_seat("table-service", 2, SeatJoinRequest(player_id="bot-1"))
        service.start_round("table-service", StartRoundRequest(round_id="round-service"))
        service.place_bet("round-service", BetRequest(player_id="alice", amount=20))
        round_state = service.place_bet("round-service", BetRequest(player_id="bot-1", amount=30))
        alice_hand_id = round_state["participants"][0]["hands"][0]["hand_id"]
        bot_hand_id = round_state["participants"][1]["hands"][0]["hand_id"]
        self.assertEqual(round_state["shoe_state"]["cards_remaining"], 1)
        self.assertEqual(round_state["shoe_state"]["shuffle_count"], 1)

        restored_service = GameService(repository=SqliteGameRepository(settings.database_url))
        restored_round = restored_service.get_round("round-service")
        self.assertEqual(restored_round["phase"], "player_turns")
        self.assertEqual(restored_round["dealer"]["cards"][1], {"is_hidden": True})
        self.assertEqual(restored_round["shoe_state"]["cards_remaining"], 1)
        self.assertEqual(restored_round["shoe_state"]["shuffle_count"], 1)
        self.assertEqual(restored_service.get_table("table-service")["shoe_state"]["cards_remaining"], 1)
        self.assertEqual(restored_service.get_table("table-service")["shoe_state"]["shuffle_count"], 1)

        restored_service.apply_action(
            "round-service",
            ActionRequest(player_id="alice", hand_id=alice_hand_id, action="stand"),
        )
        complete = restored_service.apply_action(
            "round-service",
            ActionRequest(player_id="bot-1", hand_id=bot_hand_id, action="stand"),
        )
        self.assertEqual(complete["phase"], "complete")
        self.assertEqual(complete["shoe_state"]["cards_remaining"], 0)
        self.assertEqual(complete["shoe_state"]["shuffle_count"], 1)
        self.assertEqual(restored_service.get_player_stats("alice")["stats"]["wins"], 1)

    def test_split_round_restores_active_hands_and_turn_after_restart(self) -> None:
        settings = self.build_settings("split-active-round")

        with TestClient(create_app(settings)) as client:
            client.app.state.game_service.queue_test_shoe(
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
            first_hand_id = payload["participants"][0]["hands"][0]["hand_id"]
            second_hand_id = payload["participants"][0]["hands"][1]["hand_id"]
            self.assertEqual(payload["next_request"]["hand_id"], second_hand_id)

        with TestClient(create_app(settings)) as client:
            restored_round = client.get("/rounds/round-split")
            self.assertEqual(restored_round.status_code, 200)

            payload = restored_round.json()
            self.assertEqual(payload["phase"], "player_turns")
            self.assertEqual(payload["dealer"]["cards"][1], {"is_hidden": True})
            self.assertEqual(
                [hand["hand_id"] for hand in payload["participants"][0]["hands"]],
                [first_hand_id, second_hand_id],
            )
            self.assertEqual(payload["participants"][0]["hands"][0]["status"], "standing")
            self.assertEqual(payload["participants"][0]["hands"][1]["status"], "active")
            self.assertEqual(payload["next_request"]["hand_id"], second_hand_id)

            table = client.get("/tables/table-split")
            self.assertEqual(table.status_code, 200)
            self.assertEqual(table.json()["active_round_id"], "round-split")
            self.assertEqual(table.json()["seats"][0]["active_hand_ids"], [first_hand_id, second_hand_id])

            complete = client.post(
                "/rounds/round-split/actions",
                json={"player_id": "alice", "hand_id": second_hand_id, "action": "stand"},
            )
            self.assertEqual(complete.status_code, 200)
            self.assertEqual(complete.json()["phase"], "complete")


if __name__ == "__main__":
    unittest.main()
