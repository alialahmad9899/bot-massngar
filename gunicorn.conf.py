"""Gunicorn startup hook for the unified production runtime."""


def post_worker_init(worker):
    try:
        # The unified runtime is written against the ``app`` module because
        # it owns shared state such as STOP_EVENT, DB_LOCK and worker lists.
        # Render may start either ``app:app`` or ``app_entry:app``; both paths
        # can safely converge here on the same module object.
        import app as app_module
        import production_runtime_v2

        # Runtime code installs the webhook through ``app.view_functions``.
        # In the Python module, expose Flask's registry under that established
        # name without changing the runtime's module-level state contract.
        app_module.view_functions = app_module.app.view_functions

        if not getattr(app_module, production_runtime_v2.RUNTIME_MARKER, False):
            production_runtime_v2.bootstrap(app_module)
    except Exception as exc:
        raise RuntimeError(f"RUNTIME_INIT_FAILED: {exc}") from exc
