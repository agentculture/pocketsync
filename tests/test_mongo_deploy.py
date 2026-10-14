"""Static checks on the pocketsync-mongo deployment (deploy/mongo)."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "deploy" / "mongo" / "docker-compose.yml"


def _service() -> dict:
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return data["services"]["pocketsync-mongo"]


def test_image_is_atlas_local_and_pinned():
    image = _service()["image"]
    name, _, tag = image.partition(":")
    assert name == "mongodb/mongodb-atlas-local"
    assert tag and tag not in {"latest", "preview"}
    assert tag[0].isdigit()


def test_container_name_and_restart_policy():
    svc = _service()
    assert svc["container_name"] == "pocketsync-mongo"
    assert svc["restart"] == "unless-stopped"


def test_port_is_loopback_only():
    assert _service()["ports"] == ["127.0.0.1:27019:27017"]


def test_bind_mounts_cover_db_configdb_and_mongot():
    mounts = {}
    for vol in _service()["volumes"]:
        src, dst = vol.rsplit(":", 1)
        mounts[dst] = src
    assert set(mounts) == {"/data/db", "/data/configdb", "/data/mongot"}
    for src in mounts.values():
        assert "POCKETSYNC_DATA_DIR" in src
        assert ".local/share/pocketsync/mongo" in src


def test_credentials_come_from_env_file_not_inline():
    svc = _service()
    assert svc["env_file"] == [".env"]
    assert "environment" not in svc
    example = (COMPOSE.parent / ".env.example").read_text(encoding="utf-8")
    assert "MONGODB_INITDB_ROOT_USERNAME=" in example
    assert "MONGODB_INITDB_ROOT_PASSWORD=CHANGE_ME" in example


def test_gitignore_covers_env():
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "deploy/mongo/.env" in [ln.strip() for ln in lines]


def test_fixed_hostname_so_replset_survives_recreate():
    assert _service()["hostname"] == "pocketsync-mongo"
