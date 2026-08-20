"""Gunicorn startup hook for runtime extensions."""


def post_worker_init(worker):
    try:
        import app as app_module
        import admin_runtime_hotfix
        import sales_runtime

        admin_runtime_hotfix.apply_patch(app_module)
        sales_runtime.apply_patch(app_module)
    except Exception as exc:
        raise RuntimeError(f"RUNTIME_INIT_FAILED: {exc}") from exc
