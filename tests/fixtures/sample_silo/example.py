"""Example Python file for testing code indexing."""


def greet(name: str) -> str:
    """Return a greeting message."""
    return f"Hello, {name}!"


def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


if __name__ == "__main__":
    print(greet("world"))
    print(f"2 + 3 = {add(2, 3)}")
