"""Gunicorn startup hook for the unified production runtime."""


def post_worker_init(worker):
    try:
        import app as app_module
        import production_runtime_v2
        production_runtime_v2.bootstrap(app_module)
    except Exception as exc:
        raise RuntimeError(f"RUNTIME_INIT_FAILED: {exc}") from exc
