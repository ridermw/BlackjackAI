from dataclasses import replace
from blackjack_ai.benchmark.strategies import CountingStrategy

# Create a fresh CountingStrategy
s1 = CountingStrategy(name='test', description='test')
print('Fresh instance:')
print(f'  Has _observed_round_index: {hasattr(s1, "_observed_round_index")}')
print(f'  _observed_round_index value: {s1._observed_round_index}')

# Now use replace like _clone_strategy does
s2 = replace(s1)
print('\nAfter replace:')
print(f'  Has _observed_round_index: {hasattr(s2, "_observed_round_index")}')
try:
    print(f'  _observed_round_index value: {s2._observed_round_index}')
except AttributeError as e:
    print(f'  ERROR: {e}')
