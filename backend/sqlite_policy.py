"""Avoid the WAL-reset race on unpatched SQLite runtimes.

Version thresholds: https://sqlite.org/wal.html#walresetbug
"""
import sqlite3


def journal_mode(version=None):
    v = version or sqlite3.sqlite_version_info
    fixed = v >= (3, 51, 3) or (v[:2] == (3, 50) and v[2] >= 7) or (v[:2] == (3, 44) and v[2] >= 6)
    return "WAL" if fixed else "DELETE"
