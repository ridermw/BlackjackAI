from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from typing import Mapping
from typing import Protocol

import httpx


JsonDict = dict[str, Any]


class ResponseLike(Protocol):
    status_code: int
    text: str
    content: bytes

    def json(self) -> Any: ...


class TransportLike(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
    ) -> ResponseLike: ...

    def close(self) -> None: ...


@dataclass(slots=True)
class BenchmarkApiError(Exception):
    method: str
    path: str
    status_code: int
    detail: str
    body: Any = None

    def __str__(self) -> str:
        return f"{self.method} {self.path} returned {self.status_code}: {self.detail}"


class BenchmarkApiClient:
    def __init__(self, transport: TransportLike) -> None:
        self._transport = transport

    @classmethod
    def from_base_url(cls, base_url: str, *, timeout: float = 10.0) -> BenchmarkApiClient:
        return cls(httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout))

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> BenchmarkApiClient:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def request_json(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> JsonDict:
        response = self._transport.request(method, path, headers=headers, params=params, json=payload)
        body: Any = None
        if response.content:
            try:
                body = response.json()
            except ValueError:
                body = response.text

        if response.status_code >= 400:
            detail = response.text or "API request failed."
            if isinstance(body, dict):
                detail = str(body.get("detail", detail))
            raise BenchmarkApiError(method=method, path=path, status_code=response.status_code, detail=detail, body=body)

        if body is None:
            return {}
        if isinstance(body, dict):
            return body
        raise BenchmarkApiError(
            method=method,
            path=path,
            status_code=response.status_code,
            detail="Expected a JSON object response.",
            body=body,
        )

    def create_player(self, payload: Mapping[str, Any]) -> JsonDict:
        return self.request_json("POST", "/players", payload=payload)

    @staticmethod
    def _player_headers(player_token: str | None) -> JsonDict | None:
        if player_token is None:
            return None
        return {"X-Player-Token": player_token}

    def get_player_stats(self, player_id: str) -> JsonDict:
        return self.request_json("GET", f"/players/{player_id}/stats")

    def create_table(self, payload: Mapping[str, Any]) -> JsonDict:
        return self.request_json("POST", "/tables", payload=payload)

    def get_table(self, table_id: str) -> JsonDict:
        return self.request_json("GET", f"/tables/{table_id}")

    def join_table_seat(
        self,
        table_id: str,
        seat_number: int,
        payload: Mapping[str, Any],
        *,
        player_token: str | None = None,
    ) -> JsonDict:
        return self.request_json(
            "POST",
            f"/tables/{table_id}/seats/{seat_number}/join",
            headers=self._player_headers(player_token),
            payload=payload,
        )

    def leave_table_seat(
        self,
        table_id: str,
        seat_number: int,
        payload: Mapping[str, Any],
        *,
        player_token: str | None = None,
    ) -> JsonDict:
        return self.request_json(
            "POST",
            f"/tables/{table_id}/seats/{seat_number}/leave",
            headers=self._player_headers(player_token),
            payload=payload,
        )

    def start_round(self, table_id: str, payload: Mapping[str, Any] | None = None) -> JsonDict:
        return self.request_json("POST", f"/tables/{table_id}/rounds", payload=payload)

    def get_round(self, round_id: str) -> JsonDict:
        return self.request_json("GET", f"/rounds/{round_id}")

    def place_bet(
        self,
        round_id: str,
        payload: Mapping[str, Any],
        *,
        player_token: str | None = None,
    ) -> JsonDict:
        return self.request_json(
            "POST",
            f"/rounds/{round_id}/bets",
            headers=self._player_headers(player_token),
            payload=payload,
        )

    def apply_action(
        self,
        round_id: str,
        payload: Mapping[str, Any],
        *,
        player_token: str | None = None,
    ) -> JsonDict:
        return self.request_json(
            "POST",
            f"/rounds/{round_id}/actions",
            headers=self._player_headers(player_token),
            payload=payload,
        )

    def get_round_events(
        self,
        round_id: str,
        *,
        limit: int | None = None,
        after_sequence: int | None = None,
    ) -> JsonDict:
        params: JsonDict = {}
        if limit is not None:
            params["limit"] = limit
        if after_sequence is not None:
            params["after_sequence"] = after_sequence
        return self.request_json("GET", f"/rounds/{round_id}/events", params=params)

    def get_leaderboard(self, *, participant_type: str | None = None) -> JsonDict:
        params = {"participant_type": participant_type} if participant_type is not None else None
        return self.request_json("GET", "/leaderboard", params=params)
