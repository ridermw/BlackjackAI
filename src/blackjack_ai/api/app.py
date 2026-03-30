from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi import Query
from fastapi import Request
from fastapi import status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from blackjack_ai.api.schemas import ActionRequest
from blackjack_ai.api.schemas import BetRequest
from blackjack_ai.api.schemas import CreatePlayerRequest
from blackjack_ai.api.schemas import CreateTableRequest
from blackjack_ai.api.schemas import SeatLeaveRequest
from blackjack_ai.api.schemas import SeatJoinRequest
from blackjack_ai.api.schemas import StartRoundRequest
from blackjack_ai.api.service import ApiServiceError
from blackjack_ai.api.service import GameService
from blackjack_ai.config import Settings
from blackjack_ai.config import get_settings
from blackjack_ai.engine import ParticipantType
from blackjack_ai.db import database_backend
from blackjack_ai.db import initialize_database
from blackjack_ai.db import probe_database
from blackjack_ai.repository import SqliteGameRepository


class DatabaseStatus(BaseModel):
    backend: str
    connected: bool


class ServiceStatus(BaseModel):
    status: str
    app_name: str
    environment: str
    database: DatabaseStatus


def create_app(settings: Settings | None = None, game_service: GameService | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    resolved_game_service = game_service
    if resolved_game_service is None:
        if resolved_settings.database_url != "sqlite:///:memory:":
            resolved_game_service = GameService(repository=SqliteGameRepository(resolved_settings.database_url))
        else:
            resolved_game_service = GameService()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        initialize_database(app.state.settings.database_url)
        yield

    app = FastAPI(title=resolved_settings.app_name, lifespan=lifespan)
    app.state.settings = resolved_settings
    app.state.game_service = resolved_game_service

    @app.exception_handler(ApiServiceError)
    async def handle_service_error(_: Request, exc: ApiServiceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.get("/health", response_model=ServiceStatus, tags=["service"])
    @app.get("/status", response_model=ServiceStatus, tags=["service"])
    def read_service_status(request: Request) -> ServiceStatus:
        probe_database(request.app.state.settings.database_url)

        return ServiceStatus(
            status="ok",
            app_name=request.app.state.settings.app_name,
            environment=request.app.state.settings.environment,
            database=DatabaseStatus(
                backend=database_backend(request.app.state.settings.database_url),
                connected=True,
            ),
        )

    @app.post("/players", status_code=status.HTTP_201_CREATED, tags=["players"])
    def create_player(payload: CreatePlayerRequest, request: Request) -> dict[str, Any]:
        return request.app.state.game_service.register_player(payload)

    @app.get("/players/{player_id}", tags=["players"])
    def read_player(player_id: str, request: Request) -> dict[str, Any]:
        return request.app.state.game_service.get_player(player_id)

    @app.get("/players/{player_id}/stats", tags=["players"])
    def read_player_stats(player_id: str, request: Request) -> dict[str, Any]:
        return request.app.state.game_service.get_player_stats(player_id)

    @app.post("/tables", status_code=status.HTTP_201_CREATED, tags=["tables"])
    def create_table(payload: CreateTableRequest, request: Request) -> dict[str, Any]:
        return request.app.state.game_service.create_table(payload)

    @app.get("/tables/{table_id}", tags=["tables"])
    def read_table(table_id: str, request: Request) -> dict[str, Any]:
        return request.app.state.game_service.get_table(table_id)

    @app.post("/tables/{table_id}/seats/{seat_number}/join", tags=["tables"])
    def join_table_seat(table_id: str, seat_number: int, payload: SeatJoinRequest, request: Request) -> dict[str, Any]:
        request.app.state.game_service.require_player_token(payload.player_id, request.headers.get("X-Player-Token"))
        return request.app.state.game_service.join_table_seat(table_id, seat_number, payload)

    @app.post("/tables/{table_id}/seats/{seat_number}/leave", tags=["tables"])
    def leave_table_seat(table_id: str, seat_number: int, payload: SeatLeaveRequest, request: Request) -> dict[str, Any]:
        request.app.state.game_service.require_player_token(payload.player_id, request.headers.get("X-Player-Token"))
        return request.app.state.game_service.leave_table_seat(table_id, seat_number, payload)

    @app.post("/tables/{table_id}/rounds", status_code=status.HTTP_201_CREATED, tags=["rounds"])
    def start_round(table_id: str, request: Request, payload: StartRoundRequest | None = None) -> dict[str, Any]:
        return request.app.state.game_service.start_round(table_id, payload)

    @app.get("/rounds/{round_id}", tags=["rounds"])
    def read_round(round_id: str, request: Request) -> dict[str, Any]:
        return request.app.state.game_service.get_round(round_id)

    @app.post("/rounds/{round_id}/bets", tags=["rounds"])
    def place_bet(round_id: str, payload: BetRequest, request: Request) -> dict[str, Any]:
        request.app.state.game_service.require_player_token(payload.player_id, request.headers.get("X-Player-Token"))
        return request.app.state.game_service.place_bet(round_id, payload)

    @app.post("/rounds/{round_id}/actions", tags=["rounds"])
    def apply_action(round_id: str, payload: ActionRequest, request: Request) -> dict[str, Any]:
        request.app.state.game_service.require_player_token(payload.player_id, request.headers.get("X-Player-Token"))
        return request.app.state.game_service.apply_action(round_id, payload)

    @app.get("/rounds/{round_id}/events", tags=["rounds"])
    def read_round_events(
        round_id: str,
        request: Request,
        limit: int | None = Query(default=None, ge=1),
        after_sequence: int | None = Query(default=None, ge=0),
    ) -> dict[str, Any]:
        return request.app.state.game_service.get_round_events(
            round_id,
            limit=limit,
            after_sequence=after_sequence,
        )

    @app.get("/leaderboard", tags=["leaderboard"])
    def read_leaderboard(
        request: Request,
        participant_type: ParticipantType | None = Query(default=None),
    ) -> dict[str, Any]:
        return request.app.state.game_service.get_leaderboard(participant_type=participant_type)

    return app
