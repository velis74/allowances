"""Storage backends shipped with the core. The Store contract for other backends is in
``docs/backends/writing-a-backend.md``."""

from allowances.backends.memory import MemoryStore

__all__ = ["MemoryStore"]
