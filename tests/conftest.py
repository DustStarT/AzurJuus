import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app import create_app
from backend.config import get_settings


def configure_test_env(monkeypatch, tmp_path: Path, *, social_enabled: bool = False) -> None:
    """Isolated database, workspace and provider settings for one test."""
    monkeypatch.setenv("AZURJUUS_EXPRESSION_ENABLED", "0")
    monkeypatch.setenv("AZURJUUS_MIND_ENABLED", "0")
    monkeypatch.setenv("AZURJUUS_EXECUTION_BACKEND", "hermes")
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AZURJUUS_DATABASE_URL", f"sqlite+pysqlite:///{(tmp_path / 'azurjuus.db').as_posix()}")
    monkeypatch.setenv("AZURJUUS_REDIS_URL", "")
    monkeypatch.setenv("AZURJUUS_CHROMA_URL", "http://127.0.0.1:65535")
    monkeypatch.setenv("AZURJUUS_CHROMA_PATH", str(tmp_path / "chroma"))
    monkeypatch.setenv("AZURJUUS_WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("AZURJUUS_WORKSPACE_STATE_PATH", str(tmp_path / "workspace-state.json"))
    monkeypatch.setenv("AZURJUUS_SOCIAL_ENABLED", "1" if social_enabled else "0")
    monkeypatch.setenv("AZURJUUS_SOCIAL_TICK_SECONDS", "9999")
    get_settings.cache_clear()


def open_client(monkeypatch, tmp_path: Path, *, social_enabled: bool = False):
    from fastapi.testclient import TestClient

    configure_test_env(monkeypatch, tmp_path, social_enabled=social_enabled)
    return TestClient(create_app())
