from dataclasses import replace
from blackjack_ai.benchmark.strategies import _BUILTIN_STRATEGIES, CountingStrategy

# Find the counting strategy
counting = None
for strategy in _BUILTIN_STRATEGIES:
    if isinstance(strategy, CountingStrategy):
        counting = strategy
        break

print("Original counting strategy:")
print(f"  Has _observed_round_index: {hasattr(counting, '_observed_round_index')}")

# Try to use it
try:
    counting._observed_round_index
    print(f"  _observed_round_index: {counting._observed_round_index}")
except AttributeError as e:
    print(f"  ERROR accessing _observed_round_index: {e}")

# Now clone it with replace
cloned = replace(counting)
print("\nCloned counting strategy:")
print(f"  Has _observed_round_index: {hasattr(cloned, '_observed_round_index')}")
try:
    cloned._observed_round_index
    print(f"  _observed_round_index: {cloned._observed_round_index}")
except AttributeError as e:
    print(f"  ERROR accessing _observed_round_index: {e}")

# Create a fresh instance manually
fresh = CountingStrategy(name="test", description="test")
print("\nFresh instance:")
print(f"  Has _observed_round_index: {hasattr(fresh, '_observed_round_index')}")
try:
    print(f"  _observed_round_index: {fresh._observed_round_index}")
except AttributeError as e:
    print(f"  ERROR accessing _observed_round_index: {e}")
