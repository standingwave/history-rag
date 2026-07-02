"""macOS app-usage tracker: a sampling daemon plus shared storage.

The daemon (daemon.py) records per-app time into ~/.claude/appusage.db;
sources/appusage.py feeds finalized daily totals into the history RAG index.
"""
