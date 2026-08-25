"""Production WSGI entrypoint."""
from app import app
import production_runtime_v2

production_runtime_v2.bootstrap(app)

__all__ = ["app"]
