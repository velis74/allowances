"""``allowances.W001`` — the one deployment check the app registers."""

from unittest import mock

import pytest

from django.conf import settings

from allowances_django.checks import check_sqlite_transaction_mode

POSTGRES = {"ENGINE": "django.db.backends.postgresql", "NAME": "allowances"}


def sqlite(**options):
    return {"ENGINE": "django.db.backends.sqlite3", "NAME": "db.sqlite3", "OPTIONS": options}


def run(databases):
    """
    Patch the setting rather than ``override_settings``, which tears down live connections and
    warns about it. The check only ever reads ``settings.DATABASES``.
    """
    with mock.patch.object(settings, "DATABASES", databases):
        return check_sqlite_transaction_mode(app_configs=None)


def ids(databases):
    return [warning.id for warning in run(databases)]


def test_silent_on_postgres():
    assert ids({"default": POSTGRES}) == []


@pytest.mark.parametrize("mode", ["IMMEDIATE", "immediate", "EXCLUSIVE"])
def test_silent_when_sqlite_takes_the_write_lock_up_front(mode):
    assert ids({"default": sqlite(transaction_mode=mode)}) == []


@pytest.mark.parametrize("options", [{}, {"transaction_mode": "DEFERRED"}, {"timeout": 20}])
def test_warns_when_sqlite_defers_the_write_lock(options):
    assert ids({"default": sqlite(**options)}) == ["allowances.W001"]


def test_warns_without_an_options_key_at_all():
    assert ids({"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": "db.sqlite3"}}) == ["allowances.W001"]


def test_names_the_offending_alias_so_multi_database_projects_can_find_it():
    warnings = run({"default": POSTGRES, "ledger": sqlite(), "reports": sqlite(transaction_mode="IMMEDIATE")})

    assert len(warnings) == 1
    assert '"ledger"' in warnings[0].msg
    assert 'DATABASES["ledger"]' in warnings[0].hint
