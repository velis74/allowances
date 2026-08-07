"""Every exception the core raises."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from allowances.types import FulfillmentResult


class AllowancesError(Exception):
    """Base class for everything this library raises, so callers can catch one thing."""


class DefinitionNotFoundError(AllowancesError, KeyError):
    """A provider was asked for a feature or currency it doesn't know about."""


class RecordNotFoundError(AllowancesError, LookupError):
    """
    No fulfillment record exists for the given guid.

    Distinct from the no-op that ``unfulfill`` performs on an *already reversed* record: that is a
    legitimate repeat call, whereas a guid nobody ever fulfilled under means the caller has lost
    track of which operation it was undoing.
    """


class InsufficientBalanceError(AllowancesError):
    """
    ``fulfill(..., allow_partial=False)`` could not cover the whole request.

    Carries the :class:`~allowances.types.FulfillmentResult` that *would* have been returned had
    ``allow_partial=True`` been passed, so a caller can see how far it got. ``transactions`` is
    empty — nothing was persisted, the whole call is rolled back.
    """

    def __init__(self, result: FulfillmentResult):
        self.result = result
        super().__init__(f"Requested {result.requested}, only {result.granted} available (guid={result.guid})")


class UnsupportedOperationError(AllowancesError, NotImplementedError):
    """
    A backend was asked for something it deliberately does not implement.

    Not every store can do everything — an append-only or cache-shaped backend may have no archive
    to move wallets into, and no way to answer a scan like
    ``records_with_unattached_transactions``. Raising this (rather than silently doing nothing)
    keeps the gap visible. See ``docs/backends/writing-a-backend.md``.
    """
