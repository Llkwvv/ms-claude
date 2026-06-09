"""
Failover（成功失败轮换）端到端测试

测试目标：
1. blacklist 能让高优先级模型被跳过
2. 请求失败后 excluded 机制能切换到下一个模型
3. failure tracker 超过阈值后模型被禁用
4. 所有模型都失败时返回正确错误
5. 请求成功后清除 Model 统计和 failure tracker
6. 冷却时间机制生效
"""

import json
import textwrap
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.server.proxy_server import ProxyService
from src.utils.config import Config
from src.models.model import ModelStatus


def _write_config(tmp_path: Path, model_priority=None, blacklist=None) -> Path:
    model_priority = model_priority or []
    blacklist = blacklist or []
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
              last_success_model_file: "data/last_success_model.json"
            model_priority: {json.dumps(model_priority, ensure_ascii=False)}
            modelscope:
              fetch_script_path: ""
            proxy:
              host: "127.0.0.1"
              port: 8080
              upstream_base_url: "https://api-inference.modelscope.cn"
              upstream_api_key: "test-key"
              upstream_api_key_env: ""
              upstream_type: "openai"
            blacklist: {json.dumps(blacklist, ensure_ascii=False)}
            failure_tracking:
              enabled: true
              log_file: "logs/failures.jsonl"
              failure_threshold: 5
              time_window: 3600
            quota:
              max_retries: 3
            """
        ),
        encoding="utf-8",
    )
    return config_path


def test_blacklist_skips_model(tmp_path, monkeypatch):
    """黑名单中的模型应该被跳过，调度到下一个可用模型。"""
    home_dir = tmp_path / "home"
    home_dir.mkdir()

    config = Config(
        str(_write_config(
            tmp_path,
            model_priority=["model-a", "model-b", "model-c"],
            blacklist=[{"model": "model-a", "reason": "test"}],
        )),
        home_path=str(home_dir),
    )

    service = ProxyService(config)
    models = service.model_proxy.model_manager.get_models_by_priority()

    # model-a 虽然配置在优先级第一位，但不应被选中
    excluded = set()
    selected = service._select_model_excluding({"model": "default"}, excluded)
    assert selected is not None
    assert selected.name == "model-b"


def test_failover_switches_to_next_model_on_http_error(tmp_path, monkeypatch):
    """第一个模型返回 HTTP 错误时，应自动切换到第二个模型。"""
    home_dir = tmp_path / "home"
    home_dir.mkdir()

    config = Config(
        str(_write_config(
            tmp_path,
            model_priority=["model-a", "model-b"],
        )),
        home_path=str(home_dir),
    )

    service = ProxyService(config)

    # mock session.post: model-a 失败，model-b 成功
    def mock_post(url, headers=None, json=None, timeout=None, stream=False):
        model = json.get("model", "")
        resp = MagicMock()
        if model == "model-a":
            resp.ok = False
            resp.status_code = 503
            resp.text = '{"error": {"message": "Service Unavailable"}}'
            resp.json.return_value = {"error": {"message": "Service Unavailable"}}
            resp.headers = {"content-type": "application/json"}
        else:
            resp.ok = True
            resp.status_code = 200
            resp.json.return_value = {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "choices": [{"message": {"content": "hello"}}],
            }
            resp.headers = {"content-type": "application/json"}
        return resp

    monkeypatch.setattr(service.session, "post", mock_post)

    status, headers, body, is_stream = service._proxy_chat_completions({
        "model": "default",
        "messages": [{"role": "user", "content": "hi"}],
    })

    assert status == 200
    assert body["choices"][0]["message"]["content"] == "hello"
    # 确认 model-a 被记录为 excluded（体现在内部调用）
    # 由于 mock 无法直接验证 excluded，我们通过失败追踪器验证
    assert service.model_proxy.failure_tracker.is_over_threshold("model-a") is False  # 仅1次失败，未达阈值5
    # 确认 model-a 的 Model 对象统计被更新
    model_a = service.model_proxy.model_manager.get_model("model-a")
    assert model_a.total_requests == 1
    assert model_a.consecutive_failures == 1
    assert model_a.last_failure_time is not None
    # model-b 成功，应记录 model-b 的成功（不是 model-a 的失败被清除）
    model_b = service.model_proxy.model_manager.get_model("model-b")
    assert model_b.total_requests == 1
    assert model_b.successful_requests == 1
    assert service.model_proxy.failure_tracker.get_failure_stats("model-b").get("success", 0) == 1


def test_failover_all_models_failed(tmp_path, monkeypatch):
    """所有模型都失败时，应返回 503 并包含最后错误信息。"""
    home_dir = tmp_path / "home"
    home_dir.mkdir()

    config = Config(
        str(_write_config(
            tmp_path,
            model_priority=["model-a", "model-b"],
        )),
        home_path=str(home_dir),
    )

    service = ProxyService(config)

    def mock_post(url, headers=None, json=None, timeout=None, stream=False):
        resp = MagicMock()
        resp.ok = False
        resp.status_code = 429
        resp.text = '{"error": {"message": "Rate limit"}}'
        resp.json.return_value = {"error": {"message": "Rate limit"}}
        resp.headers = {"content-type": "application/json"}
        return resp

    monkeypatch.setattr(service.session, "post", mock_post)

    status, headers, body, is_stream = service._proxy_chat_completions({
        "model": "default",
        "messages": [{"role": "user", "content": "hi"}],
    })

    assert status == 503
    assert "All models failed" in body["error"]["message"]
    assert "Rate limit" in body["error"]["message"]


def test_failure_threshold_disables_model(tmp_path, monkeypatch):
    """连续失败达到阈值后，模型应被标记为不可用。"""
    home_dir = tmp_path / "home"
    home_dir.mkdir()

    config = Config(
        str(_write_config(
            tmp_path,
            model_priority=["model-a", "model-b"],
        )),
        home_path=str(home_dir),
    )

    service = ProxyService(config)

    # 注入 5 次失败，触发阈值
    for _ in range(5):
        service.model_proxy.failure_tracker.record_failure(
            "model-a", "timeout", {}
        )

    assert service.model_proxy.failure_tracker.is_over_threshold("model-a") is True

    # _is_model_usable 应该拒绝 model-a
    excluded = set()
    assert service._is_model_usable(
        service.model_proxy.model_manager.get_model("model-a"), excluded
    ) is False


def test_excluded_set_prevents_reuse(tmp_path, monkeypatch):
    """同一个请求中已失败的模型不应被再次尝试。"""
    home_dir = tmp_path / "home"
    home_dir.mkdir()

    config = Config(
        str(_write_config(
            tmp_path,
            model_priority=["model-a", "model-b", "model-c"],
        )),
        home_path=str(home_dir),
    )

    service = ProxyService(config)

    call_models = []

    def mock_post(url, headers=None, json=None, timeout=None, stream=False):
        model = json.get("model", "")
        call_models.append(model)
        resp = MagicMock()
        resp.ok = False
        resp.status_code = 500
        resp.text = "error"
        resp.json.return_value = {"error": "error"}
        resp.headers = {"content-type": "application/json"}
        return resp

    monkeypatch.setattr(service.session, "post", mock_post)

    service._proxy_chat_completions({
        "model": "default",
        "messages": [{"role": "user", "content": "hi"}],
    })

    # max_retries=3，所以最多尝试 3 个不同模型
    assert call_models == ["model-a", "model-b", "model-c"]


def test_stream_request_failover(tmp_path, monkeypatch):
    """流式请求失败时也应切换到下一个模型。"""
    home_dir = tmp_path / "home"
    home_dir.mkdir()

    config = Config(
        str(_write_config(
            tmp_path,
            model_priority=["model-a", "model-b"],
        )),
        home_path=str(home_dir),
    )

    service = ProxyService(config)

    def mock_post(url, headers=None, json=None, timeout=None, stream=False):
        model = json.get("model", "")
        resp = MagicMock()
        if model == "model-a":
            resp.ok = False
            resp.status_code = 503
            resp.text = "error"
            resp.json.return_value = {"error": "error"}
            resp.headers = {"content-type": "application/json"}
        else:
            resp.ok = True
            resp.status_code = 200
            resp.iter_content.return_value = [b"data: test\n\n"]
            resp.headers = {"content-type": "text/event-stream"}
        return resp

    monkeypatch.setattr(service.session, "post", mock_post)

    status, headers, body, is_stream = service._proxy_chat_completions({
        "model": "default",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    })

    assert status == 200
    assert is_stream is True


def test_cooldown_blocks_repeated_attempts(tmp_path, monkeypatch):
    """模型失败后应进入冷却期，短时间内不应被再次选中。"""
    home_dir = tmp_path / "home"
    home_dir.mkdir()

    config = Config(
        str(_write_config(
            tmp_path,
            model_priority=["model-a", "model-b"],
        )),
        home_path=str(home_dir),
    )

    service = ProxyService(config)
    model_a = service.model_proxy.model_manager.get_model("model-a")

    # 模拟 model-a 刚刚失败过
    model_a.last_failure_time = time.time()
    model_a.consecutive_failures = 1

    excluded = set()
    assert service._is_model_usable(model_a, excluded) is False
    assert service._is_in_cooldown(model_a) is True


def test_anthropic_error_format_on_all_failed(tmp_path, monkeypatch):
    """_proxy_messages 所有模型失败时应返回 Anthropic 格式的错误。"""
    home_dir = tmp_path / "home"
    home_dir.mkdir()

    config = Config(
        str(_write_config(
            tmp_path,
            model_priority=["model-a"],
        )),
        home_path=str(home_dir),
    )

    service = ProxyService(config)

    def mock_post(url, headers=None, json=None, timeout=None, stream=False):
        resp = MagicMock()
        resp.ok = False
        resp.status_code = 500
        resp.text = "error"
        resp.json.return_value = {"error": "boom"}
        resp.headers = {"content-type": "application/json"}
        return resp

    monkeypatch.setattr(service.session, "post", mock_post)

    status, headers, body, is_stream = service._proxy_messages({
        "model": "claude-sonnet",
        "messages": [{"role": "user", "content": "hi"}],
    })

    assert status == 503
    assert body["type"] == "error"
    assert "api_error" in body["error"]["type"]
    assert "All models failed" in body["error"]["message"]


def test_forced_model_reloaded_on_config_change(tmp_path, monkeypatch):
    """配置热重载时应重新加载 forced_model。"""
    home_dir = tmp_path / "home"
    home_dir.mkdir()

    config = Config(
        str(_write_config(
            tmp_path,
            model_priority=["model-a", "model-b"],
        )),
        home_path=str(home_dir),
    )

    service = ProxyService(config)
    assert service._forced_model == ""

    # 模拟配置变更：设置 default_model
    config.set("proxy.default_model", "model-b")
    service._on_config_changed({}, {})
    assert service._forced_model == "model-b"
