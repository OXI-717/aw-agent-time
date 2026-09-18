import importlib
from pathlib import Path

import pytest

import tracker_config


def test_default_config_is_neutral_and_repo_local(monkeypatch, tmp_path):
    monkeypatch.delenv("AW_TRACKER_CONFIG", raising=False)
    monkeypatch.delenv("AW_TRACKER_BUCKET_PREFIX", raising=False)
    monkeypatch.setattr(tracker_config, "REPO_ROOT", tmp_path)
    cfg = tracker_config.load_config()

    assert cfg.bucket_prefix == "aw-agent-time"
    assert cfg.launchd_label_prefix == "com.example.aw-agent-time"
    assert cfg.categories_file.name == "categories.example.json"
    assert cfg.project_manifest is None
    assert cfg.obsidian_folder is None
    assert cfg.layer25_project_roots == []


def test_config_file_and_env_precedence(monkeypatch, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        """
[tracker]
bucket_prefix = "from-file"
client_name = "file-client"

[paths]
categories_file = "custom-categories.json"
obsidian_folder = "~/Notes/time"
layer25_project_roots = ["~/src", "/tmp/projects"]

[classification]
project_umbrellas = { repo-a = "Umbrella A" }

[launchd]
label_prefix = "com.file.aw"
python = "/usr/bin/python3"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("AW_TRACKER_BUCKET_PREFIX", "from-env")
    monkeypatch.setenv("AW_TRACKER_CATEGORIES_FILE", str(tmp_path / "env-cats.json"))

    cfg = tracker_config.load_config(config_path=config)

    assert cfg.bucket_prefix == "from-env"
    assert cfg.client_name == "file-client"
    assert cfg.categories_file == tmp_path / "env-cats.json"
    assert cfg.layer25_project_roots == [Path("~/src").expanduser(), Path("/tmp/projects")]
    assert cfg.project_umbrellas == {"repo-a": "Umbrella A"}
    assert cfg.launchd_label_prefix == "com.file.aw"
    assert cfg.python == "/usr/bin/python3"


def test_modules_pick_up_config_without_private_defaults(monkeypatch, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        """
[tracker]
bucket_prefix = "public-prefix"
client_name = "public-client"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("AW_TRACKER_CONFIG", str(config))

    import lib_aw

    try:
        importlib.reload(tracker_config)
        lib_aw = importlib.reload(lib_aw)
        assert lib_aw.bucket_name("states").startswith("public-prefix-states_")
        assert lib_aw.TRACKER_CONFIG.client_name == "public-client"
    finally:
        monkeypatch.delenv("AW_TRACKER_CONFIG", raising=False)
        importlib.reload(tracker_config)
        importlib.reload(lib_aw)
