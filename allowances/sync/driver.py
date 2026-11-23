"""
Runs the engine's coroutines to completion without an event loop.

The engine awaits only store methods. With a store whose methods never suspend, a coroutine of the engine
finishes on its first step; :func:`run` takes that step and returns the result. A coroutine that suspends
is closed and :class:`~allowances.errors.StoreSuspendedError` is raised.
"""

from __future__ import annotations

import functools

from collections.abc import Callable, Coroutine
from contextlib import contextmanager
from typing import Any, Iterator, ParamSpec, TypeVar

from allowances.errors import StoreSuspendedError
from allowances.store import Store

P = ParamSpec("P")
T = TypeVar("T")


def run(coroutine: Coroutine[Any, Any, T]) -> T:
    """
    Run ``coroutine`` to completion on the calling thread and return its result. An exception raised by
    the coroutine propagates unchanged.

    Raises :class:`~allowances.errors.StoreSuspendedError` when the coroutine suspends; it is closed first.
    """
    try:
        yielded = coroutine.send(None)
    except StopIteration as stop:
        return stop.value
    message = (
        f"The coroutine suspended (it yielded {type(yielded).__name__}): allowances.sync needs a store whose "
        "methods never suspend. Use the asynchronous API with this store."
    )
    try:
        coroutine.close()
    except Exception as exc:
        raise StoreSuspendedError(message) from exc
    raise StoreSuspendedError(message)


def synchronous(function: Callable[P, Coroutine[Any, Any, T]], module: str = "allowances.sync") -> Callable[P, T]:
    """
    A synchronous function with the signature and docstring of the coroutine function ``function``; each
    call runs the coroutine with :func:`run`. Its ``__module__`` is ``module``, the module that binds it under
    the coroutine function's name.
    """

    @functools.wraps(function)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        return run(function(*args, **kwargs))

    wrapper.__module__ = module
    return wrapper


@contextmanager
def unit_of_work(store: Store) -> Iterator[None]:
    """
    ``with unit_of_work(store):`` — the store's ``unit_of_work()`` as a synchronous context manager.
    Everything inside the block, the synchronous API calls included, is atomic; blocks nest with the
    store's savepoint semantics. Entering and leaving are run with :func:`run`.
    """
    manager = store.unit_of_work()
    run(manager.__aenter__())
    try:
        yield
    except BaseException as exc:
        if not run(manager.__aexit__(type(exc), exc, exc.__traceback__)):
            raise
    else:
        run(manager.__aexit__(None, None, None))
