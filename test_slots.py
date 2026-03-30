from dataclasses import dataclass, field

@dataclass(slots=True)
class TestWithSlots:
    name: str
    _count: int = field(default=0, init=False, repr=False)

@dataclass(slots=False)
class TestWithoutSlots:
    name: str
    _count: int = field(default=0, init=False, repr=False)

# Test with slots
print("Testing WITH slots=True:")
s1 = TestWithSlots(name="test")
print(f"  Has _count: {hasattr(s1, '_count')}")
if hasattr(s1, '_count'):
    print(f"  _count value: {s1._count}")
else:
    print("  _count NOT CREATED")

# Test without slots
print("\nTesting WITHOUT slots (slots=False):")
s2 = TestWithoutSlots(name="test")
print(f"  Has _count: {hasattr(s2, '_count')}")
if hasattr(s2, '_count'):
    print(f"  _count value: {s2._count}")
else:
    print("  _count NOT CREATED")
