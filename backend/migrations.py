from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

from .credentials import protect


def backup_cognition_migration(engine, settings):
    """Back up both stores and configuration before registering v2 tables."""
    if engine.dialect.name != 'sqlite' or engine.url.database == ':memory:':
        return
    with engine.connect() as connection:
        if not engine.dialect.has_table(connection, 'actors') or engine.dialect.has_table(connection, 'mind_states'):
            return
    import shutil
    folder = settings.workspace_state_path.parent / 'migration-backups' / (datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '-cognition-v2')
    folder.mkdir(parents=True, exist_ok=True)
    for label, source in [('business.db', Path(engine.url.database)), ('runs.db', settings.workspace_state_path.parent / 'runs.db')]:
        if source.exists():
            with sqlite3.connect(source) as src, sqlite3.connect(folder / label) as dst:
                src.backup(dst)
    if settings.workspace_state_path.exists():
        shutil.copy2(settings.workspace_state_path, folder / 'workspace.json')


def migrate(engine):
    """Additive migrations; back up the original SQLite before changing user rows."""
    with engine.connect() as conn:
        if engine.dialect.has_table(conn, "azur_schema_migrations"):
            if conn.execute(text("SELECT version FROM azur_schema_migrations WHERE version=1")).first():
                return
    if engine.dialect.name == "sqlite" and engine.url.database != ":memory:":
        source = Path(engine.url.database).resolve()
        folder = source.parent / "migration-backups"
        folder.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(source) as src, sqlite3.connect(folder / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-v0.db")) as dst:
            src.backup(dst)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS azur_schema_migrations(version INTEGER PRIMARY KEY)"))
        for row in conn.execute(text("SELECT id,llm_api_key,tool_api_key,ui_session_json FROM workspace_settings")).mappings():
            ui = json.loads(row["ui_session_json"]) if isinstance(row["ui_session_json"], str) else row["ui_session_json"] or {}
            for key, value in (ui.get("settingsExtras") or {}).items():
                if key.lower().endswith("apikey") and value:
                    ui["settingsExtras"][key] = protect(value)
            conn.execute(text("UPDATE workspace_settings SET llm_api_key=:llm,tool_api_key=:tool,ui_session_json=:ui WHERE id=:id"), {"id": row["id"], "llm": protect(row["llm_api_key"] or ""), "tool": protect(row["tool_api_key"] or ""), "ui": json.dumps(ui, ensure_ascii=False)})
        conn.execute(text("UPDATE workflows SET status='paused' WHERE status NOT IN ('completed','cancelled','failed')"))
        conn.execute(text("INSERT INTO azur_schema_migrations VALUES(1)"))
