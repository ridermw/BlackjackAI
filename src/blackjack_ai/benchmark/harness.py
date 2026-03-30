from __future__ import annotations

import statistics
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from random import Random
from typing import Any
from typing import ContextManager
from typing import Mapping
from typing import Sequence
from uuid import uuid4

from fastapi.testclient import TestClient

from blackjack_ai.api.app import create_app
from blackjack_ai.api.service import GameService
from blackjack_ai.benchmark.client import BenchmarkApiClient
from blackjack_ai.benchmark.client import BenchmarkApiError
from blackjack_ai.benchmark.strategies import ActionContext
from blackjack_ai.benchmark.strategies import BenchmarkStrategy
from blackjack_ai.benchmark.strategies import BetContext
from blackjack_ai.benchmark.strategies import resolve_strategy
from blackjack_ai.config import Settings
from blackjack_ai.engine import ActionType


@dataclass(frozen=True, slots=True)
class _Competitor:
    competitor_id: str
    seat_number: int
    player_id: str
    player_token: str
    display_name: str
    starting_bankroll: int
    strategy: BenchmarkStrategy

    @property
    def strategy_name(self) -> str:
        return self.strategy.name


@dataclass(frozen=True, slots=True)
class _CompetitorSpec:
    competitor_id: str
    strategy_input: str | BenchmarkStrategy
    strategy_name: str
    strategy_index: int
    display_name: str

    def resolve_strategy(self) -> BenchmarkStrategy:
        if isinstance(self.strategy_input, str):
            return resolve_strategy(self.strategy_input)
        return self.strategy_input


@dataclass(frozen=True, slots=True)
class CompetitorResult:
    rank: int
    competitor_id: str
    player_id: str
    display_name: str
    strategy_name: str
    final_bankroll: int
    stats: dict[str, Any]
    seat_number: int | None = None
    leaderboard_rank: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "competitor_id": self.competitor_id,
            "leaderboard_rank": self.leaderboard_rank,
            "player_id": self.player_id,
            "display_name": self.display_name,
            "strategy_name": self.strategy_name,
            "seat_number": self.seat_number,
            "final_bankroll": self.final_bankroll,
            "stats": dict(self.stats),
        }


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    benchmark_id: str
    table_id: str
    rounds_requested: int
    rounds_completed: int
    competitors: tuple[CompetitorResult, ...]
    stopped_early_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "benchmark_id": self.benchmark_id,
            "table_id": self.table_id,
            "rounds_requested": self.rounds_requested,
            "rounds_completed": self.rounds_completed,
            "stopped_early_reason": self.stopped_early_reason,
            "competitors": [competitor.to_dict() for competitor in self.competitors],
        }

    def format_summary(self) -> str:
        lines = [
            f"Benchmark {self.benchmark_id}: {self.rounds_completed}/{self.rounds_requested} rounds completed.",
            f"Table: {self.table_id}",
        ]
        if self.stopped_early_reason is not None:
            lines.append(f"Stopped early: {self.stopped_early_reason}")
        lines.extend(
            [
                "",
                "rank strategy      player                bankroll  delta rounds hands w/p/l   bust% avg/hand",
                "---- ------------- -------------------- -------- ------ ------ ----- ------- ----- --------",
            ]
        )
        for competitor in self.competitors:
            stats = competitor.stats
            lines.append(
                (
                    f"{competitor.rank:>4} "
                    f"{competitor.strategy_name:<13} "
                    f"{competitor.display_name:<20} "
                    f"{competitor.final_bankroll:>8} "
                    f"{stats['bankroll_delta']:>+6} "
                    f"{stats['rounds_played']:>6} "
                    f"{stats['hands_played']:>5} "
                    f"{stats['wins']:>1}/{stats['pushes']:>1}/{stats['losses']:>1} "
                    f"{stats['bust_rate'] * 100:>5.1f} "
                    f"{stats['average_return_per_hand']:>8.2f}"
                )
            )
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class SeriesCompetitorResult:
    rank: int
    competitor_id: str
    display_name: str
    strategy_name: str
    sessions_played: int
    session_wins: int
    average_rank: float
    average_final_bankroll: float
    final_bankroll_stddev: float
    average_bankroll_delta: float
    bankroll_delta_stddev: float
    average_rounds_played: float
    average_hands_played: float
    average_wins: float
    average_pushes: float
    average_losses: float
    average_bust_rate: float
    average_return_per_hand: float
    best_final_bankroll: int
    worst_final_bankroll: int
    seat_counts: dict[int, int]
    action_counts: dict[str, int]
    action_distribution: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "competitor_id": self.competitor_id,
            "display_name": self.display_name,
            "strategy_name": self.strategy_name,
            "sessions_played": self.sessions_played,
            "session_wins": self.session_wins,
            "average_rank": self.average_rank,
            "average_final_bankroll": self.average_final_bankroll,
            "final_bankroll_stddev": self.final_bankroll_stddev,
            "average_bankroll_delta": self.average_bankroll_delta,
            "bankroll_delta_stddev": self.bankroll_delta_stddev,
            "average_rounds_played": self.average_rounds_played,
            "average_hands_played": self.average_hands_played,
            "average_wins": self.average_wins,
            "average_pushes": self.average_pushes,
            "average_losses": self.average_losses,
            "average_bust_rate": self.average_bust_rate,
            "average_return_per_hand": self.average_return_per_hand,
            "best_final_bankroll": self.best_final_bankroll,
            "worst_final_bankroll": self.worst_final_bankroll,
            "seat_counts": {
                str(seat_number): count for seat_number, count in sorted(self.seat_counts.items())
            },
            "action_counts": {
                action.value: self.action_counts.get(action.value, 0)
                for action in ActionType
            },
            "action_distribution": {
                action.value: self.action_distribution.get(action.value, 0.0)
                for action in ActionType
            },
        }


