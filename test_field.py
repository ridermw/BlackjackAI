from dataclasses import dataclass, field, fields

@dataclass(slots=True)
class TestStrategy:
    name: str
    _count: int = field(default=0, init=False, repr=False)

print("Fields defined:")
for f in fields(TestStrategy):
    print(f"  {f.name}: init={f.init}, default={f.default}, default_factory={f.default_factory}")

t = TestStrategy(name="test")
print(f"\nInstance created")
print(f"  Has _count: {hasattr(t, '_count')}")

# Try to access
try:
    value = t._count
    print(f"  _count value: {value}")
except AttributeError as e:
    print(f"  ERROR: {e}")

# Check __slots__
print(f"\n__slots__: {t.__slots__}")
