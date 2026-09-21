from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.database import session_scope
from conftest import configure_test_env


def test_workspace_settings_and_ui_session_persist(monkeypatch, tmp_path: Path) -> None:
    configure_test_env(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        workspace = client.get("/api/workspace/load").json()["workspace"]
        assert workspace["session"]["actorId"] == "commander"
        assert workspace["session"]["authMode"] == "local_cookie"
        workspace["settings"]["llmModel"] = "gpt-4.1"
        workspace["settings"]["llmApiKey"] = "test-key"
        workspace["settings"]["authorizedWorkspaceRoot"] = str(tmp_path / "workspace")
        workspace["settings"]["searchApiKey"] = "search-key"
        workspace["uiSession"]["view"] = "circle"
        workspace["uiSession"]["activeConversationId"] = "port-hub"

        saved = client.post("/api/workspace/save", json={"workspace": workspace})
        assert saved.status_code == 200

    with TestClient(create_app()) as client:
        workspace = client.get("/api/workspace/load").json()["workspace"]
        assert workspace["settings"]["llmModel"] == "gpt-4.1"
        for key in ("llmApiKey", "searchApiKey"):
            assert key not in workspace["settings"]
            assert workspace["settings"][key + "Configured"] is True
        with session_scope() as session:
            service = client.app.state.service
            private = service.serialize_settings(service.get_workspace(session))
            assert private["llmApiKey"] == "test-key"
            assert private["searchApiKey"] == "search-key"
        assert workspace["uiSession"]["view"] == "circle"
        assert workspace["uiSession"]["activeConversationId"] == "port-hub"
        assert workspace["session"]["actorId"] == "commander"
        bootstrap = client.get("/api/bootstrap").json()["workspace"]
        assert bootstrap["session"]["actorId"] == "commander"
        assert bootstrap["session"]["approvalMode"] == "server_managed_secretary"


def test_social_publish_and_comment(monkeypatch, tmp_path: Path) -> None:
    configure_test_env(monkeypatch, tmp_path)
    # Moments is paused in the product; exercise its retained API in isolation.
    monkeypatch.setattr('backend.idle_social.MOMENTS_ENABLED', True)
    with TestClient(create_app()) as client:
        bootstrap = client.get("/api/bootstrap").json()["snapshot"]
        author_id = bootstrap["agents"][0]["id"]
        publish = client.post(
            "/api/posts/publish",
            json={"authorId": author_id, "body": "任务之后，先在这里记下一点此刻的想法。"},
        )
        assert publish.status_code == 200
        snapshot = publish.json()["snapshot"]
        post = snapshot["posts"][0]

        comment = client.post(
            "/api/posts/comment",
            json={"postId": post["id"], "body": "指挥官已阅，这条感想我收到了。"},
        )
        assert comment.status_code == 200
        snapshot = comment.json()["snapshot"]
        post = next(item for item in snapshot["posts"] if item["id"] == post["id"])
        assert any(item["authorId"] == "commander" for item in post["comments"])


def test_social_post_creation_requires_shareable_experience(monkeypatch, tmp_path: Path) -> None:
    """Idle posting is driven by experience, not only by elapsed time."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from backend.idle_social import run_idle_social
    from backend.models import SocialPost

    configure_test_env(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        client.get("/api/bootstrap")
        service = client.app.state.service
        with session_scope() as session:
            session.execute(update(SocialPost).values(created_at=datetime.now(UTC) - timedelta(days=1)))
            # Aged posts alone must not schedule a new one.
            assert asyncio.run(run_idle_social(service, lambda: False)) is None
