from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from typing import Mapping

from blackjack_ai.benchmark.client import BenchmarkApiClient
from blackjack_ai.benchmark.client import BenchmarkApiError
from blackjack_ai.benchmark.client import TransportLike
from blackjack_ai.engine import ActionType
from blackjack_ai.engine import ParticipantType


JsonDict = dict[str, Any]
GameplayError = BenchmarkApiError


def _enum_value(value: ParticipantType | ActionType | str) -> str:
    if isinstance(value, (ParticipantType, ActionType)):
        return value.value
    return str(value)


class GameplayClient:
    def __init__(self, api_client: BenchmarkApiClient) -> None:
        self._api_client = api_client
        self._player_tokens: dict[str, str] = {}

    @classmethod
    def from_base_url(cls, base_url: str, *, timeout: float = 10.0) -> GameplayClient:
        return cls(BenchmarkApiClient.from_base_url(base_url, timeout=timeout))

    @classmethod
    def from_transport(cls, transport: TransportLike) -> GameplayClient:
        return cls(BenchmarkApiClient(transport))

    def close(self) -> None:
        self._api_client.close()

    def __enter__(self) -> GameplayClient:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def create_player(
        self,
        display_name: str,
        *,
        player_id: str | None = None,
        participant_type: ParticipantType | str = ParticipantType.HUMAN,
        starting_bankroll: int = 1000,
        metadata: Mapping[str, Any] | None = None,
    ) -> JsonDict:
        response = self._api_client.create_player(
            {
                "player_id": player_id,
                "display_name": display_name,
                "participant_type": _enum_value(participant_type),
                "starting_bankroll": starting_bankroll,
                "metadata": dict(metadata or {}),
            }
        )
        resolved_player_id = response.get("player_id")
        player_token = response.get("player_token")
        if isinstance(resolved_player_id, str) and isinstance(player_token, str):
            self._player_tokens[resolved_player_id] = player_token
        return response

    def get_player(self, player_id: str) -> JsonDict:
        return self._api_client.request_json("GET", f"/players/{player_id}")

    def get_player_stats(self, player_id: str) -> JsonDict:
        return self._api_client.get_player_stats(player_id)

    def create_table(
        self,
        *,
        table_id: str | None = None,
        seat_count: int = 5,
        rules: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> JsonDict:
        return self._api_client.create_table(
            {
                "table_id": table_id,
                "seat_count": seat_count,
                "rules": dict(rules) if rules is not None else None,
                "metadata": dict(metadata or {}),
            }
        )

    def get_table(self, table_id: str) -> JsonDict:
        return self._api_client.get_table(table_id)

    def seat_player(
        self,
        table_id: str,
        seat_number: int,
        player_id: str,
        *,
        bankroll: int | None = None,
        player_token: str | None = None,
    ) -> JsonDict:
        payload: JsonDict = {"player_id": player_id}
        if bankroll is not None:
            payload["bankroll"] = bankroll
        return self._api_client.join_table_seat(
            table_id,
            seat_number,
            payload,
            player_token=self._player_token(player_id, player_token),
        )

    def leave_seat(
        self,
        table_id: str,
        seat_number: int,
        player_id: str,
        *,
        player_token: str | None = None,
    ) -> JsonDict:
        return self._api_client.leave_table_seat(
            table_id,
            seat_number,
            {"player_id": player_id},
            player_token=self._player_token(player_id, player_token),
        )

    def start_round(self, table_id: str, *, round_id: str | None = None) -> RoundSession:
        payload = {"round_id": round_id} if round_id is not None else None
        return RoundSession(self, self._api_client.start_round(table_id, payload))

    def get_round(self, round_id: str) -> RoundSession:
        return RoundSession(self, self._api_client.get_round(round_id))

    def get_round_events(
        self,
        round_id: str,
        *,
        limit: int | None = None,
        after_sequence: int | None = None,
    ) -> JsonDict:
        return self._api_client.get_round_events(round_id, limit=limit, after_sequence=after_sequence)

    def get_leaderboard(self, *, participant_type: ParticipantType | str | None = None) -> JsonDict:
        resolved_participant_type = _enum_value(participant_type) if participant_type is not None else None
        return self._api_client.get_leaderboard(participant_type=resolved_participant_type)

    def bet(
        self,
        round_id: str,
        *,
        player_id: str,
        amount: int,
        player_token: str | None = None,
        request_id: str | None = None,
        expected_version: int | None = None,
    ) -> RoundSession:
        payload: JsonDict = {"player_id": player_id, "amount": amount}
        if request_id is not None:
            payload["request_id"] = request_id
        if expected_version is not None:
            payload["expected_version"] = expected_version
        return RoundSession(
            self,
            self._api_client.place_bet(
                round_id,
                payload,
                player_token=self._player_token(player_id, player_token),
            ),
        )._advance()

    def action(
        self,
        round_id: str,
        *,
        player_id: str,
        hand_id: str,
        action: ActionType | str,
        player_token: str | None = None,
        request_id: str | None = None,
        expected_version: int | None = None,
    ) -> RoundSession:
        payload: JsonDict = {
            "player_id": player_id,
            "hand_id": hand_id,
            "action": _enum_value(action),
        }
        if request_id is not None:
            payload["request_id"] = request_id
        if expected_version is not None:
            payload["expected_version"] = expected_version
        return RoundSession(
            self,
            self._api_client.apply_action(
                round_id,
                payload,
                player_token=self._player_token(player_id, player_token),
            ),
        )._advance()

    def insure(
        self,
        round_id: str,
        *,
        player_id: str,
        hand_id: str,
        player_token: str | None = None,
        request_id: str | None = None,
        expected_version: int | None = None,
    ) -> RoundSession:
        return self.action(
            round_id,
            player_id=player_id,
            hand_id=hand_id,
            action=ActionType.INSURANCE,
            player_token=player_token,
            request_id=request_id,
            expected_version=expected_version,
        )

    def surrender(
        self,
        round_id: str,
        *,
        player_id: str,
        hand_id: str,
        player_token: str | None = None,
        request_id: str | None = None,
        expected_version: int | None = None,
    ) -> RoundSession:
        return self.action(
            round_id,
            player_id=player_id,
            hand_id=hand_id,
            action=ActionType.SURRENDER,
            player_token=player_token,
            request_id=request_id,
            expected_version=expected_version,
        )

    def _player_token(self, player_id: str, player_token: str | None) -> str | None:
        return player_token if player_token is not None else self._player_tokens.get(player_id)


@dataclass(slots=True)
class RoundSession:
    client: GameplayClient
    state: JsonDict
    max_wait_polls: int = 5

    @property
    def round_id(self) -> str:
        return str(self.state["round_id"])

    @property
    def phase(self) -> str:
        return str(self.state.get("phase", ""))

    @property
    def next_request(self) -> JsonDict:
        payload = self.state.get("next_request")
        return payload if isinstance(payload, dict) else {}

    @property
    def current_player_id(self) -> str | None:
        player_id = self.next_request.get("player_id")
        return str(player_id) if player_id is not None else None

    @property
    def current_hand_id(self) -> str | None:
        hand_id = self.next_request.get("hand_id")
        return str(hand_id) if hand_id is not None else None

    @property
    def legal_actions(self) -> tuple[str, ...]:
        legal_actions = self.next_request.get("legal_actions", ())
        return tuple(str(action) for action in legal_actions)

    @property
    def version(self) -> int:
        return int(self.state.get("version", 0))

    def refresh(self) -> RoundSession:
        self.state = self.client.get_round(self.round_id).state
        return self._advance()

    def participant(self, player_id: str) -> JsonDict:
        for participant in self.state.get("participants", ()):
            if participant.get("player_id") == player_id:
                return participant
        raise ValueError(f"Player '{player_id}' is not part of round '{self.round_id}'.")

    def hand(self, player_id: str, *, hand_id: str | None = None, hand_index: int | None = None) -> JsonDict:
        participant = self.participant(player_id)
        hands = participant.get("hands", ())

        if hand_id is not None:
            for hand in hands:
                if hand.get("hand_id") == hand_id:
                    return hand
            raise ValueError(f"Hand '{hand_id}' was not found for player '{player_id}'.")

        if hand_index is None:
            raise ValueError("Either hand_id or hand_index must be provided.")

        return hands[hand_index]

    def events(self, *, limit: int | None = None, after_sequence: int | None = None) -> JsonDict:
        return self.client.get_round_events(self.round_id, limit=limit, after_sequence=after_sequence)

    def bet(
        self,
        *,
        player_id: str | None = None,
        amount: int,
        player_token: str | None = None,
        request_id: str | None = None,
        expected_version: int | None = None,
    ) -> RoundSession:
        resolved_player_id = player_id or self._resolve_betting_player_id()
        self.state = self.client.bet(
            self.round_id,
            player_id=resolved_player_id,
            amount=amount,
            player_token=player_token,
            request_id=request_id,
            expected_version=expected_version,
        ).state
        return self

    def action(
        self,
        action: ActionType | str,
        *,
        player_id: str | None = None,
        hand_id: str | None = None,
        player_token: str | None = None,
        request_id: str | None = None,
        expected_version: int | None = None,
    ) -> RoundSession:
        resolved_player_id, resolved_hand_id = self._resolve_action_target(player_id=player_id, hand_id=hand_id)
        self.state = self.client.action(
            self.round_id,
            player_id=resolved_player_id,
            hand_id=resolved_hand_id,
            action=action,
            player_token=player_token,
            request_id=request_id,
            expected_version=expected_version,
        ).state
        return self

    def insure(
        self,
        *,
        player_id: str | None = None,
        player_token: str | None = None,
        request_id: str | None = None,
        expected_version: int | None = None,
    ) -> RoundSession:
        return self.action(
            ActionType.INSURANCE,
            player_id=player_id,
            player_token=player_token,
            request_id=request_id,
            expected_version=expected_version,
        )

    def hit(
        self,
        *,
        player_id: str | None = None,
        hand_id: str | None = None,
        player_token: str | None = None,
        request_id: str | None = None,
        expected_version: int | None = None,
    ) -> RoundSession:
        return self.action(
            ActionType.HIT,
            player_id=player_id,
            hand_id=hand_id,
            player_token=player_token,
            request_id=request_id,
            expected_version=expected_version,
        )

    def stand(
        self,
        *,
        player_id: str | None = None,
        hand_id: str | None = None,
        player_token: str | None = None,
        request_id: str | None = None,
        expected_version: int | None = None,
    ) -> RoundSession:
        return self.action(
            ActionType.STAND,
            player_id=player_id,
            hand_id=hand_id,
            player_token=player_token,
            request_id=request_id,
            expected_version=expected_version,
        )

    def double(
        self,
        *,
        player_id: str | None = None,
        hand_id: str | None = None,
        player_token: str | None = None,
        request_id: str | None = None,
        expected_version: int | None = None,
    ) -> RoundSession:
        return self.action(
            ActionType.DOUBLE,
            player_id=player_id,
            hand_id=hand_id,
            player_token=player_token,
            request_id=request_id,
            expected_version=expected_version,
        )

    def split(
        self,
        *,
        player_id: str | None = None,
        hand_id: str | None = None,
        player_token: str | None = None,
        request_id: str | None = None,
        expected_version: int | None = None,
    ) -> RoundSession:
        return self.action(
            ActionType.SPLIT,
            player_id=player_id,
            hand_id=hand_id,
            player_token=player_token,
            request_id=request_id,
            expected_version=expected_version,
        )

    def surrender(
        self,
        *,
        player_id: str | None = None,
        hand_id: str | None = None,
        player_token: str | None = None,
        request_id: str | None = None,
        expected_version: int | None = None,
    ) -> RoundSession:
        return self.action(
            ActionType.SURRENDER,
            player_id=player_id,
            hand_id=hand_id,
            player_token=player_token,
            request_id=request_id,
            expected_version=expected_version,
        )

    def _resolve_betting_player_id(self) -> str:
        if self.next_request.get("type") != "bet":
            raise ValueError(f"Round '{self.round_id}' is not waiting for a bet.")
        pending_player_ids = [str(player_id) for player_id in self.next_request.get("pending_player_ids", ())]
        if len(pending_player_ids) == 1:
            return pending_player_ids[0]
        if not pending_player_ids:
            raise ValueError(f"Round '{self.round_id}' has no pending bet requests.")
        raise ValueError("player_id is required when more than one player is waiting to bet.")

    def _resolve_action_target(self, *, player_id: str | None, hand_id: str | None) -> tuple[str, str]:
        if self.next_request.get("type") != "action":
            raise ValueError(f"Round '{self.round_id}' is not waiting for a player action.")

        resolved_player_id = player_id or self.current_player_id
        resolved_hand_id = hand_id or self.current_hand_id
        if resolved_player_id is None or resolved_hand_id is None:
            raise ValueError(f"Round '{self.round_id}' does not expose a current player hand.")
        return resolved_player_id, resolved_hand_id

    def _advance(self) -> RoundSession:
        stagnant_wait_polls = 0
        last_wait_signature: tuple[Any, ...] | None = None

        while self.next_request.get("type") == "wait":
            wait_signature = (
                self.state.get("phase"),
                self.state.get("version", self.state.get("action_count")),
                repr(self.next_request),
            )
            if wait_signature == last_wait_signature:
                stagnant_wait_polls += 1
            else:
                last_wait_signature = wait_signature
                stagnant_wait_polls = 1

            if stagnant_wait_polls > self.max_wait_polls:
                raise RuntimeError(f"Round '{self.round_id}' did not progress after repeated wait states.")

            self.state = self.client.get_round(self.round_id).state

        return self
