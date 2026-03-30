from __future__ import annotations

import json
from contextlib import contextmanager
from random import Random
from typing import Any
from typing import Iterator
from typing import Mapping

import pytest

from blackjack_ai.api.service import GameService
from blackjack_ai.benchmark.__main__ import main
from blackjack_ai.benchmark.harness import BenchmarkHarness
from blackjack_ai.benchmark.harness import BenchmarkReport
from blackjack_ai.benchmark.harness import CompetitorResult
from blackjack_ai.benchmark.harness import SeriesCompetitorResult
from blackjack_ai.benchmark.harness import _build_series_results
from blackjack_ai.benchmark.harness import local_api_client
from blackjack_ai.benchmark.harness import run_benchmark_series


class _StubBenchmarkApiClient:
    def __init__(self, *, start_round_response: Mapping[str, Any], get_round_response: Mapping[str, Any] | None = None) -> None:
        self._start_round_response = dict(start_round_response)
        self._get_round_response = dict(get_round_response) if get_round_response is not None else None
        self._player_tokens: dict[str, str] = {}
        self.get_round_calls = 0

    def create_player(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        player_id = str(payload["player_id"])
        player_token = f"token-{player_id}"
        self._player_tokens[player_id] = player_token
        return {**payload, "player_token": player_token}

    def create_table(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return dict(payload)

    def join_table_seat(
        self,
        table_id: str,
        seat_number: int,
        payload: Mapping[str, Any],
        *,
        player_token: str | None = None,
    ) -> dict[str, Any]:
        player_id = str(payload["player_id"])
        assert player_token == self._player_tokens[player_id]
        return {"table_id": table_id, "seat_number": seat_number, **payload}

    def start_round(self, table_id: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return dict(self._start_round_response)

    def get_round(self, round_id: str) -> dict[str, Any]:
        self.get_round_calls += 1
        if self._get_round_response is None:
            raise AssertionError("get_round should not have been called.")
        return dict(self._get_round_response)

    def get_table(self, table_id: str) -> dict[str, Any]:
        raise AssertionError("get_table should not be reached for failing harness tests.")

    def get_leaderboard(self, *, participant_type: str | None = None) -> dict[str, Any]:
        raise AssertionError("get_leaderboard should not be reached for failing harness tests.")

    def get_player_stats(self, player_id: str) -> dict[str, Any]:
        raise AssertionError("get_player_stats should not be reached for failing harness tests.")


class _RecordingBenchmarkApiClient:
    def __init__(self, *, seed: int | None = None) -> None:
        self.seed = seed
        self._leaderboard_entries: dict[str, dict[str, Any]] = {}
        self._player_tokens: dict[str, str] = {}
        self._tables: dict[str, dict[int, dict[str, int | str] | None]] = {}
        self.seat_assignments_by_table: dict[str, list[tuple[int, str]]] = {}

    def create_player(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        player_id = str(payload["player_id"])
        player_token = f"token-{player_id}"
        self._player_tokens[player_id] = player_token
        self._leaderboard_entries[player_id] = {
            "player_id": player_id,
            "rank": 0,
            "stats": {
                "bankroll_delta": 0,
                "rounds_played": 1,
                "hands_played": 1,
                "wins": 0,
                "pushes": 1,
                "losses": 0,
                "bust_rate": 0.0,
                "average_return_per_hand": 0.0,
            },
        }
        return {**payload, "player_token": player_token}

    def create_table(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        table_id = str(payload["table_id"])
        self._tables[table_id] = {
            seat_number: None for seat_number in range(1, int(payload["seat_count"]) + 1)
        }
        self.seat_assignments_by_table[table_id] = []
        return dict(payload)

    def join_table_seat(
        self,
        table_id: str,
        seat_number: int,
        payload: Mapping[str, Any],
        *,
        player_token: str | None = None,
    ) -> dict[str, Any]:
        player_id = str(payload["player_id"])
        assert player_token == self._player_tokens[player_id]
        bankroll = int(payload["bankroll"])
        self._tables[table_id][seat_number] = {"player_id": player_id, "bankroll": bankroll}
        self.seat_assignments_by_table[table_id].append((seat_number, player_id))
        return {"table_id": table_id, "seat_number": seat_number, **payload}

    def start_round(self, table_id: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        round_id = str(payload["round_id"]) if payload is not None else f"{table_id}-round-0001"
        return {"round_id": round_id, "phase": "complete", "participants": []}

    def get_round(self, round_id: str) -> dict[str, Any]:
        raise AssertionError("get_round should not be reached when the round is already complete.")

    def get_table(self, table_id: str) -> dict[str, Any]:
        seats = [
            {
                "seat_number": seat_number,
                "occupant": None if occupant is None else {"player_id": occupant["player_id"]},
                "bankroll": 0 if occupant is None else int(occupant["bankroll"]),
            }
            for seat_number, occupant in sorted(self._tables[table_id].items())
        ]
        return {"table_id": table_id, "seats": seats}

    def get_leaderboard(self, *, participant_type: str | None = None) -> dict[str, Any]:
        entries = []
        for rank, player_id in enumerate(sorted(self._leaderboard_entries), start=1):
            entry = self._leaderboard_entries[player_id]
            entries.append(
                {
                    "player_id": entry["player_id"],
                    "rank": rank,
                    "stats": dict(entry["stats"]),
                }
            )
        return {"entries": entries}

    def get_player_stats(self, player_id: str) -> dict[str, Any]:
        entry = self._leaderboard_entries[player_id]
        return {"player_id": player_id, "stats": dict(entry["stats"])}


def _series_stats(
    *,
    bankroll_delta: int,
    wins: int,
    pushes: int,
    losses: int,
    action_counts: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    normalized_action_counts = {
        "hit": 0,
        "stand": 0,
        "double": 0,
        "split": 0,
        "surrender": 0,
        "insurance": 0,
    }
    if action_counts is not None:
        normalized_action_counts.update({action: int(count) for action, count in action_counts.items()})
    total_actions = sum(normalized_action_counts.values())
    return {
        "bankroll_delta": bankroll_delta,
        "rounds_played": 1,
        "hands_played": 1,
        "wins": wins,
        "pushes": pushes,
        "losses": losses,
        "bust_rate": 0.0,
        "average_return_per_hand": float(bankroll_delta),
        "action_counts": normalized_action_counts,
        "action_distribution": {
            action: round(count / total_actions, 4) if total_actions else 0.0
            for action, count in normalized_action_counts.items()
        },
    }


def test_harness_runs_multiple_rounds_and_collects_leaderboard_metrics() -> None:
    game_service = GameService(randomizer=Random(7))

    with local_api_client(game_service=game_service) as api_client:
        report = BenchmarkHarness(api_client).run(
            ["conservative", "aggressive"],
            rounds=5,
            starting_bankroll=500,
            rules={"minimum_bet": 10, "maximum_bet": 50},
            benchmark_id="benchmark-test",
        )

    assert report.rounds_completed == 5
    assert report.rounds_requested == 5
    assert report.stopped_early_reason is None
    assert len(report.competitors) == 2
    assert {competitor.strategy_name for competitor in report.competitors} == {"conservative", "aggressive"}
    assert all(competitor.stats["rounds_played"] == 5 for competitor in report.competitors)
    assert all(competitor.final_bankroll > 0 for competitor in report.competitors)

    summary = report.format_summary()
    assert "benchmark-test" in summary
    assert "conservative" in summary
    assert "aggressive" in summary

    payload = report.to_dict()
    assert "seat_assignments" not in payload
    assert {
        (competitor["competitor_id"], competitor["seat_number"])
        for competitor in payload["competitors"]
    } == {
        ("conservative-1", 1),
        ("aggressive-1", 2),
    }


def test_cli_lists_builtin_strategies(capsys) -> None:
    exit_code = main(["--list-strategies"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "basic" in output
    assert "counting" in output
    assert "conservative" in output
    assert "aggressive" in output


def test_harness_runs_basic_strategy() -> None:
    game_service = GameService(randomizer=Random(11))

    with local_api_client(game_service=game_service) as api_client:
        report = BenchmarkHarness(api_client).run(
            ["basic"],
            rounds=3,
            starting_bankroll=250,
            rules={"minimum_bet": 10, "maximum_bet": 50},
            benchmark_id="benchmark-basic",
        )

    assert report.rounds_completed == 3
    assert len(report.competitors) == 1
    assert report.competitors[0].strategy_name == "basic"
    assert report.competitors[0].final_bankroll > 0


def test_harness_runs_series_and_aggregates_session_metrics() -> None:
    game_service = GameService(randomizer=Random(17))

    with local_api_client(game_service=game_service) as api_client:
        report = BenchmarkHarness(api_client).run_series(
            ["conservative", "aggressive"],
            sessions=3,
            rounds=4,
            starting_bankroll=500,
            rules={"minimum_bet": 10, "maximum_bet": 50},
            benchmark_id="benchmark-series",
        )

    assert report.sessions_requested == 3
    assert report.sessions_completed == 3
    assert report.rounds_requested_per_session == 4
    assert len(report.sessions) == 3
    assert [session.benchmark_id for session in report.sessions] == [
        "benchmark-series-session-0001",
        "benchmark-series-session-0002",
        "benchmark-series-session-0003",
    ]
    assert len(report.competitors) == 2
    assert all(competitor.sessions_played == 3 for competitor in report.competitors)
    assert sum(competitor.session_wins for competitor in report.competitors) == 3
    assert all(competitor.best_final_bankroll >= competitor.worst_final_bankroll for competitor in report.competitors)

    summary = report.format_summary()
    assert "benchmark-series" in summary
    assert "avg rank" in summary.lower()
    assert "sessions" in summary.lower()


def test_harness_run_series_rotates_seats_and_preserves_duplicate_competitor_identity() -> None:
    api_client = _RecordingBenchmarkApiClient()

    report = BenchmarkHarness(api_client).run_series(
        ["counting", "basic", "counting"],
        sessions=3,
        rounds=1,
        starting_bankroll=500,
        benchmark_id="rotating-series",
    )

    assert api_client.seat_assignments_by_table["rotating-series-session-0001-table"] == [
        (1, "rotating-series-session-0001-counting-1"),
        (2, "rotating-series-session-0001-basic-1"),
        (3, "rotating-series-session-0001-counting-2"),
    ]
    assert api_client.seat_assignments_by_table["rotating-series-session-0002-table"] == [
        (1, "rotating-series-session-0002-basic-1"),
        (2, "rotating-series-session-0002-counting-2"),
        (3, "rotating-series-session-0002-counting-1"),
    ]
    assert api_client.seat_assignments_by_table["rotating-series-session-0003-table"] == [
        (1, "rotating-series-session-0003-counting-2"),
        (2, "rotating-series-session-0003-counting-1"),
        (3, "rotating-series-session-0003-basic-1"),
    ]
    assert {competitor.display_name for competitor in report.competitors} == {
        "Basic 1",
        "Counting 1",
        "Counting 2",
    }
    assert {competitor.competitor_id for competitor in report.competitors} == {
        "basic-1",
        "counting-1",
        "counting-2",
    }
    assert all(competitor.sessions_played == 3 for competitor in report.competitors)
    aggregated_by_id = {competitor.competitor_id: competitor for competitor in report.competitors}
    assert aggregated_by_id["basic-1"].seat_counts == {1: 1, 2: 1, 3: 1}
    assert aggregated_by_id["counting-1"].seat_counts == {1: 1, 2: 1, 3: 1}
    assert aggregated_by_id["counting-2"].seat_counts == {1: 1, 2: 1, 3: 1}

    payload = report.to_dict()
    assert sorted(competitor["competitor_id"] for competitor in payload["competitors"]) == [
        "basic-1",
        "counting-1",
        "counting-2",
    ]
    assert {
        competitor["competitor_id"]: competitor["seat_counts"] for competitor in payload["competitors"]
    } == {
        "basic-1": {"1": 1, "2": 1, "3": 1},
        "counting-1": {"1": 1, "2": 1, "3": 1},
        "counting-2": {"1": 1, "2": 1, "3": 1},
    }
    assert [
        sorted(
            (competitor["seat_number"], competitor["competitor_id"])
            for competitor in session["competitors"]
        )
        for session in payload["sessions"]
    ] == [
        [
            (1, "counting-1"),
            (2, "basic-1"),
            (3, "counting-2"),
        ],
        [
            (1, "basic-1"),
            (2, "counting-2"),
            (3, "counting-1"),
        ],
        [
            (1, "counting-2"),
            (2, "counting-1"),
            (3, "basic-1"),
        ],
    ]
    assert all("seat_assignments" not in session for session in payload["sessions"])


def test_run_benchmark_series_rotates_seats_deterministically_with_base_seed() -> None:
    first_run_clients: list[_RecordingBenchmarkApiClient] = []
    second_run_clients: list[_RecordingBenchmarkApiClient] = []

    @contextmanager
    def first_factory(seed: int | None) -> Iterator[_RecordingBenchmarkApiClient]:
        client = _RecordingBenchmarkApiClient(seed=seed)
        first_run_clients.append(client)
        yield client

    @contextmanager
    def second_factory(seed: int | None) -> Iterator[_RecordingBenchmarkApiClient]:
        client = _RecordingBenchmarkApiClient(seed=seed)
        second_run_clients.append(client)
        yield client

    first_report = run_benchmark_series(
        first_factory,
        ["counting", "basic", "counting"],
        sessions=3,
        rounds=1,
        starting_bankroll=500,
        benchmark_id="seeded-rotation",
        seed=29,
    )
    second_report = run_benchmark_series(
        second_factory,
        ["counting", "basic", "counting"],
        sessions=3,
        rounds=1,
        starting_bankroll=500,
        benchmark_id="seeded-rotation",
        seed=29,
    )

    assert first_report.to_dict() == second_report.to_dict()
    assert [client.seed for client in first_run_clients] == [29, 30, 31]
    assert [client.seed for client in second_run_clients] == [29, 30, 31]
    assert [
        client.seat_assignments_by_table[f"seeded-rotation-session-{session_index:04d}-table"]
        for session_index, client in enumerate(first_run_clients, start=1)
    ] == [
        [
            (1, "seeded-rotation-session-0001-counting-1"),
            (2, "seeded-rotation-session-0001-basic-1"),
            (3, "seeded-rotation-session-0001-counting-2"),
        ],
        [
            (1, "seeded-rotation-session-0002-basic-1"),
            (2, "seeded-rotation-session-0002-counting-2"),
            (3, "seeded-rotation-session-0002-counting-1"),
        ],
        [
            (1, "seeded-rotation-session-0003-counting-2"),
            (2, "seeded-rotation-session-0003-counting-1"),
            (3, "seeded-rotation-session-0003-basic-1"),
        ],
    ]


def test_build_series_results_groups_duplicate_strategies_by_stable_competitor_identity() -> None:
    session_reports = (
        BenchmarkReport(
            benchmark_id="series-session-0001",
            table_id="table-1",
            rounds_requested=1,
            rounds_completed=1,
            competitors=(
                CompetitorResult(
                    rank=1,
                    competitor_id="counting-1",
                    player_id="series-session-0001-counting-1",
                    display_name="Counting First Seat",
                    strategy_name="counting",
                    seat_number=1,
                    final_bankroll=550,
                    stats=_series_stats(
                        bankroll_delta=50,
                        wins=1,
                        pushes=0,
                        losses=0,
                        action_counts={"hit": 2, "stand": 1},
                    ),
                ),
                CompetitorResult(
                    rank=2,
                    competitor_id="counting-2",
                    player_id="series-session-0001-counting-2",
                    display_name="Counting Third Seat",
                    strategy_name="counting",
                    seat_number=3,
                    final_bankroll=450,
                    stats=_series_stats(
                        bankroll_delta=-50,
                        wins=0,
                        pushes=0,
                        losses=1,
                        action_counts={"stand": 2, "surrender": 1},
                    ),
                ),
            ),
        ),
        BenchmarkReport(
            benchmark_id="series-session-0002",
            table_id="table-2",
            rounds_requested=1,
            rounds_completed=1,
            competitors=(
                CompetitorResult(
                    rank=2,
                    competitor_id="counting-1",
                    player_id="series-session-0002-counting-1",
                    display_name="Counting Second Seat",
                    strategy_name="counting",
                    seat_number=2,
                    final_bankroll=475,
                    stats=_series_stats(
                        bankroll_delta=-25,
                        wins=0,
                        pushes=0,
                        losses=1,
                        action_counts={"hit": 2, "stand": 1, "double": 2, "split": 1, "insurance": 1},
                    ),
                ),
                CompetitorResult(
                    rank=1,
                    competitor_id="counting-2",
                    player_id="series-session-0002-counting-2",
                    display_name="Counting First Seat",
                    strategy_name="counting",
                    seat_number=1,
                    final_bankroll=525,
                    stats=_series_stats(
                        bankroll_delta=25,
                        wins=1,
                        pushes=0,
                        losses=0,
                        action_counts={"hit": 1, "stand": 1, "surrender": 2},
                    ),
                ),
            ),
        ),
    )

    aggregated = _build_series_results(session_reports)
    aggregated_by_id = {competitor.competitor_id: competitor for competitor in aggregated}

    assert len(aggregated) == 2
    assert set(aggregated_by_id) == {"counting-1", "counting-2"}
    assert aggregated_by_id["counting-1"].sessions_played == 2
    assert aggregated_by_id["counting-2"].sessions_played == 2
    assert aggregated_by_id["counting-1"].average_final_bankroll == 512.5
    assert aggregated_by_id["counting-2"].average_final_bankroll == 487.5
    assert aggregated_by_id["counting-1"].final_bankroll_stddev == 37.5
    assert aggregated_by_id["counting-2"].final_bankroll_stddev == 37.5
    assert aggregated_by_id["counting-1"].bankroll_delta_stddev == 37.5
    assert aggregated_by_id["counting-2"].bankroll_delta_stddev == 37.5
    assert aggregated_by_id["counting-1"].display_name == "Counting First Seat"
    assert aggregated_by_id["counting-2"].display_name == "Counting Third Seat"
    assert aggregated_by_id["counting-1"].seat_counts == {1: 1, 2: 1}
    assert aggregated_by_id["counting-2"].seat_counts == {1: 1, 3: 1}
    assert aggregated_by_id["counting-1"].action_counts == {
        "hit": 4,
        "stand": 2,
        "double": 2,
        "split": 1,
        "surrender": 0,
        "insurance": 1,
    }
    assert aggregated_by_id["counting-1"].action_distribution == {
        "hit": 0.4,
        "stand": 0.2,
        "double": 0.2,
        "split": 0.1,
        "surrender": 0.0,
        "insurance": 0.1,
    }
    assert aggregated_by_id["counting-2"].action_counts == {
        "hit": 1,
        "stand": 3,
        "double": 0,
        "split": 0,
        "surrender": 3,
        "insurance": 0,
    }
    assert aggregated_by_id["counting-2"].action_distribution == {
        "hit": 0.1429,
        "stand": 0.4286,
        "double": 0.0,
        "split": 0.0,
        "surrender": 0.4286,
        "insurance": 0.0,
    }
    assert aggregated_by_id["counting-1"].to_dict()["action_counts"] == {
        "hit": 4,
        "stand": 2,
        "double": 2,
        "split": 1,
        "surrender": 0,
        "insurance": 1,
    }
    assert aggregated_by_id["counting-1"].to_dict()["action_distribution"] == {
        "hit": 0.4,
        "stand": 0.2,
        "double": 0.2,
        "split": 0.1,
        "surrender": 0.0,
        "insurance": 0.1,
    }
    assert aggregated_by_id["counting-1"].to_dict()["final_bankroll_stddev"] == 37.5
    assert aggregated_by_id["counting-1"].to_dict()["bankroll_delta_stddev"] == 37.5
    assert all(isinstance(competitor, SeriesCompetitorResult) for competitor in aggregated)


def test_build_series_results_reports_zero_stddev_for_single_session_competitor() -> None:
    aggregated = _build_series_results(
        (
            BenchmarkReport(
                benchmark_id="series-session-0001",
                table_id="table-1",
                rounds_requested=1,
                rounds_completed=1,
                competitors=(
                    CompetitorResult(
                        rank=1,
                        competitor_id="basic-1",
                        player_id="series-session-0001-basic-1",
                        display_name="Basic First Seat",
                        strategy_name="basic",
                        seat_number=1,
                        final_bankroll=500,
                        stats=_series_stats(
                            bankroll_delta=0,
                            wins=0,
                            pushes=1,
                            losses=0,
                        ),
                    ),
                ),
            ),
        )
    )

    assert len(aggregated) == 1
    assert aggregated[0].final_bankroll_stddev == 0.0
    assert aggregated[0].bankroll_delta_stddev == 0.0
    assert aggregated[0].to_dict()["final_bankroll_stddev"] == 0.0
    assert aggregated[0].to_dict()["bankroll_delta_stddev"] == 0.0


def test_cli_series_mode_is_reproducible_with_base_seed(capsys) -> None:
    argv = [
        "--strategy",
        "conservative",
        "--strategy",
        "aggressive",
        "--series",
        "3",
        "--rounds",
        "4",
        "--seed",
        "29",
        "--benchmark-id",
        "seeded-series",
        "--json",
    ]

    first_exit_code = main(argv)
    first_payload = json.loads(capsys.readouterr().out)
    second_exit_code = main(argv)
    second_payload = json.loads(capsys.readouterr().out)

    assert first_exit_code == 0
    assert second_exit_code == 0
    assert first_payload == second_payload
    assert first_payload["sessions_requested"] == 3
    assert first_payload["sessions_completed"] == 3
    assert first_payload["session_seeds"] == [29, 30, 31]
    assert [session["benchmark_id"] for session in first_payload["sessions"]] == [
        "seeded-series-session-0001",
        "seeded-series-session-0002",
        "seeded-series-session-0003",
    ]
    assert sorted(competitor["competitor_id"] for competitor in first_payload["competitors"]) == [
        "aggressive-1",
        "conservative-1",
    ]
    assert {
        competitor["competitor_id"]: competitor["seat_counts"] for competitor in first_payload["competitors"]
    } == {
        "aggressive-1": {"1": 1, "2": 2},
        "conservative-1": {"1": 2, "2": 1},
    }
    assert [
        sorted(
            (competitor["seat_number"], competitor["competitor_id"])
            for competitor in session["competitors"]
        )
        for session in first_payload["sessions"]
    ] == [
        [
            (1, "conservative-1"),
            (2, "aggressive-1"),
        ],
        [
            (1, "aggressive-1"),
            (2, "conservative-1"),
        ],
        [
            (1, "conservative-1"),
            (2, "aggressive-1"),
        ],
    ]
    assert all("seat_assignments" not in session for session in first_payload["sessions"])
    assert sum(competitor["session_wins"] for competitor in first_payload["competitors"]) == 3


def test_harness_rejects_unknown_request_types_without_polling() -> None:
    api_client = _StubBenchmarkApiClient(
        start_round_response={
            "round_id": "round-stuck",
            "phase": "dealer_turn",
            "next_request": {"type": "mystery"},
            "participants": [],
        }
    )

    with pytest.raises(ValueError, match="Unsupported next_request type"):
        BenchmarkHarness(api_client).run(
            ["conservative"],
            rounds=1,
            starting_bankroll=200,
            benchmark_id="unknown-request",
        )

    assert api_client.get_round_calls == 0


def test_harness_aborts_when_wait_state_never_progresses() -> None:
    wait_state = {
        "round_id": "round-wait",
        "phase": "dealer_turn",
        "action_count": 3,
        "next_request": {"type": "wait", "reason": "dealer_turn"},
        "participants": [],
    }
    api_client = _StubBenchmarkApiClient(
        start_round_response=wait_state,
        get_round_response=wait_state,
    )

    with pytest.raises(RuntimeError, match="did not progress"):
        BenchmarkHarness(api_client).run(
            ["conservative"],
            rounds=1,
            starting_bankroll=200,
            benchmark_id="wait-stuck",
        )

    assert api_client.get_round_calls == 5