@dataclass(frozen=True, slots=True)
class BenchmarkSeriesReport:
    benchmark_id: str
    sessions_requested: int
    sessions_completed: int
    rounds_requested_per_session: int
    competitors: tuple[SeriesCompetitorResult, ...]
    sessions: tuple[BenchmarkReport, ...]
    session_seeds: tuple[int | None, ...] = ()

    @property
    def total_rounds_requested(self) -> int:
        return self.sessions_requested * self.rounds_requested_per_session

    @property
    def total_rounds_completed(self) -> int:
        return sum(session.rounds_completed for session in self.sessions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "benchmark_id": self.benchmark_id,
            "sessions_requested": self.sessions_requested,
            "sessions_completed": self.sessions_completed,
            "rounds_requested_per_session": self.rounds_requested_per_session,
            "total_rounds_requested": self.total_rounds_requested,
            "total_rounds_completed": self.total_rounds_completed,
            "session_seeds": list(self.session_seeds),
            "competitors": [competitor.to_dict() for competitor in self.competitors],
            "sessions": [session.to_dict() for session in self.sessions],
        }

    def format_summary(self) -> str:
        lines = [
            (
                f"Benchmark series {self.benchmark_id}: "
                f"{self.sessions_completed}/{self.sessions_requested} sessions completed."
            ),
            f"Rounds completed: {self.total_rounds_completed}/{self.total_rounds_requested}",
        ]
        known_session_seeds = [seed for seed in self.session_seeds if seed is not None]
        if known_session_seeds:
            lines.append("Session seeds: " + ", ".join(str(seed) for seed in known_session_seeds))
        lines.extend(
            [
                "",
                "rank strategy      player                avg roll   avg delta avg rank 1st sessions avg/hand bust%",
                "---- ------------- -------------------- ---------- --------- -------- --- -------- -------- -----",
            ]
        )
        for competitor in self.competitors:
            lines.append(
                (
                    f"{competitor.rank:>4} "
                    f"{competitor.strategy_name:<13} "
                    f"{competitor.display_name:<20} "
                    f"{competitor.average_final_bankroll:>10.2f} "
                    f"{competitor.average_bankroll_delta:>+9.2f} "
                    f"{competitor.average_rank:>8.2f} "
                    f"{competitor.session_wins:>3} "
                    f"{competitor.sessions_played:>8} "
                    f"{competitor.average_return_per_hand:>8.2f} "
                    f"{competitor.average_bust_rate * 100:>5.1f}"
                )
            )
        return "\n".join(lines)


