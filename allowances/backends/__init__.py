"""Storage backends shipped with the core. See ``docs/backends/`` for writing your own."""

from allowances.backends.memory import MemoryStore

__all__ = ["MemoryStore"]
