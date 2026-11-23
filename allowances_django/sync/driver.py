"""Synchronous twins of the Django shortcuts, run with :func:`allowances.sync.driver.run`."""

from __future__ import annotations

import functools

from collections.abc import Callable, Coroutine
from typing import Any, ParamSpec, TypeVar

from allowances.sync.driver import run
from allowances_django.config import default_sync_store

P = ParamSpec("P")
T = TypeVar("T")


def synchronous(function: Callable[P, Coroutine[Any, Any, T]], module: str) -> Callable[P, T]:
    """
    A synchronous function with the signature and docstring of the coroutine function ``function``, a Django
    shortcut with a keyword ``store``. Each call passes ``store`` or the store from ``settings.ALLOWANCES_SYNC_STORE``
    and runs the coroutine with :func:`~allowances.sync.driver.run`. Its ``__module__`` is ``module``, the module
    that binds it under the coroutine function's name.
    """

    @functools.wraps(function)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        kwargs["store"] = default_sync_store(kwargs.get("store"))
        return run(function(*args, **kwargs))

    wrapper.__module__ = module
    return wrapper