class BenchmarkHarness:
    def __init__(self, api_client: BenchmarkApiClient) -> None:
        self._api_client = api_client

    def run(
        self,
        strategies: Sequence[str | BenchmarkStrategy],
        *,
        rounds: int,
        starting_bankroll: int = 1000,
        rules: Mapping[str, Any] | None = None,
        benchmark_id: str | None = None,
    ) -> BenchmarkReport:
        if rounds <= 0:
            raise ValueError("rounds must be greater than zero.")
        if starting_bankroll <= 0:
            raise ValueError("starting_bankroll must be greater than zero.")
        if not strategies:
            raise ValueError("At least one strategy is required.")

        return self._run_competitor_specs(
            _build_competitor_specs(strategies),
            rounds=rounds,
            starting_bankroll=starting_bankroll,
            rules=rules,
            benchmark_id=benchmark_id,
        )

    def run_series(
        self,
        strategies: Sequence[str | BenchmarkStrategy],
        *,
        sessions: int,
        rounds: int,
        starting_bankroll: int = 1000,
        rules: Mapping[str, Any] | None = None,
        benchmark_id: str | None = None,
    ) -> BenchmarkSeriesReport:
        if sessions <= 0:
            raise ValueError("sessions must be greater than zero.")
        if rounds <= 0:
            raise ValueError("rounds must be greater than zero.")
        if starting_bankroll <= 0:
            raise ValueError("starting_bankroll must be greater than zero.")
        if not strategies:
            raise ValueError("At least one strategy is required.")

        series_identifier = benchmark_id or f"benchmark-series-{uuid4().hex[:8]}"
        competitor_specs = _build_competitor_specs(strategies)
        session_reports = tuple(
            self._run_competitor_specs(
                _rotate_competitor_specs(competitor_specs, session_index),
                rounds=rounds,
                starting_bankroll=starting_bankroll,
                rules=rules,
                benchmark_id=_series_session_benchmark_id(series_identifier, session_index),
            )
            for session_index in range(1, sessions + 1)
        )
        return _build_series_report(
            benchmark_id=series_identifier,
            sessions_requested=sessions,
            rounds_requested_per_session=rounds,
            session_reports=session_reports,
            session_seeds=tuple(None for _ in session_reports),
        )

    def _run_competitor_specs(
        self,
        competitor_specs: Sequence[_CompetitorSpec],
        *,
        rounds: int,
        starting_bankroll: int = 1000,
        rules: Mapping[str, Any] | None = None,
        benchmark_id: str | None = None,
    ) -> BenchmarkReport:
        if rounds <= 0:
            raise ValueError("rounds must be greater than zero.")
        if starting_bankroll <= 0:
            raise ValueError("starting_bankroll must be greater than zero.")
        if not competitor_specs:
            raise ValueError("At least one strategy is required.")

        benchmark_identifier = benchmark_id or f"benchmark-{uuid4().hex[:8]}"
        competitors = self._register_competitors(
            benchmark_id=benchmark_identifier,
            competitor_specs=competitor_specs,
            starting_bankroll=starting_bankroll,
        )
        competitors_by_player_id = {competitor.player_id: competitor for competitor in competitors}

        table_id = f"{benchmark_identifier}-table"
        self._api_client.create_table(
            {
                "table_id": table_id,
                "seat_count": len(competitors),
                "rules": dict(rules or {}),
                "metadata": {"benchmark_id": benchmark_identifier},
            }
        )
        for competitor in competitors:
            self._api_client.join_table_seat(
                table_id,
                competitor.seat_number,
                {"player_id": competitor.player_id, "bankroll": competitor.starting_bankroll},
                player_token=competitor.player_token,
            )

        rounds_completed = 0
        stopped_early_reason: str | None = None
        for round_index in range(1, rounds + 1):
            round_id = f"{benchmark_identifier}-round-{round_index:04d}"
            try:
                round_state = self._api_client.start_round(table_id, {"round_id": round_id})
            except BenchmarkApiError as exc:
                if exc.status_code == 409 and "ready for the next round" in exc.detail.lower():
                    stopped_early_reason = exc.detail
                    break
                raise
            self._play_round(round_state, competitors_by_player_id, round_index)
            rounds_completed += 1

        table_state = self._api_client.get_table(table_id)
        leaderboard = self._api_client.get_leaderboard(participant_type="ai")
        results = self._build_results(
            competitors=competitors,
            table_state=table_state,
            leaderboard=leaderboard,
        )

        return BenchmarkReport(
            benchmark_id=benchmark_identifier,
            table_id=table_id,
            rounds_requested=rounds,
            rounds_completed=rounds_completed,
            competitors=tuple(results),
            stopped_early_reason=stopped_early_reason,
        )

    def _register_competitors(
        self,
        *,
        benchmark_id: str,
        competitor_specs: Sequence[_CompetitorSpec],
        starting_bankroll: int,
    ) -> list[_Competitor]:
        competitors: list[_Competitor] = []
        for seat_number, competitor_spec in enumerate(competitor_specs, start=1):
            strategy = competitor_spec.resolve_strategy()
            player_id = f"{benchmark_id}-{competitor_spec.competitor_id}"
            created_player = self._api_client.create_player(
                {
                    "player_id": player_id,
                    "display_name": competitor_spec.display_name,
                    "participant_type": "ai",
                    "starting_bankroll": starting_bankroll,
                    "metadata": {
                        "benchmark_id": benchmark_id,
                        "strategy": competitor_spec.strategy_name,
                        "competitor_id": competitor_spec.competitor_id,
                    },
                }
            )
            competitors.append(
                _Competitor(
                    competitor_id=competitor_spec.competitor_id,
                    seat_number=seat_number,
                    player_id=player_id,
                    player_token=str(created_player["player_token"]),
                    display_name=competitor_spec.display_name,
                    starting_bankroll=starting_bankroll,
                    strategy=strategy,
                )
            )
        return competitors

    def _play_round(
        self,
        round_state: dict[str, Any],
        competitors_by_player_id: Mapping[str, _Competitor],
        round_index: int,
    ) -> dict[str, Any]:
        current_state = round_state
        stagnant_wait_polls = 0
        last_wait_signature: tuple[Any, ...] | None = None
        while current_state.get("phase") != "complete":
            next_request = current_state.get("next_request", {})
            request_type = next_request.get("type")

            if request_type == "bet":
                stagnant_wait_polls = 0
                last_wait_signature = None
                pending_player_ids = list(next_request.get("pending_player_ids", []))
                for player_id in pending_player_ids:
                    if current_state.get("phase") != "waiting_for_bets":
                        break
                    participant = self._participant(current_state, player_id)
                    competitor = competitors_by_player_id[player_id]
                    amount = competitor.strategy.choose_bet(
                        BetContext(
                            round_index=round_index,
                            player_id=player_id,
                            participant=participant,
                            round_state=current_state,
                        )
                    )
                    current_state = self._api_client.place_bet(
                        current_state["round_id"],
                        {"player_id": player_id, "amount": self._normalize_bet(amount, current_state, participant)},
                        player_token=competitor.player_token,
                    )
                continue

            if request_type == "action":
                stagnant_wait_polls = 0
                last_wait_signature = None
                player_id = str(next_request["player_id"])
                hand_id = str(next_request["hand_id"])
                legal_actions = tuple(str(action) for action in next_request.get("legal_actions", []))
                participant = self._participant(current_state, player_id)
                hand = self._hand(participant, hand_id)
                competitor = competitors_by_player_id[player_id]
                action = competitor.strategy.choose_action(
                    ActionContext(
                        round_index=round_index,
                        player_id=player_id,
                        participant=participant,
                        hand=hand,
                        round_state=current_state,
                        legal_actions=legal_actions,
                    )
                )
                current_state = self._api_client.apply_action(
                    current_state["round_id"],
                    {
                        "player_id": player_id,
                        "hand_id": hand_id,
                        "action": self._normalize_action(action, legal_actions),
                    },
                    player_token=competitor.player_token,
                )
                continue

            if request_type == "round_complete":
                break

            if request_type == "wait":
                wait_signature = (
                    current_state.get("phase"),
                    current_state.get("action_count"),
                    repr(next_request),
                )
                if wait_signature == last_wait_signature:
                    stagnant_wait_polls += 1
                else:
                    last_wait_signature = wait_signature
                    stagnant_wait_polls = 1

                if stagnant_wait_polls > 5:
                    raise RuntimeError(
                        f"Round '{current_state.get('round_id')}' did not progress after repeated wait states."
                    )

                current_state = self._api_client.get_round(str(current_state["round_id"]))
                continue

            raise ValueError(f"Unsupported next_request type '{request_type}' from API.")

        return current_state

    def _build_results(
        self,
        *,
        competitors: Sequence[_Competitor],
        table_state: Mapping[str, Any],
        leaderboard: Mapping[str, Any],
    ) -> list[CompetitorResult]:
        bankrolls = self._seat_bankrolls(table_state)
        competitor_index = {competitor.player_id: competitor for competitor in competitors}
        leaderboard_index = {
            entry["player_id"]: entry
            for entry in leaderboard.get("entries", [])
            if entry.get("player_id") in competitor_index
        }

        raw_results: list[CompetitorResult] = []
        for competitor in competitors:
            leaderboard_entry = leaderboard_index.get(competitor.player_id)
            if leaderboard_entry is None:
                stats_payload = self._api_client.get_player_stats(competitor.player_id)
                stats = dict(stats_payload["stats"])
                leaderboard_rank = None
            else:
                stats = dict(leaderboard_entry["stats"])
                leaderboard_rank = int(leaderboard_entry["rank"])

            raw_results.append(
                CompetitorResult(
                    rank=0,
                    competitor_id=competitor.competitor_id,
                    player_id=competitor.player_id,
                    display_name=competitor.display_name,
                    strategy_name=competitor.strategy_name,
                    final_bankroll=bankrolls.get(
                        competitor.player_id,
                        competitor.starting_bankroll + int(stats["bankroll_delta"]),
                    ),
                    stats=stats,
                    seat_number=competitor.seat_number,
                    leaderboard_rank=leaderboard_rank,
                )
            )

        raw_results.sort(key=self._result_sort_key)
        return [
            CompetitorResult(
                rank=rank,
                competitor_id=result.competitor_id,
                player_id=result.player_id,
                display_name=result.display_name,
                strategy_name=result.strategy_name,
                final_bankroll=result.final_bankroll,
                stats=result.stats,
                seat_number=result.seat_number,
                leaderboard_rank=result.leaderboard_rank,
            )
            for rank, result in enumerate(raw_results, start=1)
        ]

    @staticmethod
    def _result_sort_key(result: CompetitorResult) -> tuple[float, float, float, str]:
        stats = result.stats
        average_return = float(stats["average_return_per_hand"]) if stats["hands_played"] else 0.0
        return (
            -float(stats["bankroll_delta"]),
            -float(stats["wins"]),
            -average_return,
            result.display_name.lower(),
        )

    @staticmethod
    def _participant(round_state: Mapping[str, Any], player_id: str) -> Mapping[str, Any]:
        for participant in round_state.get("participants", []):
            if participant.get("player_id") == player_id:
                return participant
        raise ValueError(f"Player '{player_id}' is not part of round '{round_state.get('round_id')}'.")

    @staticmethod
    def _hand(participant: Mapping[str, Any], hand_id: str) -> Mapping[str, Any]:
        for hand in participant.get("hands", []):
            if hand.get("hand_id") == hand_id:
                return hand
        raise ValueError(f"Hand '{hand_id}' was not found for player '{participant.get('player_id')}'.")

    @staticmethod
    def _normalize_bet(amount: int, round_state: Mapping[str, Any], participant: Mapping[str, Any]) -> int:
        rules = round_state.get("rules", {})
        minimum_bet = int(rules.get("minimum_bet", 10))
        maximum_bet = int(rules.get("maximum_bet", minimum_bet))
        available_bankroll = int(participant.get("available_bankroll", minimum_bet))
        if available_bankroll < minimum_bet:
            raise ValueError(
                f"Player '{participant.get('player_id')}' does not have enough bankroll to meet the table minimum."
            )
        return max(minimum_bet, min(maximum_bet, available_bankroll, int(amount)))

    @staticmethod
    def _normalize_action(action: str, legal_actions: Sequence[str]) -> str:
        normalized_action = action.strip().lower()
        if normalized_action in legal_actions:
            return normalized_action
        if "stand" in legal_actions:
            return "stand"
        if not legal_actions:
            raise ValueError("No legal actions are available.")
        return legal_actions[0]

    @staticmethod
    def _seat_bankrolls(table_state: Mapping[str, Any]) -> dict[str, int]:
        bankrolls: dict[str, int] = {}
        for seat in table_state.get("seats", []):
            occupant = seat.get("occupant")
            if occupant is None:
                continue
            bankrolls[str(occupant["player_id"])] = int(seat.get("bankroll", 0))
        return bankrolls


