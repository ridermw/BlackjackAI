from __future__ import annotations

import argparse
import json
from typing import Sequence

from blackjack_ai.benchmark import BenchmarkApiClient
from blackjack_ai.benchmark import BenchmarkHarness
from blackjack_ai.benchmark import list_builtin_strategies
from blackjack_ai.benchmark import local_api_client
from blackjack_ai.benchmark import resolve_strategy
from blackjack_ai.benchmark import run_benchmark_series


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run repeated Blackjack strategy comparisons through the public REST API."
    )
    parser.add_argument("--base-url", help="Target an already-running API service instead of the default in-process app.")
    parser.add_argument("--strategy", action="append", help="Built-in strategy name. Repeat to add more seats.")
    parser.add_argument("--rounds", type=_positive_int, default=50, help="Number of rounds to simulate per session.")
    parser.add_argument(
        "--series",
        type=_positive_int,
        default=1,
        help="Number of independent benchmark sessions/shoes to aggregate.",
    )
    parser.add_argument(
        "--starting-bankroll",
        type=_positive_int,
        default=1000,
        help="Initial bankroll assigned to each benchmark player.",
    )
    parser.add_argument("--minimum-bet", type=_positive_int, default=10, help="Table minimum bet.")
    parser.add_argument("--maximum-bet", type=_positive_int, default=500, help="Table maximum bet.")
    parser.add_argument(
        "--seed",
        type=int,
        help="Base random seed for the default in-process benchmark server. Series mode increments it per session.",
    )
    parser.add_argument("--benchmark-id", help="Optional stable identifier prefix for benchmark-created resources.")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Emit the final report as JSON.")
    parser.add_argument("--list-strategies", action="store_true", help="List built-in strategies and exit.")
    return parser


def _strategy_listing() -> str:
    lines = ["Built-in strategies:"]
    for strategy in list_builtin_strategies():
        lines.append(f"- {strategy.name}: {strategy.description}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_strategies:
        print(_strategy_listing())
        return 0

    if args.maximum_bet < args.minimum_bet:
        parser.error("--maximum-bet must be greater than or equal to --minimum-bet.")
    if args.base_url and args.seed is not None:
        parser.error("--seed is only supported when using the default in-process benchmark server.")

    strategy_names = args.strategy or ["conservative", "aggressive"]
    for strategy_name in strategy_names:
        try:
            resolve_strategy(strategy_name)
        except ValueError as exc:
            parser.error(str(exc))

    rules = {
        "minimum_bet": args.minimum_bet,
        "maximum_bet": args.maximum_bet,
    }

    if args.series == 1:
        if args.base_url:
            with BenchmarkApiClient.from_base_url(args.base_url) as api_client:
                report = BenchmarkHarness(api_client).run(
                    strategy_names,
                    rounds=args.rounds,
                    starting_bankroll=args.starting_bankroll,
                    rules=rules,
                    benchmark_id=args.benchmark_id,
                )
        else:
            with local_api_client(seed=args.seed) as api_client:
                report = BenchmarkHarness(api_client).run(
                    strategy_names,
                    rounds=args.rounds,
                    starting_bankroll=args.starting_bankroll,
                    rules=rules,
                    benchmark_id=args.benchmark_id,
                )
    else:
        if args.base_url:
            report = run_benchmark_series(
                lambda _session_seed: BenchmarkApiClient.from_base_url(args.base_url),
                strategy_names,
                sessions=args.series,
                rounds=args.rounds,
                starting_bankroll=args.starting_bankroll,
                rules=rules,
                benchmark_id=args.benchmark_id,
            )
        else:
            report = run_benchmark_series(
                lambda session_seed: local_api_client(seed=session_seed),
                strategy_names,
                sessions=args.series,
                rounds=args.rounds,
                starting_bankroll=args.starting_bankroll,
                rules=rules,
                benchmark_id=args.benchmark_id,
                seed=args.seed,
            )

    print(json.dumps(report.to_dict(), indent=2) if args.json_output else report.format_summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
