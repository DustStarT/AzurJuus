from pathlib import Path

from backend.config import ROOT, get_settings


def test_dotenv_relative_paths_are_rooted_at_application(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AZURJUUS_WORKSPACE_STATE_PATH", ".azurjuus/workspace-v2.json")
    monkeypatch.setenv("AZURJUUS_WORKSPACE_ROOT", ".")
    monkeypatch.setenv("AZURJUUS_CHROMA_PATH", ".azurjuus/chroma")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.workspace_state_path == ROOT / ".azurjuus/workspace-v2.json"
        assert settings.default_workspace_root == ROOT
        assert settings.chroma_path == ROOT / ".azurjuus/chroma"
    finally:
        get_settings.cache_clear()
