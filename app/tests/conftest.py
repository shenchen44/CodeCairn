import os
import shutil
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ["DATABASE_URL"] = "sqlite:///./test_codecairn.db"
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("WORKSPACE_ROOT", str(Path.cwd() / ".tmp-test-workspaces"))

from app.db.base import Base
from app.db.session import get_db
from app.main import app


TEST_DB_URL = os.environ["DATABASE_URL"]
engine = create_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture(autouse=True)
def reset_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db_session():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def client():
    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def sample_issue_payload() -> dict:
    return {
        "action": "opened",
        "installation": {"id": 999},
        "repository": {
            "id": 123,
            "name": "demo-repo",
            "full_name": "octo/demo-repo",
            "default_branch": "main",
            "owner": {"login": "octo"},
        },
        "issue": {
            "id": 456,
            "number": 7,
            "title": "Handle None display name",
            "body": "When display_name is None, formatting crashes.",
            "state": "open",
            "html_url": "https://github.com/octo/demo-repo/issues/7",
            "labels": [{"name": "bug"}],
        },
    }


@pytest.fixture()
def workspace_tmp_dir() -> Path:
    base = Path.cwd() / "app" / "tests" / ".runtime_tmp"
    base.mkdir(parents=True, exist_ok=True)
    path = base / str(uuid.uuid4())
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
