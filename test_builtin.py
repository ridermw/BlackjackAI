from blackjack_ai.benchmark.strategies import _BUILTIN_STRATEGIES, CountingStrategy

print("Checking builtin strategies:")
for strategy in _BUILTIN_STRATEGIES:
    print(f"\n{strategy.name}: {type(strategy)}")
    if isinstance(strategy, CountingStrategy):
        print(f"  Has _observed_round_index: {hasattr(strategy, '_observed_round_index')}")
        if hasattr(strategy, '_observed_round_index'):
            print(f"  _observed_round_index: {strategy._observed_round_index}")
            print(f"  _running_count: {strategy._running_count}")
        print(f"  All attributes: {[x for x in dir(strategy) if not x.startswith('__')]}")
