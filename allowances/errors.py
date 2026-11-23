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


class WalletNotFoundError(AllowancesError, LookupError):
    """
    No live wallet exists with the given id — it was never opened, or it has been archived. Also raised
    by ``unfulfill`` for a transfer whose source wallet has been archived.
    """


class UnsuitableWalletError(AllowancesError, ValueError):
    """
    The wallet cannot take this operation: :func:`allowances.engine.credit` on a windowed wallet or one
    not valid at the given time, :func:`allowances.engine.adjust_wallet` on a wallet without an
    ``allotment``, or :func:`allowances.engine.transfer` between wallets of different currencies, from a
    wallet to itself, or with a wallet not valid at the given time.
    """


class InsufficientBalanceError(AllowancesError):
    """
    ``fulfill(..., allow_partial=False)`` could not cover the whole request, the source wallet of
    :func:`allowances.engine.transfer` does not hold the amount, or ``unfulfill`` cannot reverse a
    transfer because its target no longer holds the transferred amount.

    Carries a :class:`~allowances.types.FulfillmentResult`: for ``fulfill`` the one that *would* have
    been returned had ``allow_partial=True`` been passed, for a transfer or its reversal the amount
    requested and the amount available. ``transactions`` is empty — nothing was persisted, the whole
    call is rolled back.
    """

    def __init__(self, result: FulfillmentResult):
        self.result = result
        super().__init__(f"Requested {result.requested}, only {result.granted} available (guid={result.guid})")


class MembershipCycleError(AllowancesError):
    """
    :func:`allowances.engine.add_member` was asked to make a subject a member of itself, or of a
    group that is already (transitively) a member of that subject.
    """


class UnsupportedOperationError(AllowancesError, NotImplementedError):
    """
    A backend was asked for something it deliberately does not implement.

    Not every store can do everything — an append-only or cache-shaped backend may have no archive
    to move wallets into, and no way to answer a scan like
    ``records_with_unattached_transactions``. Raising this (rather than silently doing nothing)
    keeps the gap visible. See ``docs/backends/writing-a-backend.md``.
    """
