"""Execution facts and independent measurement for Kalshi strategies."""
from typing import TYPE_CHECKING
from .store import Store
if TYPE_CHECKING:
    from .broker import Broker


def __getattr__(name: str):
    if name == "Broker":
        from .broker import Broker
        return Broker
    raise AttributeError(name)

__all__ = ["Broker", "Store"]
