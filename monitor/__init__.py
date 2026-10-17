"""China energy watchlist monitor.

Runs as short-lived GitHub Actions jobs. Deterministic Python gathers prices,
exchange filings and news; headless Claude Code turns them into messages.

Modules used by the cheap ``plan`` step (``state``, ``telegram``, ``schedule``)
must stay standard-library only — they run before dependencies are installed.
"""
