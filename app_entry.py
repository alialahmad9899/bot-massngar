"""Production WSGI entrypoint.

Imports the legacy application, then installs the single unified runtime before
Gunicorn begins serving requests.
"""

from app import app
import production_runtime

production_runtime.bootstrap(app)

__all__ = ["app"]
