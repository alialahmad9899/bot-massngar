"""Lightweight Gunicorn hook for the admin/CRM runtime extension."""


def post_worker_init(worker):
    try:
        import app as app_module
        import admin_runtime
        admin_runtime.apply_patch(app_module)
    except Exception as exc:
        # Fail fast: serving without the admin/runtime safety layer is not acceptable.
        raise RuntimeError(f"ADMIN_RUNTIME_INIT_FAILED: {exc}") from exc
