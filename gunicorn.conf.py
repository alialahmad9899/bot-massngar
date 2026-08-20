"""Lightweight Gunicorn hook for the admin/CRM runtime extension."""


def post_worker_init(worker):
    try:
        import app as app_module
        import admin_runtime_hotfix
        admin_runtime_hotfix.apply_patch(app_module)
    except Exception as exc:
        raise RuntimeError(f"ADMIN_RUNTIME_INIT_FAILED: {exc}") from exc
