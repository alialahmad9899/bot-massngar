"""Gunicorn startup hook for the canonical production WSGI entrypoint.

The Render Procfile loads ``app_entry:app``. That entrypoint is responsible
for installing the unified production runtime, so this hook must operate on
the Flask application object rather than the ``app`` Python module.
"""


def post_worker_init(worker):
    try:
        # Import the exact WSGI entrypoint used by Procfile. Importing it here
        # is safe because production_runtime_v2.bootstrap() is idempotent on
        # the Flask application object.
        import app_entry
        import production_runtime_v2

        application = app_entry.app
        if not getattr(application, production_runtime_v2.RUNTIME_MARKER, False):
            production_runtime_v2.bootstrap(application)
    except Exception as exc:
        raise RuntimeError(f"RUNTIME_INIT_FAILED: {exc}") from exc