def run_benchmark_series(
    api_client_factory: Callable[[int | None], ContextManager[BenchmarkApiClient]],
    strategies: Sequence[str | BenchmarkStrategy],
    *,
    sessions: int,
    rounds: int,
    starting_bankroll: int = 1000,
    rules: Mapping[str, Any] | None = None,
    benchmark_id: str | None = None,
    seed: int | None = None,
) -> BenchmarkSeriesReport:
    if sessions <= 0:
        raise ValueError("sessions must be greater than zero.")
    if rounds <= 0:
        raise ValueError("rounds must be greater than zero.")
    if starting_bankroll <= 0:
        raise ValueError("starting_bankroll must be greater than zero.")
    if not strategies:
        raise ValueError("At least one strategy is required.")

    series_identifier = benchmark_id or f"benchmark-series-{uuid4().hex[:8]}"
    competitor_specs = _build_competitor_specs(strategies)
    session_reports: list[BenchmarkReport] = []
    session_seeds: list[int | None] = []
    for session_index in range(1, sessions + 1):
        session_seed = _series_session_seed(seed, session_index)
        session_seeds.append(session_seed)
        with api_client_factory(session_seed) as api_client:
            session_reports.append(
                BenchmarkHarness(api_client)._run_competitor_specs(
                    _rotate_competitor_specs(competitor_specs, session_index),
                    rounds=rounds,
                    starting_bankroll=starting_bankroll,
                    rules=rules,
                    benchmark_id=_series_session_benchmark_id(series_identifier, session_index),
                )
            )

    return _build_series_report(
        benchmark_id=series_identifier,
        sessions_requested=sessions,
        rounds_requested_per_session=rounds,
        session_reports=tuple(session_reports),
        session_seeds=tuple(session_seeds),
    )


