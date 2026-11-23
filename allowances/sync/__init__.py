"""
The synchronous API: the public names of :mod:`allowances` as plain functions.

Each engine function here has the signature and docstring of its coroutine in :mod:`allowances` and runs
that coroutine to completion on the calling thread, without an event loop
(:func:`allowances.sync.driver.run`). Types, errors, definitions and pure helpers are the same objects as
in :mod:`allowances`::

    from allowances.sync import fulfill, attach, unfulfill, SubjectRef, InsufficientBalanceError

The store must never suspend: its methods are coroutines whose bodies await nothing that yields to an
event loop, as in :class:`~allowances.backends.memory.MemoryStore` and
``allowances_django.sync.store.DjangoStore``. A call that suspends raises
:class:`~allowances.errors.StoreSuspendedError`.

:func:`unit_of_work` opens a store's unit of work as a synchronous context manager, so several calls
share one atomic block.
"""

from allowances import engine as _engine
from allowances.definitions import (
    CurrencyDefinition,
    CurrencySpec,
    DefinitionProvider,
    DictProvider,
    FeatureDefinition,
    FeatureSection,
)
from allowances.errors import (
    AllowancesError,
    DefinitionNotFoundError,
    InsufficientBalanceError,
    MembershipCycleError,
    RecordNotFoundError,
    StoreSuspendedError,
    UnsuitableWalletError,
    UnsupportedOperationError,
    WalletNotFoundError,
)
from allowances.store import Store
from allowances.sync.driver import synchronous, unit_of_work
from allowances.types import (
    Allowance,
    FulfillmentRecord,
    FulfillmentResult,
    Modifier,
    ObjectRef,
    PLAN_SOURCE,
    SubjectRef,
    SwitchPolicy,
    Transaction,
    TransactionKind,
    Wallet,
    WalletSpec,
    WalletType,
)
from allowances.windows import window_start

add_member = synchronous(_engine.add_member)
adjust_wallet = synchronous(_engine.adjust_wallet)
archive_expired_wallets = synchronous(_engine.archive_expired_wallets)
attach = synchronous(_engine.attach)
cancel_scheduled_switch = synchronous(_engine.cancel_scheduled_switch)
close_wallet = synchronous(_engine.close_wallet)
consumed = synchronous(_engine.consumed)
credit = synchronous(_engine.credit)
effective_allowances = synchronous(_engine.effective_allowances)
effective_balance = synchronous(_engine.effective_balance)
find_unattached_fulfillment_records = synchronous(_engine.find_unattached_fulfillment_records)
fulfill = synchronous(_engine.fulfill)
groups_of = synchronous(_engine.groups_of)
open_wallet = synchronous(_engine.open_wallet)
peek = synchronous(_engine.peek)
plan_switch = synchronous(_engine.plan_switch)
remove_member = synchronous(_engine.remove_member)
transfer = synchronous(_engine.transfer)
unfulfill = synchronous(_engine.unfulfill)

__all__ = [
    "Allowance",
    "AllowancesError",
    "CurrencyDefinition",
    "CurrencySpec",
    "DefinitionNotFoundError",
    "DefinitionProvider",
    "DictProvider",
    "FeatureDefinition",
    "FeatureSection",
    "FulfillmentRecord",
    "FulfillmentResult",
    "InsufficientBalanceError",
    "MembershipCycleError",
    "Modifier",
    "ObjectRef",
    "PLAN_SOURCE",
    "RecordNotFoundError",
    "Store",
    "StoreSuspendedError",
    "SubjectRef",
    "SwitchPolicy",
    "Transaction",
    "TransactionKind",
    "UnsuitableWalletError",
    "UnsupportedOperationError",
    "Wallet",
    "WalletNotFoundError",
    "WalletSpec",
    "WalletType",
    "add_member",
    "adjust_wallet",
    "archive_expired_wallets",
    "attach",
    "cancel_scheduled_switch",
    "close_wallet",
    "consumed",
    "credit",
    "effective_allowances",
    "effective_balance",
    "find_unattached_fulfillment_records",
    "fulfill",
    "groups_of",
    "open_wallet",
    "peek",
    "plan_switch",
    "remove_member",
    "transfer",
    "unfulfill",
    "unit_of_work",
    "window_start",
]
