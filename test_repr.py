from dataclasses import dataclass, field, fields

@dataclass(slots=True)
class Test1:
    name: str
    _count: int = field(default=0, init=False, repr=False)

@dataclass(slots=True)
class Test2:
    name: str
    _count: int = field(default=0, init=False)

print("Test1 fields (repr=False):")
for f in fields(Test1):
    print(f"  {f.name}: init={f.init}, repr={f.repr}")

print("\nTest2 fields (no repr):")
for f in fields(Test2):
    print(f"  {f.name}: init={f.init}, repr={f.repr}")

# Try to instantiate
t1 = Test1(name="test")
t2 = Test2(name="test")

print(f"\nTest1 instance has _count: {hasattr(t1, '_count')}")
print(f"Test2 instance has _count: {hasattr(t2, '_count')}")

if hasattr(t1, '_count'):
    print(f"Test1._count = {t1._count}")
if hasattr(t2, '_count'):
    print(f"Test2._count = {t2._count}")
