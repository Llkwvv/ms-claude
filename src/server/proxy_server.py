"""
HTTP proxy server for Claude-compatible clients.
"""

from __future__ import annotations

import json
import logging
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

        self.upstream_base_url = (
            self.config.get("proxy.upstream_base_url", "") or ""
        ).rstrip("/")
        self.upstream_type = (
            self.config.get("proxy.upstream_type", "openai") or "openai"
        ).lower()
        self.timeout = self.config.get("proxy.timeout", 60)
        self.api_key = self._resolve_api_key()

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
        """选择模型，优先上次成功的，其次 payload 指定的，最后按优先级。"""
        # 1. 优先使用上次成功的模型
        if self._last_successful_model and self._last_successful_model not in excluded:
            model = self.model_proxy.model_manager.get_model(self._last_successful_model)
            if model and model.status == ModelStatus.AVAILABLE:
                if not self.model_proxy.failure_tracker.is_over_threshold(model.name):
                    return model

        # 2. 其次使用 payload 指定的模型
        model_name = payload.get("model")
        if model_name and model_name not in excluded:
            model = self.model_proxy.model_manager.get_model(model_name)
            if model and model.status == ModelStatus.AVAILABLE:
                if not self.model_proxy.failure_tracker.is_over_threshold(model.name):
                    return model

        # 3. 最后按优先级列表选择
        for model in self.model_proxy.model_manager.get_models_by_priority():
            if model.name in excluded:
                continue
            if model.status != ModelStatus.AVAILABLE:
                continue
            if self.model_proxy.failure_tracker.is_over_threshold(model.name):
                continue
            return model
        return None

    def _record_model_failure(self, model: Model, error_msg: str) -> None:
        """记录失败。"""
        self.logger.warning("Recording failure for %s: %s", model.name, error_msg)
        self.model_proxy.failure_tracker.record_failure(
            model.name, error_msg, {}
        )

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

            # 限制 max_tokens 在 ModelScope API 允许范围内
            max_tokens = upstream_payload.get("max_tokens")
            if isinstance(max_tokens, int):
                if max_tokens > 16384:
                    upstream_payload["max_tokens"] = 16384
                    self.logger.debug("Capped max_tokens from %d to 16384", max_tokens)
                elif max_tokens < 1:
                    upstream_payload["max_tokens"] = 1
            elif max_tokens is not None:
                upstream_payload["max_tokens"] = 8192

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

        max_retries = self.config.get("quota.max_retries", 3)
        excluded: set[str] = set()
        last_error = "Unknown error"

        for attempt in range(max_retries):
            model = self._select_model_excluding(payload, excluded)
            if not model:
                self.logger.error("No more available models to try")
                break

            excluded.add(model.name)

            # 限制 max_tokens 在 ModelScope API 允许范围内
            max_tokens = payload.get("max_tokens")
            if isinstance(max_tokens, int):
                if max_tokens > 16384:
                    payload["max_tokens"] = 16384
                    self.logger.debug("Capped max_tokens from %d to 16384", max_tokens)
                elif max_tokens < 1:
                    payload["max_tokens"] = 1
            elif max_tokens is not None:
                payload["max_tokens"] = 8192

            stream = bool(payload.get("stream"))

            try:
                if self.upstream_type == "anthropic":
                    upstream_payload = dict(payload)
                    upstream_payload["model"] = model.name
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
                        if stream:
                            return self._forward_raw_stream(response)
                        return self._forward_json(response, anthropic=True)

                else:
                    openai_payload = self._anthropic_to_openai_payload(payload, model.name)
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
            "error": {
                "message": f"All models failed after {len(excluded)} attempts. Last error: {last_error}",
                "type": "service_unavailable",
            }
        }, False

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
        if is_stream:
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
        file_handler = logging.FileHandler(str(log_file), encoding='utf-8')
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

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
