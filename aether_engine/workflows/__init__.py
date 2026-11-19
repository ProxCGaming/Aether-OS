"""Workflows (Full Furnish W-9): named, saved, ordered lists of
task prompts, run in order as ordinary Aether tasks.

Two halves:

- :mod:`aether_engine.workflows.store` — JSON persistence for
  workflow definitions and run records (durable across restart,
  honest errors, no silent drops).
- :mod:`aether_engine.workflows.runner` — the in-process run
  driver: one chat session per run, steps launched strictly in
  order through the ordinary START_TASK path, each step's result
  carried into the next step's prompt as a bounded excerpt.

The websocket surface lives in ``aether_engine/routes/ws_workflows.py``.
"""
