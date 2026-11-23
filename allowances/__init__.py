"""
allowances — grants and consumable budgets: checked and drawn before an operation, recorded in an
append-only ledger.

The core has no dependencies and reads and writes state only through a
:class:`~allowances.store.Store` passed to each call. ``import allowances`` imports no web framework
and no database driver.

Applications usually call a backend's shortcuts instead (``allowances_django.shortcuts``, installed
with ``pip install allowances[django]``); the shortcuts supply the store.
"""

from allowances.definitions import (
    CurrencyDefinition,
    CurrencySpec,
    DefinitionProvider,
    DictProvider,
    FeatureDefinition,
    FeatureSection,
)
from allowances.engine import (
    add_member,
    adjust_wallet,
    archive_expired_wallets,
    attach,
    cancel_scheduled_switch,
    close_wallet,
    consumed,
    credit,
    effective_allowances,
    effective_balance,
    find_unattached_fulfillment_records,
    fulfill,
    groups_of,
    open_wallet,
    peek,
    plan_switch,
    remove_member,
    transfer,
    unfulfill,
)
from allowances.errors import (
    AllowancesError,
    DefinitionNotFoundError,
    InsufficientBalanceError,
    MembershipCycleError,
    RecordNotFoundError,
    UnsuitableWalletError,
    UnsupportedOperationError,
    WalletNotFoundError,
)
from allowances.store import Store
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

__version__ = "0.4.0"

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
    "archive_expired_wallets",
    "adjust_wallet",
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
    "window_start",
]