def _build_series_report(
    *,
    benchmark_id: str,
    sessions_requested: int,
    rounds_requested_per_session: int,
    session_reports: Sequence[BenchmarkReport],
    session_seeds: Sequence[int | None],
) -> BenchmarkSeriesReport:
    return BenchmarkSeriesReport(
        benchmark_id=benchmark_id,
        sessions_requested=sessions_requested,
        sessions_completed=len(session_reports),
        rounds_requested_per_session=rounds_requested_per_session,
        competitors=tuple(_build_series_results(session_reports)),
        sessions=tuple(session_reports),
        session_seeds=tuple(session_seeds),
    )


def _build_series_results(session_reports: Sequence[BenchmarkReport]) -> list[SeriesCompetitorResult]:
    grouped_results: dict[str, list[CompetitorResult]] = {}
    for session_report in session_reports:
        for competitor in session_report.competitors:
            grouped_results.setdefault(competitor.competitor_id, []).append(competitor)

    raw_results: list[SeriesCompetitorResult] = []
    for competitor_id, competitors in grouped_results.items():
        exemplar = competitors[0]
        session_count = len(competitors)
        final_bankrolls = [float(competitor.final_bankroll) for competitor in competitors]
        bankroll_deltas = [float(competitor.stats["bankroll_delta"]) for competitor in competitors]
        rounds_played = [float(competitor.stats["rounds_played"]) for competitor in competitors]
        hands_played = [float(competitor.stats["hands_played"]) for competitor in competitors]
        wins = [float(competitor.stats["wins"]) for competitor in competitors]
        pushes = [float(competitor.stats["pushes"]) for competitor in competitors]
        losses = [float(competitor.stats["losses"]) for competitor in competitors]
        bust_rates = [float(competitor.stats["bust_rate"]) for competitor in competitors]
        average_returns = [float(competitor.stats["average_return_per_hand"]) for competitor in competitors]
        seat_counts = _aggregate_seat_counts(competitors)
        action_counts = _aggregate_action_counts(competitors)
        action_distribution = _build_action_distribution(action_counts)

        raw_results.append(
            SeriesCompetitorResult(
                rank=0,
                competitor_id=competitor_id,
                display_name=exemplar.display_name,
                strategy_name=exemplar.strategy_name,
                sessions_played=session_count,
                session_wins=sum(1 for competitor in competitors if competitor.rank == 1),
                average_rank=sum(competitor.rank for competitor in competitors) / session_count,
                average_final_bankroll=sum(final_bankrolls) / session_count,
                final_bankroll_stddev=_population_stddev(final_bankrolls),
                average_bankroll_delta=sum(bankroll_deltas) / session_count,
                bankroll_delta_stddev=_population_stddev(bankroll_deltas),
                average_rounds_played=sum(rounds_played) / session_count,
                average_hands_played=sum(hands_played) / session_count,
                average_wins=sum(wins) / session_count,
                average_pushes=sum(pushes) / session_count,
                average_losses=sum(losses) / session_count,
                average_bust_rate=sum(bust_rates) / session_count,
                average_return_per_hand=sum(average_returns) / session_count,
                best_final_bankroll=max(competitor.final_bankroll for competitor in competitors),
                worst_final_bankroll=min(competitor.final_bankroll for competitor in competitors),
                seat_counts=seat_counts,
                action_counts=action_counts,
                action_distribution=action_distribution,
            )
        )

    raw_results.sort(key=_series_result_sort_key)
    return [
        SeriesCompetitorResult(
            rank=rank,
            competitor_id=result.competitor_id,
            display_name=result.display_name,
            strategy_name=result.strategy_name,
            sessions_played=result.sessions_played,
            session_wins=result.session_wins,
            average_rank=result.average_rank,
            average_final_bankroll=result.average_final_bankroll,
            final_bankroll_stddev=result.final_bankroll_stddev,
            average_bankroll_delta=result.average_bankroll_delta,
            bankroll_delta_stddev=result.bankroll_delta_stddev,
            average_rounds_played=result.average_rounds_played,
            average_hands_played=result.average_hands_played,
            average_wins=result.average_wins,
            average_pushes=result.average_pushes,
            average_losses=result.average_losses,
            average_bust_rate=result.average_bust_rate,
            average_return_per_hand=result.average_return_per_hand,
            best_final_bankroll=result.best_final_bankroll,
            worst_final_bankroll=result.worst_final_bankroll,
            seat_counts=dict(result.seat_counts),
            action_counts=dict(result.action_counts),
            action_distribution=dict(result.action_distribution),
        )
        for rank, result in enumerate(raw_results, start=1)
    ]


