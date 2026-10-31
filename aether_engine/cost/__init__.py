"""Cost observability for AETHER (Epic 8, E8-A8.1).

``aether_engine.cost.ledger`` is the persistent per-call usage ledger
(``~/.aether/usage.db``); ``aether_engine.cost.pricing`` prices calls
via LiteLLM with a fallback table. Submodules are imported directly —
this package stays import-light on purpose (Epic 9 lazy-import
discipline).
"""
