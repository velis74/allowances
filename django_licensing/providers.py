"""
Definition-layer providers (README: "Definition vs. State").

The *state* layer (Wallet/Transaction/... in ``models.py``) never talks to ``settings`` directly.
Instead the fulfillment engine (``engine.py``) asks a ``DefinitionProvider`` for ``FeatureDefinition``
and ``CurrencyDefinition`` objects. ``SettingsProvider`` is the only implementation shipped in this
concern — it parses the ``settings.LICENSING`` dict shape shown in the README's "Feature" section.

The abstract base exists so a future ``ServerProvider`` (README: "Out of Scope" — fetches
definitions from a remote licence server) is a drop-in replacement with no changes required to
``engine.py`` or the models. See ``NEXT_STEPS.md``.
"""

import abc

from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings
from django.utils.module_loading import import_string


class DefinitionNotFoundError(KeyError):
    """Raised when a provider is asked for a feature/currency it doesn't know about."""


@dataclass(frozen=True)
class CurrencyDefinition:
    """A currency as declared in ``settings.LICENSING['currencies']`` (README: "Currency")."""

    name: str
    general: bool = False
    round_fn: str | None = None  # "floor" | "ceil" | "round" | None


@dataclass(frozen=True)
class CurrencySpec:
    """
    One entry in a Feature section's ``currencies`` list: a currency name plus the conversion rate
    against the feature's own request unit (README: "A currency tuple ``('credits', 0.5)`` means:
    consuming 1 unit of this feature costs 0.5 credits."). A bare string entry (no tuple) means
    rate=1.
    """

    currency: str
    rate: Decimal = Decimal("1")


@dataclass(frozen=True)
class FeatureSection:
    """The ``grant`` or ``consumable`` sub-dict of a Feature definition."""

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
    """A feature as declared in ``settings.LICENSING['features']`` (README: "Feature")."""

    name: str
    grant: FeatureSection | None
    consumable: FeatureSection | None
    subjects: frozenset
    # Either a list of (type, subject) tuples (README: "Fulfillment Order") or a callable taking
    # and returning an ordered QuerySet of candidate wallets (README: dotted-string callable form).
    fulfillment_order: object
    overdraft: bool = False


class DefinitionProvider(abc.ABC):
    """
    Abstract source of Feature/Currency definitions.

    ``SettingsProvider`` (below) is the only implementation in this concern. A future
    ``ServerProvider`` implements the same interface against a remote licence server — see
    README "Out of Scope".
    """

    @abc.abstractmethod
    def get_feature(self, name: str) -> FeatureDefinition: ...

    @abc.abstractmethod
    def get_currency(self, name: str) -> CurrencyDefinition: ...


class SettingsProvider(DefinitionProvider):
    """
    Reads Feature/Currency definitions from ``settings.LICENSING`` (README's current/default
    provider). A ``config`` dict may be injected directly (bypassing Django settings entirely) —
    useful for tests and for host applications that want a definition source scoped more narrowly
    than the global settings module (e.g. per-tenant config assembled at request time).
    """

    def __init__(self, config: dict | None = None):
        self._config = config

    @property
    def config(self) -> dict:
        if self._config is not None:
            return self._config
        return getattr(settings, "LICENSING", {})

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
        # Dotted-string callable form (README: "fulfillment_order may be a dotted string reference
        # to a callable that receives a QuerySet of candidate wallets and returns it ordered").
        if isinstance(raw, str):
            return import_string(raw)
        parsed = []
        for entry in raw:
            if isinstance(entry, str):
                # Shorthand: "location" == (None, "location").
                parsed.append((None, entry))
            else:
                wallet_type, subject = entry
                parsed.append((wallet_type, subject))
        return parsed


def get_default_provider() -> DefinitionProvider:
    """The provider used by ``engine.py`` when the caller doesn't inject one explicitly."""
    return SettingsProvider()
