import json
import sys
import textwrap
from pathlib import Path

from src.models.manager import ModelManager
from src.utils.config import Config


def _write_config(tmp_path: Path, model_priority=None) -> Path:
    model_priority = model_priority or []
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            f"""
            app:
              data_dir: "data"
              log_dir: "logs"
              model_cache_file: "data/models_cache.json"
              failure_log_file: "logs/failures.jsonl"
              proxy_log_file: "logs/proxy.log"
            model_priority: {json.dumps(model_priority, ensure_ascii=False)}
            modelscope:
              fetch_script_path: ""
            proxy:
              host: "127.0.0.1"
              port: 8080
              upstream_base_url: "https://api-inference.modelscope.cn"
              upstream_api_key_env: "MS_CLAUDE_UPSTREAM_API_KEY"
              upstream_api_key: ""
              upstream_type: "openai"
            """
        ),
        encoding="utf-8",
    )
    return config_path


def test_update_from_modelscope_uses_fetch_script_and_respects_priority(
    tmp_path, monkeypatch
):
    home_dir = tmp_path / "home"
    home_dir.mkdir()

    script_path = tmp_path / "fetch_text_generation_models.py"
    script_path.write_text(
        textwrap.dedent(
            """
            #!/usr/bin/env python3
            import json
            import sys

            print(json.dumps(["alpha-model", "beta-model", "alpha-model"]))
            """
        ),
        encoding="utf-8",
    )

    captured_env = {}

    def fake_run(cmd, check, capture_output, text, env):
        captured_env.update(env)
        assert cmd[0] == sys.executable
        assert cmd[1] == str(script_path)
        return type(
            "Result",
            (),
            {
                "returncode": 0,
                "stdout": json.dumps(["alpha-model", "beta-model", "alpha-model"]),
                "stderr": "",
            },
        )()

    monkeypatch.setenv("MS_CLAUDE_MODEL_FETCHER", str(script_path))
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setenv("NO_PROXY", "localhost")
    monkeypatch.setattr("src.models.manager.subprocess.run", fake_run)

    config = Config(str(_write_config(tmp_path, model_priority=["beta-model"])), home_path=str(home_dir))
    manager = ModelManager(config)

    assert manager.get_priority_order() == ["beta-model", "alpha-model"]
    assert [model.name for model in manager.get_all_models()] == [
        "beta-model",
        "alpha-model",
    ]
    assert "HTTP_PROXY" not in captured_env
    assert "HTTPS_PROXY" not in captured_env
    assert "ALL_PROXY" not in captured_env
    assert "NO_PROXY" not in captured_env


def test_model_manager_ignores_duplicate_models(
    tmp_path, monkeypatch
):
    home_dir = tmp_path / "home"
    home_dir.mkdir()

    script_path = tmp_path / "fetch_text_generation_models.py"
    script_path.write_text(
        "print('[\"alpha\", \"alpha\", \"beta\"]')\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("MS_CLAUDE_MODEL_FETCHER", str(script_path))
    monkeypatch.setattr(
        "src.models.manager.subprocess.run",
        lambda *args, **kwargs: type(
            "Result",
            (),
            {
                "returncode": 0,
                "stdout": "[\"alpha\", \"alpha\", \"beta\"]",
                "stderr": "",
            },
        )(),
    )

    config = Config(str(_write_config(tmp_path)), home_path=str(home_dir))
    manager = ModelManager(config)

    assert [model.name for model in manager.get_all_models()] == ["alpha", "beta"]
