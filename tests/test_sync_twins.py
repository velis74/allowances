"""
Every coroutine function of the asynchronous API has a synchronous twin of the same name and signature, and every
function of the synchronous API has an asynchronous twin:

- :mod:`allowances` and :mod:`allowances.sync`;
- :mod:`allowances_django.shortcuts` and :mod:`allowances_django.sync.shortcuts`;
- :mod:`allowances_django.permissions` and :mod:`allowances_django.sync.permissions`.

A name both modules bind to the same object (a type, an error, a pure helper) is a re-export, not a twin.
"""

import importlib
import inspect
import pickle

import pytest

PAIRS = [
    ("allowances", "allowances.sync"),
    ("allowances_django.shortcuts", "allowances_django.sync.shortcuts"),
    ("allowances_django.permissions", "allowances_django.sync.permissions"),
]

#: Synchronous names without an asynchronous twin: asynchronous code writes ``async with store.unit_of_work()``.
SYNC_ONLY = {"allowances.sync": {"unit_of_work"}}


def public_functions(module):
    """Public functions defined or exported by ``module``: ``__all__``, else the functions defined in it."""
    if hasattr(module, "__all__"):
        names = module.__all__
    else:
        names = [
            name
            for name, value in vars(module).items()
            if not name.startswith("_") and getattr(value, "__module__", None) == module.__name__
        ]
    return {name: getattr(module, name) for name in names if inspect.isfunction(getattr(module, name))}


def ids(pair):
    return pair[1]


@pytest.mark.parametrize("pair", PAIRS, ids=ids)
def test_every_coroutine_function_has_a_synchronous_twin_with_the_same_signature(pair):
    asynchronous, synchronous = (importlib.import_module(name) for name in pair)
    coroutine_functions = {
        name: function
        for name, function in public_functions(asynchronous).items()
        if inspect.iscoroutinefunction(function)
    }

    assert coroutine_functions
    for name, function in coroutine_functions.items():
        twin = getattr(synchronous, name, None)
        assert twin is not None, f"{pair[1]} has no {name}"
        assert not inspect.iscoroutinefunction(twin), f"{pair[1]}.{name} is a coroutine function"
        assert inspect.signature(twin) == inspect.signature(function), name
        assert twin.__doc__ == function.__doc__, name


@pytest.mark.parametrize("pair", PAIRS, ids=ids)
def test_every_synchronous_function_has_an_asynchronous_twin_with_the_same_signature(pair):
    asynchronous, synchronous = (importlib.import_module(name) for name in pair)
    sync_only = SYNC_ONLY.get(pair[1], set())

    for name, function in public_functions(synchronous).items():
        if name in sync_only:
            continue
        twin = getattr(asynchronous, name, None)
        assert twin is not None, f"{pair[0]} has no {name}"
        if twin is function:
            assert not inspect.iscoroutinefunction(function), f"{pair[1]}.{name} is the coroutine function itself"
            continue
        assert not inspect.iscoroutinefunction(function), f"{pair[1]}.{name} is a coroutine function"
        assert inspect.signature(twin) == inspect.signature(function), name


@pytest.mark.parametrize("pair", PAIRS, ids=ids)
def test_no_public_name_carries_an_a_prefix_twin(pair):
    """The asynchronous API uses the plain names: no ``afulfill`` next to ``fulfill``."""
    for module in (importlib.import_module(name) for name in pair):
        names = set(dir(module))
        assert not {name for name in names if name.startswith("a") and name[1:] in names and not name.startswith("_")}


def test_the_synchronous_only_names_are_synchronous():
    import allowances.sync

    assert not inspect.iscoroutinefunction(allowances.sync.unit_of_work)
    assert not hasattr(importlib.import_module("allowances"), "unit_of_work")


@pytest.mark.parametrize("pair", PAIRS, ids=ids)
def test_every_synchronous_twin_belongs_to_its_synchronous_module_and_pickles(pair):
    """A twin names the module that binds it, so ``pickle`` (and a process pool) finds the twin, not the coroutine."""
    asynchronous, synchronous = (importlib.import_module(name) for name in pair)

    sync_only = SYNC_ONLY.get(pair[1], set())

    for name, function in public_functions(synchronous).items():
        if name in sync_only or getattr(asynchronous, name, None) is function:
            continue
        assert function.__module__ == pair[1], name
        assert pickle.loads(pickle.dumps(function)) is function, name
