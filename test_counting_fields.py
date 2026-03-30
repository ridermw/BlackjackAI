from dataclasses import fields
from blackjack_ai.benchmark.strategies import CountingStrategy

print("CountingStrategy fields:")
for f in fields(CountingStrategy):
    print(f"  {f.name}:")
    print(f"    type: {f.type}")
    print(f"    init: {f.init}")
    print(f"    default: {f.default}")
    print(f"    default_factory: {f.default_factory}")