def _aggregate_seat_counts(competitors: Sequence[CompetitorResult]) -> dict[int, int]:
    seat_counts: dict[int, int] = {}
    for competitor in competitors:
        seat_number = competitor.seat_number
        if seat_number is None:
            continue
        seat_counts[seat_number] = seat_counts.get(seat_number, 0) + 1
    return dict(sorted(seat_counts.items()))


def _aggregate_action_counts(competitors: Sequence[CompetitorResult]) -> dict[str, int]:
    action_counts = {action.value: 0 for action in ActionType}
    for competitor in competitors:
        competitor_action_counts = competitor.stats.get("action_counts", {})
        if not isinstance(competitor_action_counts, Mapping):
            continue
        for action in ActionType:
            action_counts[action.value] += int(competitor_action_counts.get(action.value, 0))
    return action_counts


def _build_action_distribution(action_counts: Mapping[str, int]) -> dict[str, float]:
    total_actions = sum(int(action_counts.get(action.value, 0)) for action in ActionType)
    return {
        action.value: round(int(action_counts.get(action.value, 0)) / total_actions, 4) if total_actions else 0.0
        for action in ActionType
    }


def _population_stddev(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return statistics.pstdev(values)


def _series_result_sort_key(result: SeriesCompetitorResult) -> tuple[float, float, float, float, str]:
    return (
        result.average_rank,
        -float(result.session_wins),
        -result.average_bankroll_delta,
        -result.average_return_per_hand,
        result.competitor_id,
    )


def _series_session_seed(seed: int | None, session_index: int) -> int | None:
    if seed is None:
        return None
    return seed + session_index - 1


def _build_competitor_specs(strategies: Sequence[str | BenchmarkStrategy]) -> tuple[_CompetitorSpec, ...]:
    duplicates: dict[str, int] = {}
    competitor_specs: list[_CompetitorSpec] = []
    for strategy in strategies:
        strategy_name = _strategy_name(strategy)
        duplicates[strategy_name] = duplicates.get(strategy_name, 0) + 1
        strategy_index = duplicates[strategy_name]
        competitor_specs.append(
            _CompetitorSpec(
                competitor_id=f"{strategy_name}-{strategy_index}",
                strategy_input=strategy,
                strategy_name=strategy_name,
                strategy_index=strategy_index,
                display_name=f"{strategy_name.title()} {strategy_index}",
            )
        )
    return tuple(competitor_specs)


def _rotate_competitor_specs(
    competitor_specs: Sequence[_CompetitorSpec],
    session_index: int,
) -> tuple[_CompetitorSpec, ...]:
    normalized_competitor_specs = tuple(competitor_specs)
    if not normalized_competitor_specs:
        return ()
    rotation = (session_index - 1) % len(normalized_competitor_specs)
    return normalized_competitor_specs[rotation:] + normalized_competitor_specs[:rotation]


def _strategy_name(strategy: str | BenchmarkStrategy) -> str:
    if isinstance(strategy, str):
        return resolve_strategy(strategy).name
    return strategy.name


def _series_session_benchmark_id(benchmark_id: str, session_index: int) -> str:
    return f"{benchmark_id}-session-{session_index:04d}"


@contextmanager
def local_api_client(
    *,
    seed: int | None = None,
    game_service: GameService | None = None,
) -> Iterator[BenchmarkApiClient]:
    randomizer = Random(seed) if seed is not None and game_service is None else None
    service = game_service or GameService(randomizer=randomizer)
    app = create_app(
        Settings(
            environment="benchmark",
            database_url="sqlite:///:memory:",
        ),
        game_service=service,
    )
    with TestClient(app, base_url="http://benchmark.local") as transport:
        yield BenchmarkApiClient(transport)
