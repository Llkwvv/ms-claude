import json
from pathlib import Path

from src.main import _prepare_claude_settings, _strip_proxy_env_inplace


def test_prepare_claude_settings_creates_isolated_settings(tmp_path):
    settings_path = tmp_path / "isolated-home" / ".claude" / "settings.json"

    _prepare_claude_settings(
        settings_path=settings_path,
        host="127.0.0.1",
        port=8080,
    )

    assert settings_path.exists()
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    assert data["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8080"
    assert data["env"]["ANTHROPIC_AUTH_TOKEN"] == "PROXY_MANAGED"
    assert data["permissions"]["defaultMode"] == "auto"


def test_prepare_claude_settings_backs_up_existing_file(tmp_path):
    settings_path = tmp_path / "home" / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "keep-me"}}),
        encoding="utf-8",
    )

    _prepare_claude_settings(
        settings_path=settings_path,
        host="127.0.0.1",
        port=8081,
    )

    backup_path = settings_path.with_suffix(settings_path.suffix + ".bak")
    assert backup_path.exists()
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    assert data["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8081"
    assert data["env"]["ANTHROPIC_AUTH_TOKEN"] == "keep-me"


def test_strip_proxy_env_inplace():
    env = {
        "HTTP_PROXY": "http://127.0.0.1:7897",
        "HTTPS_PROXY": "http://127.0.0.1:7897",
        "ALL_PROXY": "http://127.0.0.1:7897",
        "http_proxy": "http://127.0.0.1:7897",
        "keep": "value",
    }

    # 默认不剥离代理环境
    _strip_proxy_env_inplace(env)

    assert env["keep"] == "value"
    assert env["HTTP_PROXY"] == "http://127.0.0.1:7897"
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:7897"
