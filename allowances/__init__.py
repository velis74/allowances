"""
allowances — grants and consumable budgets, enforced before the fact and auditable after it.

The core is dependency-free and storage-agnostic: it does arithmetic against a
:class:`~allowances.store.Store` you provide. ``import allowances`` never imports a web framework
or a database driver.

Most applications don't use this module directly — they install a backend
(``pip install allowances[django]``) and call its shortcuts, which supply the store for you.
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
    adjust_wallet,
    archive_expired_wallets,
    attach,
    consumed,
    credit,
    effective_balance,
    find_unattached_fulfillment_records,
    fulfill,
    open_wallet,
    peek,
    plan_switch,
    unfulfill,
)
from allowances.errors import (
    AllowancesError,
    DefinitionNotFoundError,
    InsufficientBalanceError,
    RecordNotFoundError,
    UnsuitableWalletError,
    UnsupportedOperationError,
    WalletNotFoundError,
)
from allowances.store import Store
from allowances.types import (
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

__version__ = "0.3.0"

__all__ = [
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
    "archive_expired_wallets",
    "adjust_wallet",
    "attach",
    "consumed",
    "credit",
    "effective_balance",
    "find_unattached_fulfillment_records",
    "fulfill",
    "open_wallet",
    "peek",
    "plan_switch",
    "unfulfill",
    "window_start",
]
