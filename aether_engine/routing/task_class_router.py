"""Compatibility shim for infer_task_class.

Historical import path for :func:`aether_engine.routing.signals.infer_task_class`.
The canonical implementation now lives in :mod:`aether_engine.routing.signals`.
"""

from aether_engine.routing.signals import infer_task_class

__all__ = ["infer_task_class"]
