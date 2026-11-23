"""
adorm storage backend for the ``allowances`` core: :class:`allowances_adorm.store.AdormStore`.

The store reads and writes the tables of ``allowances_django`` (which stays in ``INSTALLED_APPS`` for the
migrations) through adorm models generated from them, :mod:`allowances_adorm.models`. Install the extra
``allowances[adorm]``; ``adorm[asyncpg]`` adds adorm's native PostgreSQL driver.

adorm is configured by the application: ``"adorm.contrib.django"`` in ``INSTALLED_APPS`` (every ``DATABASES`` alias
becomes a profile on adorm's Django driver, ``ADORM`` holds the arguments of ``adorm.configure()``), or
``adorm.configure()`` after ``django.setup()``::

    ADORM = {
        "profiles": {
            "default": {"driver": "adorm.drivers.django.DjangoDriver", "alias": "default"},
            "asyncpg": {"driver": "adorm.drivers.asyncpg.AsyncpgDriver", "alias": "default"},
        },
        "pool": {"max_connections": 10},
    }

    store = AdormStore("asyncpg")        # every call on the session ("asyncpg", "default")
    store = AdormStore()                 # every call on adorm's current session

This package imports nothing; import the store from its submodule::

    from allowances_adorm.store import AdormStore
"""
