"""
HTTP proxy server for Claude-compatible clients.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterable, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests

from ..core.proxy import ModelProxy
from ..models.model import Model, ModelStatus
from ..utils.config import Config

logger = logging.getLogger(__name__)


class ThreadedProxyHTTPServer(ThreadingHTTPServer):
    """HTTP server with attached proxy service."""

    def __init__(self, server_address, RequestHandlerClass, proxy_service):
        super().__init__(server_address, RequestHandlerClass)
        self.proxy_service = proxy_service


class ProxyService:
    """Implements Claude-compatible endpoints backed by an upstream API."""

    def __init__(self, config: Config, model_proxy: Optional[ModelProxy] = None):
        self.config = config
        self.model_proxy = model_proxy or ModelProxy(config)
        self.logger = logging.getLogger(__name__)
        self.session = requests.Session()
        self._last_successful_model: Optional[str] = None
        self._last_success_model_file = config.resolve_path(
            config.get("app.last_success_model_file", "data/last_success_model.json"),
            "data/last_success_model.json"
        )
        self._load_last_successful_model()
        # 注册配置变更回调
        self.config.register_change_callback(self._on_config_changed)

        # 加载模型分组配置
        self._model_groups: Dict[str, set[str]] = {}
        self._model_group_aliases: Dict[str, str] = {}
        self._load_model_groups()

        # 加载黑名单（配置中的模型调度时直接跳过）
        self._blacklist: set[str] = set()
        self._blacklist_reasons: Dict[str, str] = {}
        self._load_blacklist()

        self.upstream_base_url = (
            self.config.get("proxy.upstream_base_url", "") or ""
        ).rstrip("/")
        self.upstream_type = (
            self.config.get("proxy.upstream_type", "openai") or "openai"
        ).lower()
        self.timeout = self.config.get("proxy.timeout", 60)
        self.api_key = self._resolve_api_key()

        # 用户强制指定的模型（环境变量或配置）
        self._forced_model = os.environ.get("MS_CLAUDE_MODEL", "") or ""
        if not self._forced_model:
            self._forced_model = self.config.get("proxy.default_model", "") or ""

        # 启动时显示当前模型配置
        self._log_startup_models()

    def _log_startup_models(self) -> None:
        """启动时显示当前模型配置。"""
        models = self.model_proxy.model_manager.get_models_by_priority()
        available = [m for m in models if m.status == ModelStatus.AVAILABLE]
        blacklisted = sorted(self._blacklist)

        self.logger.info("=" * 50)
        self.logger.info("Loaded %d models, %d available", len(models), len(available))
        if self._forced_model:
            self.logger.info("Forced model (override): %s", self._forced_model)
        elif available:
            self.logger.info("Default model: %s", available[0].name)
            if len(available) > 1:
                self.logger.info(
                    "Fallback chain: %s",
                    " -> ".join(m.name for m in available[:5])
                    + (" ..." if len(available) > 5 else "")
                )
        else:
            self.logger.warning("No available models loaded!")
        if blacklisted:
            self.logger.info("Blacklisted: %s", blacklisted)
        self.logger.info("=" * 50)

    def _resolve_api_key(self) -> str:
        api_key = self.config.get("proxy.upstream_api_key", "") or ""
        if api_key:
            return api_key

        api_key_env = self.config.get("proxy.upstream_api_key_env", "") or ""
        if api_key_env:
            return os.environ.get(api_key_env, "")

        fallback_key = os.environ.get("MS_CLAUDE_UPSTREAM_API_KEY", "")
        if fallback_key:
            return fallback_key

        return ""

    def _load_blacklist(self) -> None:
        """加载黑名单配置。"""
        blacklist_config = self.config.get("blacklist", [])
        if not blacklist_config:
            return
        for entry in blacklist_config:
            if isinstance(entry, dict):
                model_name = entry.get("model", "")
                reason = entry.get("reason", "")
            elif isinstance(entry, str):
                model_name = entry
                reason = ""
            else:
                continue
            if model_name:
                self._blacklist.add(model_name)
                if reason:
                    self._blacklist_reasons[model_name] = reason
        if self._blacklist:
            self.logger.info(
                "Loaded %d blacklisted models: %s",
                len(self._blacklist),
                sorted(self._blacklist),
            )

    def _load_model_groups(self) -> None:
        """加载模型分组配置。"""
        groups_config = self.config.get("model_groups", {})
        for group_name, group_data in groups_config.items():
            if isinstance(group_data, dict):
                models = group_data.get("models", [])
            elif isinstance(group_data, list):
                models = group_data
            else:
                models = []
            self._model_groups[group_name] = set(models)
            self.logger.info(
                "Model group '%s' loaded with %d models", group_name, len(models)
            )

        aliases = self.config.get("model_group_aliases", {})
        self._model_group_aliases = dict(aliases)
        if aliases:
            self.logger.info(
                "Loaded %d model group aliases", len(aliases)
            )

    def _load_last_successful_model(self) -> None:
        """从持久化文件加载上次成功使用的模型。"""
        try:
            if self._last_success_model_file.exists():
                with open(self._last_success_model_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self._last_successful_model = data.get("last_successful_model")
                if self._last_successful_model:
                    self.logger.info(
                        "Loaded last successful model: %s", self._last_successful_model
                    )
        except Exception as e:
            self.logger.warning("Failed to load last successful model: %s", e)

    def _save_last_successful_model(self) -> None:
        """持久化上次成功使用的模型。"""
        if not self._last_successful_model:
            return
        try:
            self._last_success_model_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._last_success_model_file, 'w', encoding='utf-8') as f:
                json.dump({"last_successful_model": self._last_successful_model}, f)
        except Exception as e:
            self.logger.warning("Failed to save last successful model: %s", e)

    def _on_config_changed(self, old_config: Dict[str, Any], new_config: Dict[str, Any]) -> None:
        """配置热重载回调：更新 ProxyService 运行时配置。"""
        self.logger.info("Config hot-reload: updating ProxyService settings")
        self.upstream_base_url = (
            self.config.get("proxy.upstream_base_url", "") or ""
        ).rstrip("/")
        self.upstream_type = (
            self.config.get("proxy.upstream_type", "openai") or "openai"
        ).lower()
        self.timeout = self.config.get("proxy.timeout", 60)
        self.api_key = self._resolve_api_key()
        self._load_model_groups()
        self._blacklist.clear()
        self._blacklist_reasons.clear()
        self._load_blacklist()
        # 重新加载用户强制指定的模型
        self._forced_model = os.environ.get("MS_CLAUDE_MODEL", "") or ""
        if not self._forced_model:
            self._forced_model = self.config.get("proxy.default_model", "") or ""
        self.model_proxy.config = self.config
        self.model_proxy._on_config_changed(old_config, new_config)

    def _resolve_model_group(self, model_name: str) -> Optional[str]:
        """解析模型名对应的分组。"""
        # 直接是组名
        if model_name in self._model_groups:
            return model_name
        # 是别名
        if model_name in self._model_group_aliases:
            return self._model_group_aliases[model_name]
        return None

    def _is_model_usable(self, model: Model, excluded: set[str]) -> bool:
        """检查模型是否可用（未黑名单、未排除、状态正常、未超阈值）。"""
        if model.name in self._blacklist:
            self.logger.debug(
                "Model %s is blacklisted (%s), skipping",
                model.name,
                self._blacklist_reasons.get(model.name, "no reason"),
            )
            return False
        if model.name in excluded:
            return False
        if model.status != ModelStatus.AVAILABLE:
            self.logger.debug("Model %s status: %s", model.name, model.status.value)
            return False
        if self.model_proxy.failure_tracker.is_over_threshold(model.name):
            self.logger.debug("Model %s exceeded failure threshold", model.name)
            return False
        if self._is_in_cooldown(model):
            self.logger.debug("Model %s is in cooldown", model.name)
            return False
        return True

    def build_headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            if self.upstream_type == "anthropic":
                headers["x-api-key"] = self.api_key
                headers["anthropic-version"] = "2023-06-01"
            else:
                headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def list_models(self) -> Dict[str, Any]:
        models = self.model_proxy.model_manager.get_models_by_priority()
        return {
            "object": "list",
            "data": [
                {
                    "id": model.name,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": model.provider,
                }
                for model in models
                if model.name not in self._blacklist
            ],
        }

    def health(self) -> Dict[str, Any]:
        status = self.model_proxy.get_status()
        return {
            "status": "ok",
            "proxy_home": str(self.config.home_path),
            "upstream_base_url": self.upstream_base_url,
            "upstream_type": self.upstream_type,
            "models": status,
        }

    def route_request(
        self,
        path: str,
        payload: Dict[str, Any]
    ) -> Tuple[int, Dict[str, str], Any, bool]:
        if path == "/v1/models":
            return HTTPStatus.OK, {"Content-Type": "application/json"}, self.list_models(), False

        if path == "/health" or path == "/healthz":
            return HTTPStatus.OK, {"Content-Type": "application/json"}, self.health(), False

        if path == "/v1/chat/completions":
            return self._proxy_chat_completions(payload)

        if path == "/v1/messages":
            return self._proxy_messages(payload)

        return HTTPStatus.NOT_FOUND, {"Content-Type": "application/json"}, {
            "error": {"message": f"Unsupported path: {path}", "type": "invalid_request_error"}
        }, False

    def _clamp_max_tokens(self, payload: Dict[str, Any]) -> None:
        """限制 max_tokens 在 ModelScope API 允许范围内（原地修改）。"""
        max_tokens = payload.get("max_tokens")
        if isinstance(max_tokens, int):
            if max_tokens > 16384:
                payload["max_tokens"] = 16384
                self.logger.debug("Capped max_tokens from %d to 16384", max_tokens)
            elif max_tokens < 1:
                payload["max_tokens"] = 1
        elif max_tokens is not None:
            payload["max_tokens"] = 8192

    def _sanitize_payload(self, payload: Dict[str, Any]) -> None:
        """清理 payload，确保数值参数类型正确（原地修改）。
        某些后端对类型校验严格（如 Qwen3 要求 temperature 为 Float）。"""
        # temperature: int -> float
        temp = payload.get("temperature")
        if isinstance(temp, int):
            payload["temperature"] = float(temp)
        # top_p: int -> float
        top_p = payload.get("top_p")
        if isinstance(top_p, int):
            payload["top_p"] = float(top_p)
        # presence_penalty / frequency_penalty: int -> float
        for key in ("presence_penalty", "frequency_penalty"):
            val = payload.get(key)
            if isinstance(val, int):
                payload[key] = float(val)

    def _check_model_switch_command(
        self, payload: Dict[str, Any]
    ) -> Optional[Tuple[int, Dict[str, str], Any, bool]]:
        """检测并处理 !model 切换命令。
        如果用户消息以 '!model ' 开头，提取模型名并切换。"""
        messages = payload.get("messages", [])
        if not messages or not isinstance(messages, list):
            return None

        # 找最后一条 user 消息
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                content_type = type(content).__name__
                # Claude CLI 可能发送 list 格式的 content（多模态），提取文本
                text_content = ""
                if isinstance(content, str):
                    text_content = content
                elif isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            text_content += item.get("text", "")
                        elif isinstance(item, str):
                            text_content += item

                self.logger.info(
                    "Checking user message: content_type=%s text=%r starts_with_model=%s",
                    content_type, text_content[:120], text_content.startswith("@model "),
                )

                if text_content.startswith("@model "):
                    # 只取第一行，防止被 system-reminder 等内容污染
                    model_name = text_content[7:].split("\n")[0].strip()
                    if not model_name:
                        self.logger.info("Empty model name in @model command")
                        return None

                    # 验证模型是否存在
                    model = self.model_proxy.model_manager.get_model(model_name)
                    payload_model = payload.get("model", "claude-sonnet-4-20250514")
                    if not model:
                        self.logger.info("Model not found: %s", model_name)
                        return HTTPStatus.OK, {"Content-Type": "application/json"}, {
                            "id": f"msg_{int(time.time())}",
                            "type": "message",
                            "role": "assistant",
                            "model": payload_model,
                            "content": [{"type": "text", "text": f"Model '{model_name}' not found."}],
                            "stop_reason": "end_turn",
                            "stop_sequence": None,
                            "usage": {"input_tokens": 0, "output_tokens": 0},
                        }, False

                    # 设置强制模型
                    self._forced_model = model_name
                    self.logger.info("Switched to model via command: %s", model_name)
                    return HTTPStatus.OK, {"Content-Type": "application/json"}, {
                        "id": f"msg_{int(time.time())}",
                        "type": "message",
                        "role": "assistant",
                        "model": payload_model,
                        "content": [{"type": "text", "text": f"Switched to model: {model_name}"}],
                        "stop_reason": "end_turn",
                        "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    }, False

                # 恢复自动切换
                if text_content.strip() == "@model auto":
                    self._forced_model = ""
                    self.logger.info("Cleared forced model, back to auto-selection")
                    payload_model = payload.get("model", "claude-sonnet-4-20250514")
                    return HTTPStatus.OK, {"Content-Type": "application/json"}, {
                        "id": f"msg_{int(time.time())}",
                        "type": "message",
                        "role": "assistant",
                        "model": payload_model,
                        "content": [{"type": "text", "text": "Auto model selection restored."}],
                        "stop_reason": "end_turn",
                        "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    }, False

        self.logger.info("No @model command found in messages")
        return None

    def _select_model(self, payload: Dict[str, Any]) -> Model:
        model_name = payload.get("model")
        if model_name:
            model = self.model_proxy.model_manager.get_model(model_name)
            if model:
                return model

        model = self.model_proxy.get_available_model()
        if model:
            return model

        raise RuntimeError("No available model")

    def _select_model_excluding(
        self,
        payload: Dict[str, Any],
        excluded: set[str],
    ) -> Optional[Model]:
        """选择模型，支持分组内切换。
        优先级：强制指定 > 上次成功 > 分组内 > payload 指定 > 全局列表。"""
        requested_model = payload.get("model")

        # 0. 用户通过环境变量或配置强制指定的模型（最高优先级）
        if self._forced_model and self._forced_model not in excluded:
            model = self.model_proxy.model_manager.get_model(self._forced_model)
            if model and self._is_model_usable(model, excluded):
                self.logger.info("Using forced model: %s", self._forced_model)
                return model
            self.logger.warning(
                "Forced model '%s' is not available (blacklisted, disabled, or failed), "
                "falling back to auto-selection",
                self._forced_model,
            )

        # 1. 尝试解析分组
        group_name = None
        if requested_model:
            group_name = self._resolve_model_group(requested_model)

        if group_name:
            self.logger.debug(
                "Model '%s' resolved to group '%s'", requested_model, group_name
            )
            model = self._select_from_group(group_name, excluded)
            if model:
                return model
            self.logger.warning(
                "All models in group '%s' failed, falling back to global list",
                group_name,
            )

        # 2. 无分组匹配：回退到全局逻辑
        # 2a. 上次成功的
        if self._last_successful_model and self._last_successful_model not in excluded:
            model = self.model_proxy.model_manager.get_model(self._last_successful_model)
            if model and self._is_model_usable(model, excluded):
                return model

        # 2b. payload 指定的具体模型
        if requested_model and requested_model not in excluded:
            model = self.model_proxy.model_manager.get_model(requested_model)
            if model and self._is_model_usable(model, excluded):
                return model

        # 2c. 全局优先级列表
        for model in self.model_proxy.model_manager.get_models_by_priority():
            if self._is_model_usable(model, excluded):
                return model
        return None

    def _select_from_group(
        self, group_name: str, excluded: set[str]
    ) -> Optional[Model]:
        """在指定分组内选择模型。"""
        group_models = self._model_groups.get(group_name, set())
        if not group_models:
            self.logger.warning("Group '%s' is empty", group_name)
            return None

        # 1. 上次成功的在组内？
        if (
            self._last_successful_model
            and self._last_successful_model not in excluded
            and self._last_successful_model in group_models
        ):
            model = self.model_proxy.model_manager.get_model(
                self._last_successful_model
            )
            if model and self._is_model_usable(model, excluded):
                self.logger.debug(
                    "Using last successful model in group '%s': %s",
                    group_name,
                    model.name,
                )
                return model

        # 2. 按全局优先级在组内选
        for model in self.model_proxy.model_manager.get_models_by_priority():
            if model.name not in group_models:
                continue
            if self._is_model_usable(model, excluded):
                self.logger.debug(
                    "Selected model from group '%s': %s", group_name, model.name
                )
                return model

        return None

    def _record_model_failure(self, model: Model, error_msg: str) -> None:
        """记录失败。"""
        self.logger.warning("Recording failure for %s: %s", model.name, error_msg)
        self.model_proxy.failure_tracker.record_failure(
            model.name, error_msg, {}
        )
        # 同步更新 Model 对象运行时统计，供冷却时间等机制使用
        model.update_stats(success=False)

    @staticmethod
    def _extract_error_message(response: requests.Response) -> str:
        """从 HTTP 响应中提取错误信息。"""
        try:
            payload = response.json()
            if isinstance(payload, dict):
                err = payload.get("error")
                if isinstance(err, dict):
                    return err.get("message", str(payload))
                return str(err) if err else str(payload)
            return str(payload)
        except Exception:
            return response.text[:200]

    def _proxy_chat_completions(self, payload: Dict[str, Any]) -> Tuple[int, Dict[str, str], Any, bool]:
        if not self.upstream_base_url:
            return self._upstream_not_configured()

        max_retries = self.config.get("quota.max_retries", 3)
        excluded: set[str] = set()
        last_error = "Unknown error"

        for attempt in range(max_retries):
            model = self._select_model_excluding(payload, excluded)
            if not model:
                self.logger.error("No more available models to try")
                break

            excluded.add(model.name)
            upstream_payload = dict(payload)
            upstream_payload["model"] = model.name
            self._clamp_max_tokens(upstream_payload)
            self._sanitize_payload(upstream_payload)

            url = urljoin(self.upstream_base_url + "/", "v1/chat/completions")
            stream = bool(upstream_payload.get("stream"))

            try:
                response = self.session.post(
                    url,
                    headers=self.build_headers(),
                    json=upstream_payload,
                    timeout=self.timeout,
                    stream=stream,
                )
            except Exception as exc:
                error_msg = f"Connection error: {exc}"
                self.logger.warning(
                    "Attempt %d: %s failed - %s", attempt + 1, model.name, error_msg
                )
                self._record_model_failure(model, error_msg)
                last_error = error_msg
                continue

            if response.ok:
                self.logger.info(
                    "Attempt %d: %s succeeded", attempt + 1, model.name
                )
                self._last_successful_model = model.name
                self._save_last_successful_model()
                self.model_proxy.failure_tracker.record_success(model.name)
                model.update_stats(success=True)
                return self._forward_openai_response(response, stream=stream)

            error_msg = self._extract_error_message(response)
            self.logger.warning(
                "Attempt %d: %s returned HTTP %d - %s",
                attempt + 1, model.name, response.status_code, error_msg,
            )
            self._record_model_failure(model, error_msg)
            last_error = f"HTTP {response.status_code}: {error_msg}"
            # 继续循环，尝试下一个模型

        return HTTPStatus.SERVICE_UNAVAILABLE, {
            "Content-Type": "application/json",
        }, {
            "error": {
                "message": f"All models failed after {len(excluded)} attempts. Last error: {last_error}",
                "type": "service_unavailable",
            }
        }, False

    def _proxy_messages(self, payload: Dict[str, Any]) -> Tuple[int, Dict[str, str], Any, bool]:
        if not self.upstream_base_url:
            return self._upstream_not_configured()

        # 检测对话中的 @model 切换命令
        switch_result = self._check_model_switch_command(payload)
        if switch_result:
            return switch_result

        max_retries = self.config.get("quota.max_retries", 3)
        excluded: set[str] = set()
        last_error = "Unknown error"

        for attempt in range(max_retries):
            model = self._select_model_excluding(payload, excluded)
            if not model:
                self.logger.error("No more available models to try")
                break

            excluded.add(model.name)
            self._clamp_max_tokens(payload)
            stream = bool(payload.get("stream"))

            try:
                if self.upstream_type == "anthropic":
                    upstream_payload = dict(payload)
                    upstream_payload["model"] = model.name
                    self._sanitize_payload(upstream_payload)
                    url = urljoin(self.upstream_base_url + "/", "v1/messages")
                    response = self.session.post(
                        url,
                        headers=self.build_headers(),
                        json=upstream_payload,
                        timeout=self.timeout,
                        stream=stream,
                    )
                    if response.ok:
                        self.logger.info(
                            "Attempt %d: %s succeeded", attempt + 1, model.name
                        )
                        self._last_successful_model = model.name
                        self._save_last_successful_model()
                        self.model_proxy.failure_tracker.record_success(model.name)
                        model.update_stats(success=True)
                        if stream:
                            return self._forward_raw_stream(response)
                        return self._forward_json(response, anthropic=True)

                else:
                    openai_payload = self._anthropic_to_openai_payload(payload, model.name)
                    self._sanitize_payload(openai_payload)
                    url = urljoin(self.upstream_base_url + "/", "v1/chat/completions")
                    response = self.session.post(
                        url,
                        headers=self.build_headers(),
                        json=openai_payload,
                        timeout=self.timeout,
                        stream=stream,
                    )
                    if response.ok:
                        self.logger.info(
                            "Attempt %d: %s succeeded", attempt + 1, model.name
                        )
                        self._last_successful_model = model.name
                        self._save_last_successful_model()
                        self.model_proxy.failure_tracker.record_success(model.name)
                        model.update_stats(success=True)
                        if stream:
                            return self._transform_openai_stream_to_anthropic(response)
                        return self._transform_openai_to_anthropic(response)

            except Exception as exc:
                error_msg = f"Connection error: {exc}"
                self.logger.warning(
                    "Attempt %d: %s failed - %s", attempt + 1, model.name, error_msg
                )
                self._record_model_failure(model, error_msg)
                last_error = error_msg
                continue

            error_msg = self._extract_error_message(response)
            self.logger.warning(
                "Attempt %d: %s returned HTTP %d - %s",
                attempt + 1, model.name, response.status_code, error_msg,
            )
            self._record_model_failure(model, error_msg)
            last_error = f"HTTP {response.status_code}: {error_msg}"
            # 继续循环，尝试下一个模型

        return HTTPStatus.SERVICE_UNAVAILABLE, {
            "Content-Type": "application/json",
        }, {
            "type": "error",
            "error": {
                "type": "api_error",
                "message": f"All models failed after {len(excluded)} attempts. Last error: {last_error}",
            }
        }, False

    def _is_in_cooldown(self, model: Model) -> bool:
        """检查模型是否在指数退避冷却期。"""
        if model.last_failure_time is None:
            return False
        base_cooldown = 60
        multiplier = 2 ** min(model.consecutive_failures, 5)
        cooldown = base_cooldown * multiplier
        elapsed = time.time() - model.last_failure_time
        return elapsed < cooldown

    def _anthropic_to_openai_payload(self, payload: Dict[str, Any], model_name: str) -> Dict[str, Any]:
        messages = []
        system_prompt = payload.get("system")
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        for message in payload.get("messages", []):
            content = message.get("content", "")
            if isinstance(content, list):
                text_parts = [
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                content = "".join(text_parts)
            messages.append({
                "role": message.get("role", "user"),
                "content": content,
            })

        return {
            "model": model_name,
            "messages": messages,
            "temperature": payload.get("temperature"),
            "max_tokens": payload.get("max_tokens"),
            "stream": payload.get("stream", False),
        }

    def _forward_openai_response(
        self,
        response: requests.Response,
        stream: bool = False
    ) -> Tuple[int, Dict[str, str], Any, bool]:
        if stream:
            return self._forward_raw_stream(response)
        return self._forward_json(response, anthropic=False)

    def _forward_json(
        self,
        response: requests.Response,
        anthropic: bool = False
    ) -> Tuple[int, Dict[str, str], Any, bool]:
        content_type = response.headers.get("content-type", "application/json")
        try:
            payload = response.json()
        except Exception:
            payload = {"error": {"message": response.text}}

        if not response.ok:
            return response.status_code, {"Content-Type": content_type}, payload, False

        if anthropic:
            return response.status_code, {"Content-Type": "application/json"}, payload, False
        return response.status_code, {"Content-Type": "application/json"}, payload, False

    def _transform_openai_to_anthropic(
        self,
        response: requests.Response
    ) -> Tuple[int, Dict[str, str], Any, bool]:
        if not response.ok:
            return self._forward_json(response, anthropic=False)

        payload = response.json()
        choice = (payload.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content", "")
        if isinstance(content, list):
            content = "".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict)
            )

        usage = payload.get("usage") or {}
        anthropic_payload = {
            "id": payload.get("id", f"msg_{int(time.time())}"),
            "type": "message",
            "role": "assistant",
            "model": payload.get("model"),
            "content": [
                {"type": "text", "text": content}
            ],
            "stop_reason": choice.get("finish_reason") or "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            },
        }
        return response.status_code, {"Content-Type": "application/json"}, anthropic_payload, False

    def _transform_openai_stream_to_anthropic(
        self,
        response: requests.Response
    ) -> Tuple[int, Dict[str, str], Any, bool]:
        if not response.ok:
            return self._forward_json(response, anthropic=False)

        # 强制 UTF-8 解码，防止上游 SSE 缺少 charset 时 requests 回退到 ISO-8859-1
        response.encoding = "utf-8"

        def generator() -> Iterable[bytes]:
            message_id = f"msg_{int(time.time())}"
            yield self._sse("message_start", {
                "type": "message_start",
                "message": {
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": None,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                }
            })
            yield self._sse("content_block_start", {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""}
            })

            text_started = False
            for line in response.iter_lines(decode_unicode=True):
                if not line:
                    continue
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except Exception:
                    continue

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                text = delta.get("content")
                if text:
                    text_started = True
                    yield self._sse("content_block_delta", {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": text}
                    })

                finish_reason = choices[0].get("finish_reason")
                if finish_reason:
                    yield self._sse("content_block_stop", {
                        "type": "content_block_stop",
                        "index": 0
                    })
                    yield self._sse("message_delta", {
                        "type": "message_delta",
                        "delta": {
                            "stop_reason": finish_reason,
                            "stop_sequence": None
                        }
                    })
                    yield self._sse("message_stop", {
                        "type": "message_stop"
                    })
                    return

            if not text_started:
                yield self._sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": ""}
                })

            yield self._sse("content_block_stop", {
                "type": "content_block_stop",
                "index": 0
            })
            yield self._sse("message_delta", {
                "type": "message_delta",
                "delta": {
                    "stop_reason": "end_turn",
                    "stop_sequence": None
                }
            })
            yield self._sse("message_stop", {
                "type": "message_stop"
            })

        return HTTPStatus.OK, {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }, generator(), True

    def _forward_raw_stream(
        self,
        response: requests.Response
    ) -> Tuple[int, Dict[str, str], Any, bool]:
        content_type = response.headers.get("content-type", "text/event-stream")

        def generator() -> Iterable[bytes]:
            for chunk in response.iter_content(chunk_size=1024):
                if chunk:
                    yield chunk

        return response.status_code, {
            "Content-Type": content_type,
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }, generator(), True

    def _upstream_not_configured(self) -> Tuple[int, Dict[str, str], Any, bool]:
        return HTTPStatus.SERVICE_UNAVAILABLE, {
            "Content-Type": "application/json",
        }, {
            "error": {
                "message": "Upstream model service is not configured. Set proxy.upstream_base_url.",
                "type": "configuration_error",
            }
        }, False

    def _sse(self, event: str, payload: Dict[str, Any]) -> bytes:
        return (
            f"event: {event}\n"
            f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        ).encode("utf-8")


class ProxyRequestHandler(BaseHTTPRequestHandler):
    """HTTP handler for ms-claude proxy."""

    protocol_version = "HTTP/1.1"

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        body = self.rfile.read(length)
        if not body:
            return {}
        return json.loads(body.decode("utf-8"))

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_stream(self, status: int, headers: Dict[str, str], chunks: Iterable[bytes]) -> None:
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        for chunk in chunks:
            if not chunk:
                continue
            self.wfile.write(chunk)
            self.wfile.flush()

    def do_GET(self) -> None:  # noqa: N802
        service: ProxyService = self.server.proxy_service
        status, headers, payload, is_stream = service.route_request(
            urlparse(self.path).path,
            {}
        )
        if is_stream:
            self._send_stream(status, headers, payload)
        else:
            self._send_json(status, payload)

    def _json_to_sse(self, payload: Dict[str, Any]) -> Iterable[bytes]:
        """将 Anthropic 格式的 JSON 响应转换为 SSE 流格式。"""
        msg_id = payload.get("id", f"msg_{int(time.time())}")
        model_name = payload.get("model", "unknown")

        def _sse(event: str, data: Dict[str, Any]) -> bytes:
            return ("data: " + json.dumps(data) + "\n\n").encode()

        # message_start
        yield _sse("message_start", {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": model_name,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
        })
        # content_block_start
        yield _sse("content_block_start", {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""}
        })
        # content_block_delta (text)
        content = payload.get("content", [])
        text = ""
        if content and isinstance(content, list) and len(content) > 0:
            first = content[0]
            if isinstance(first, dict) and first.get("type") == "text":
                text = first.get("text", "")
        if text:
            chunk_size = 100
            for i in range(0, len(text), chunk_size):
                chunk = text[i:i+chunk_size]
                yield _sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": chunk}
                })
        # content_block_stop
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        # message_delta
        yield _sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": payload.get("stop_reason", "end_turn"), "stop_sequence": None},
            "usage": {"output_tokens": 0}
        })
        # message_stop
        yield _sse("message_stop", {"type": "message_stop"})
        # 结束标记（某些客户端需要）
        yield b"data: [DONE]\n\n"

    def do_POST(self) -> None:  # noqa: N802
        service: ProxyService = self.server.proxy_service
        try:
            payload = self._read_json()
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {
                "error": {
                    "message": f"Invalid JSON body: {exc}",
                    "type": "invalid_request_error",
                }
            })
            return

        status, headers, response_body, is_stream = service.route_request(
            urlparse(self.path).path,
            payload
        )
        # 如果原始请求要求流式，但代理返回非流式（如 @model 命令），
        # 将 JSON 转换为 SSE 流式格式
        if payload.get("stream") and not is_stream and isinstance(response_body, dict):
            sse_headers = {
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            }
            logger.info("Converting JSON to SSE stream for @model response")
            self._send_stream(status, sse_headers, self._json_to_sse(response_body))
        elif is_stream:
            self._send_stream(status, headers, response_body)
        else:
            self._send_json(status, response_body)

    def log_message(self, format: str, *args: Any) -> None:
        logging.getLogger(__name__).info("%s - %s", self.address_string(), format % args)


def run_proxy_server(config: Config, host: Optional[str] = None, port: Optional[int] = None) -> None:
    """Run the HTTP proxy server forever."""
    # 配置根日志记录器，确保所有模块的日志都写入文件
    log_file = config.resolve_path(
        config.get("app.proxy_log_file", "logs/proxy.log"),
        "logs/proxy.log"
    )
    log_file.parent.mkdir(parents=True, exist_ok=True)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    formatter = logging.Formatter(
        config.get("logging.format", "%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    # 避免重复添加文件处理器
    has_file_handler = any(
        isinstance(h, logging.FileHandler) and str(log_file) in str(getattr(h, 'baseFilename', ''))
        for h in root_logger.handlers
    )
    if not has_file_handler:
        file_handler = logging.handlers.TimedRotatingFileHandler(
            str(log_file), when='midnight', interval=1, backupCount=7, encoding='utf-8'
        )
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    config.start_hot_reload()
    service = ProxyService(config)
    server_host = host or config.get("proxy.host", "127.0.0.1")
    server_port = int(port or config.get("proxy.port", 8080))

    httpd = ThreadedProxyHTTPServer((server_host, server_port), ProxyRequestHandler, service)
    logging.getLogger(__name__).info(
        "ms-claude proxy listening on http://%s:%s", server_host, server_port
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
