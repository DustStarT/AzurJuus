from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency
    load_dotenv = None


ROOT = Path(__file__).resolve().parent.parent
APPDATA_ROOT = ROOT / ".azurjuus"

if load_dotenv is not None:
    load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    app_name: str
    host: str
    port: int
    database_url: str
    redis_url: str
    chroma_url: str
    chroma_collection: str
    chroma_path: Path
    default_workspace_root: Path
    workspace_state_path: Path
    llm_timeout_seconds: float
    social_enabled: bool
    social_tick_seconds: float
    workflow_runtime_enabled: bool
    workflow_tick_seconds: float


def _getenv_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _project_path(key: str, default: Path) -> Path:
    path = Path(os.getenv(key) or str(default)).expanduser()
    return (path if path.is_absolute() else ROOT / path).resolve()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    APPDATA_ROOT.mkdir(parents=True, exist_ok=True)
    return Settings(
        app_name="AzurJuus",
        host=os.getenv("AZURJUUS_HOST", "127.0.0.1"),
        port=int(os.getenv("AZURJUUS_PORT", "4173")),
        database_url=os.getenv(
            "AZURJUUS_DATABASE_URL",
            f"sqlite+pysqlite:///{(APPDATA_ROOT / 'azurjuus.db').as_posix()}",
        ),
        redis_url=os.getenv("AZURJUUS_REDIS_URL", ""),
        chroma_url=os.getenv("AZURJUUS_CHROMA_URL", ""),
        chroma_collection=os.getenv("AZURJUUS_CHROMA_COLLECTION", "azurjuus_memory"),
        chroma_path=_project_path("AZURJUUS_CHROMA_PATH", APPDATA_ROOT / "chroma"),
        default_workspace_root=_project_path("AZURJUUS_WORKSPACE_ROOT", ROOT),
        workspace_state_path=_project_path("AZURJUUS_WORKSPACE_STATE_PATH", APPDATA_ROOT / "workspace-v2.json"),
        llm_timeout_seconds=float(os.getenv("AZURJUUS_LLM_TIMEOUT", "45")),
        social_enabled=_getenv_bool("AZURJUUS_SOCIAL_ENABLED", True),
        social_tick_seconds=float(os.getenv("AZURJUUS_SOCIAL_TICK_SECONDS", "20")),
        workflow_runtime_enabled=_getenv_bool("AZURJUUS_WORKFLOW_RUNTIME_ENABLED", True),
        workflow_tick_seconds=float(os.getenv("AZURJUUS_WORKFLOW_TICK_SECONDS", "12")),
    )
