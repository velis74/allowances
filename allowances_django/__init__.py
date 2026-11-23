"""
Django storage backend for the ``allowances`` core.

Add ``"allowances_django"`` to ``INSTALLED_APPS``, declare features and currencies in
``settings.ALLOWANCES``, and call the functions in :mod:`allowances_django.shortcuts`.

This package imports nothing: Django imports it while building the app registry, before models are
ready, and importing models here fails at startup. Import from the submodule::

    from allowances_django.shortcuts import fulfill, peek

Django finds ``apps.AllowancesConfig`` automatically.
"""
