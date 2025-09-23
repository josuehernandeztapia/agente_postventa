"""Compatibility shim exposing the FastAPI app from `app.api`.

Keeps historical imports working while delegating implementation to
`app.api`. Attribute writes are mirrored back to the underlying module so tests
can `patch('main.<name>')` as before.
"""

from importlib import import_module
import sys
import types

_api = import_module("app.api")
_module = sys.modules[__name__]


class _MainProxy(types.ModuleType):
    """Module proxy that forwards attribute access to `app.api`."""

    def __getattr__(self, name: str):
        try:
            return super().__getattribute__(name)
        except AttributeError:
            return getattr(_api, name)

    def __setattr__(self, name: str, value):
        setattr(_api, name, value)
        return super().__setattr__(name, value)


_module.__class__ = _MainProxy

# Ensure FastAPI finds the app as `main:app` (uvicorn compat)
app = _api.app  # type: ignore[attr-defined]

# Re-export all attributes (including private helpers)
for _name in dir(_api):
    if _name.startswith("__"):
        continue
    setattr(_module, _name, getattr(_api, _name))

__all__ = [name for name in dir(_module) if not name.startswith("__")]
