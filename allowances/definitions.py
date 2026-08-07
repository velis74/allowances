"""
The definition layer: what rights *exist*, as opposed to what a subject currently *holds*.

Definitions (features, currencies, conversion rates, fulfillment order) are configuration, not
state — they are not rows in a database and they are not versioned per subject. The engine never
reads them directly; it asks a :class:`DefinitionProvider`.

:class:`DictProvider` reads them from a plain dict. The Django adapter subclasses it to read
``settings.ALLOWANCES``. A remote licence server would be a third implementation, and the engine
would not notice.
"""

from __future__ import annotations

import abc
import importlib

from dataclasses import dataclass
from decimal import Decimal

from allowances.errors import DefinitionNotFoundError


def import_string(dotted_path: str):
    """
    Import ``"package.module.attribute"`` and return the attribute.

    A dependency-free equivalent of ``django.utils.module_loading.import_string`` — the core must
    not import Django to resolve a ``fulfillment_order`` callable.
    """
    module_path, _, attr = dotted_path.rpartition(".")
    if not module_path:
        raise ImportError(f"{dotted_path!r} is not a dotted module path")
    try:
        return getattr(importlib.import_module(module_path), attr)
    except AttributeError as exc:
        raise ImportError(f"Module {module_path!r} has no attribute {attr!r}") from exc


@dataclass(frozen=True)
class CurrencyDefinition:
    """
    A named unit of entitlement.

    ``general=True`` marks a fallback currency (e.g. ``credits``) that any feature accepting it may
    draw from. ``round_fn`` decides how a fractional cost is turned into a whole unit —
    ``"floor"``, ``"ceil"``, ``"round"``, or ``None`` to leave fractions alone.
    """

    name: str
    general: bool = False
    round_fn: str | None = None


@dataclass(frozen=True)
class CurrencySpec:
    """
    One entry in a feature section's ``currencies`` list: a currency plus its conversion rate
    against the feature's own request unit. ``("credits", 0.5)`` means one unit of this feature
    costs half a credit. A bare string means a rate of 1.
    """

    currency: str
    rate: Decimal = Decimal("1")


@dataclass(frozen=True)
class FeatureSection:
    """The ``grant`` or ``consumable`` half of a feature definition."""

    currencies: tuple[CurrencySpec, ...]

    def rate_for(self, currency: str) -> Decimal | None:
        for spec in self.currencies:
            if spec.currency == currency:
                return spec.rate
        return None

    @property
    def currency_names(self) -> tuple[str, ...]:
        return tuple(spec.currency for spec in self.currencies)


@dataclass(frozen=True)
class FeatureDefinition:
    """
    A named capability: ``send_email``, ``coolant_fill``.

    ``fulfillment_order`` is either a list of ``(wallet_type, subject_name)`` steps or a callable
    that receives the candidate wallets and returns them in draw order. Note the callable receives
    a ``list[Wallet]`` — plain value objects, not a database query — so the same ordering function
    works against any backend.
    """

    name: str
    grant: FeatureSection | None
    consumable: FeatureSection | None
    subjects: frozenset
    fulfillment_order: object
    overdraft: bool = False

    def section_for(self, wallet_type: str) -> FeatureSection | None:
        return self.grant if wallet_type == "grant" else self.consumable


class DefinitionProvider(abc.ABC):
    """Abstract source of feature and currency definitions."""

    @abc.abstractmethod
    def get_feature(self, name: str) -> FeatureDefinition: ...

    @abc.abstractmethod
    def get_currency(self, name: str) -> CurrencyDefinition: ...


class DictProvider(DefinitionProvider):
    """
    Reads definitions from a dict of the shape::

        {
            "currencies": {
                "email_send": {},
                "credits": {"general": True, "round_fn": "ceil"},
            },
            "features": {
                "send_email": {
                    "grant": {"currencies": ["email_send"]},
                    "consumable": {"currencies": ["email_send", ("credits", 0.5)]},
                    "subjects": {"device", "team"},
                    "fulfillment_order": [("grant", "device"), ("grant", None), "team"],
                    "overdraft": False,
                },
            },
        }
    """

    def __init__(self, config: dict | None = None):
        self._config = config

    @property
    def config(self) -> dict:
        return self._config if self._config is not None else {}

    def get_feature(self, name: str) -> FeatureDefinition:
        try:
            raw = self.config["features"][name]
        except KeyError:
            raise DefinitionNotFoundError(f"Unknown feature: {name!r}") from None
        return self._parse_feature(name, raw)

    def get_currency(self, name: str) -> CurrencyDefinition:
        try:
            raw = self.config["currencies"][name]
        except KeyError:
            raise DefinitionNotFoundError(f"Unknown currency: {name!r}") from None
        return CurrencyDefinition(
            name=name,
            general=bool(raw.get("general", False)),
            round_fn=raw.get("round_fn"),
        )

    def _parse_feature(self, name: str, raw: dict) -> FeatureDefinition:
        return FeatureDefinition(
            name=name,
            grant=self._parse_section(raw.get("grant")),
            consumable=self._parse_section(raw.get("consumable")),
            subjects=frozenset(raw.get("subjects", ())),
            fulfillment_order=self._parse_fulfillment_order(raw.get("fulfillment_order", [])),
            overdraft=bool(raw.get("overdraft", False)),
        )

    @staticmethod
    def _parse_section(raw: dict | None) -> FeatureSection | None:
        if raw is None:
            return None
        specs = []
        for entry in raw.get("currencies", []):
            if isinstance(entry, (tuple, list)):
                currency, rate = entry
                specs.append(CurrencySpec(currency=currency, rate=Decimal(str(rate))))
            else:
                specs.append(CurrencySpec(currency=entry, rate=Decimal("1")))
        return FeatureSection(currencies=tuple(specs))

    @staticmethod
    def _parse_fulfillment_order(raw):
        if callable(raw):
            return raw
        if isinstance(raw, str):
            return import_string(raw)
        parsed = []
        for entry in raw:
            if isinstance(entry, str):
                # Shorthand: "team" means (None, "team") — any wallet type, that subject.
                parsed.append((None, entry))
            else:
                wallet_type, subject = entry
                parsed.append((wallet_type, subject))
        return parsed
