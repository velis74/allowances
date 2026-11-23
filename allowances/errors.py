"""Every exception the core raises."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from allowances.types import FulfillmentResult


class AllowancesError(Exception):
    """Base class of every exception this library raises."""


class DefinitionNotFoundError(AllowancesError, KeyError):
    """A provider has no definition for the requested feature or currency."""


class RecordNotFoundError(AllowancesError, LookupError):
    """
    No fulfilment record exists for the given guid. Raised by ``attach`` and ``unfulfill``.

    ``unfulfill`` on an already reversed record does not raise; it is a no-op.
    """


class WalletNotFoundError(AllowancesError, LookupError):
    """
    No live wallet exists with the given id: it was never opened, or it has been archived. Also raised
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
    requested and the amount available. ``transactions`` is empty: the call is rolled back and
    nothing is persisted.
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
    A backend does not implement the requested operation.

    Example: a backend without an archive raises it from ``archive_wallet``, one without the scan
    from ``records_with_unattached_transactions``. See ``docs/backends/writing-a-backend.md``.
    """
