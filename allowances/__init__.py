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
    archive_expired_wallets,
    attach,
    effective_balance,
    find_unattached_fulfillment_records,
    fulfill,
    peek,
    plan_switch,
    unfulfill,
)
from allowances.errors import (
    AllowancesError,
    DefinitionNotFoundError,
    InsufficientBalanceError,
    RecordNotFoundError,
    UnsupportedOperationError,
)
from allowances.store import Store
from allowances.types import (
    FulfillmentRecord,
    FulfillmentResult,
    Modifier,
    ObjectRef,
    SubjectRef,
    Transaction,
    TransactionKind,
    Wallet,
    WalletSpec,
    WalletType,
)
from allowances.windows import window_start

__version__ = "0.1.0"

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
    "RecordNotFoundError",
    "Store",
    "SubjectRef",
    "Transaction",
    "TransactionKind",
    "UnsupportedOperationError",
    "Wallet",
    "WalletSpec",
    "WalletType",
    "archive_expired_wallets",
    "attach",
    "effective_balance",
    "find_unattached_fulfillment_records",
    "fulfill",
    "peek",
    "plan_switch",
    "unfulfill",
    "window_start",
]
