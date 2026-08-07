"""
Django storage backend for the ``allowances`` core.

Add ``"allowances_django"`` to ``INSTALLED_APPS``, declare features and currencies in
``settings.ALLOWANCES``, and call the functions in :mod:`allowances_django.shortcuts`.

Nothing is imported here on purpose. Django imports an application package while building the app
registry, before models are ready, so pulling models in from this module would break startup.
Import from the submodule you need instead::

    from allowances_django.shortcuts import fulfill, peek

(Django finds ``apps.AllowancesConfig`` on its own — ``default_app_config`` was removed in 4.1.)
"""
