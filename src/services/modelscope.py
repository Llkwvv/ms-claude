"""
ModelScope / 上游 API 服务

负责与 ModelScope 或上游兼容 API 交互，获取模型列表等信息
"""

import os
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional


try:
    import requests
except ImportError:
    requests = None


class ModelScopeAPI:
    """
    ModelScope API客户端
    """

    def __init__(self, config: Dict[str, Any]):
        """
        初始化API客户端

        Args:
            config: 配置字典
        """
        self.config = config
        self.logger = logging.getLogger(__name__)

        self.api_base = config.get(
            "modelscope.api_base",
            "https://modelscope.cn/api/v1"
        )
        self.upstream_base_url = config.get("proxy.upstream_base_url", "") or ""
        self.upstream_type = config.get("proxy.upstream_type", "openai") or "openai"
        self.upstream_api_key = config.get("proxy.upstream_api_key", "") or ""
        self.upstream_api_key_env = config.get("proxy.upstream_api_key_env", "") or ""
        self.cache_ttl = config.get("modelscope.cache_ttl", 86400)

        # 会话（可选）
        self._session = None

    def __del__(self):
        """清理资源"""
        if self._session:
            try:
                self._session.close()
            except Exception:
                pass

    @property
    def session(self):
        """获取requests会话"""
        if self._session is None:
            if requests:
                self._session = requests.Session()
                # 设置默认超时
                self._session.timeout = 30
            else:
                raise ImportError(
                    "requests library is required for ModelScope API"
                )
        return self._session

    def _resolve_api_key(self) -> str:
        """解析上游 API Token。"""
        if self.upstream_api_key:
            return self.upstream_api_key

        if self.upstream_api_key_env:
            value = os.environ.get(self.upstream_api_key_env, "")
            if value:
                return value

        value = os.environ.get("MS_CLAUDE_UPSTREAM_API_KEY", "")
        if value:
            return value

        # 兼容旧项目的做法：如果环境里已经有 Anthropic Token，则优先使用
        token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
        if token and token != "PROXY_MANAGED":
            return token

        return ""

    def _build_headers(self) -> Dict[str, str]:
        """构建上游请求头。"""
        headers = {"Content-Type": "application/json"}
        token = self._resolve_api_key()
        if token:
            if self.upstream_type == "anthropic":
                headers["x-api-key"] = token
                headers["anthropic-version"] = "2023-06-01"
            else:
                headers["Authorization"] = f"Bearer {token}"
        return headers

    def get_upstream_models(self) -> Dict[str, Any]:
        """
        获取上游服务的模型列表

        Returns:
            兼容 OpenAI / Anthropic / ModelScope 风格的响应
        """
        if not self.upstream_base_url:
            return {"data": [], "error": "proxy.upstream_base_url is not configured"}

        try:
            response = self.session.get(
                f"{self.upstream_base_url.rstrip('/')}/v1/models",
                headers=self._build_headers(),
                timeout=30
            )

            if response.status_code == 200:
                data = response.json()
                models = data.get("data") or data.get("items") or []
                return {"data": models}

            self.logger.error(
                f"Upstream model API error: {response.status_code} - {response.text}"
            )
            return {"error": response.text, "data": []}

        except Exception as e:
            self.logger.error(f"Error fetching upstream models: {e}")
            return {"error": str(e), "data": []}

    def get_models(
        self,
        framework: str = "all",
        search: str = "",
        page: int = 1,
        page_size: int = 50
    ) -> Dict[str, Any]:
        """
        获取模型列表

        Args:
            framework: 框架类型
            search: 搜索关键词
            page: 页码
            page_size: 每页数量

        Returns:
            API响应数据
        """
        try:
            response = self.session.get(
                f"{self.api_base}/models",
                params={
                    "framework": framework,
                    "search": search,
                    "page": page,
                    "page_size": page_size
                },
                timeout=30
            )

            if response.status_code == 200:
                return response.json()
            else:
                self.logger.error(
                    f"ModelScope API error: {response.status_code} - "
                    f"{response.text}"
                )
                return {"error": response.text, "items": []}

        except Exception as e:
            self.logger.error(f"Error fetching from ModelScope: {e}")
            return {"error": str(e), "items": []}

    def get_model(self, model_id: str) -> Dict[str, Any]:
        """
        获取单个模型信息

        Args:
            model_id: 模型ID

        Returns:
            API响应数据
        """
        try:
            response = self.session.get(
                f"{self.api_base}/models/{model_id}",
                timeout=30
            )

            if response.status_code == 200:
                return response.json()
            else:
                self.logger.error(
                    f"ModelScope API error: {response.status_code}"
                )
                return {"error": response.text}

        except Exception as e:
            self.logger.error(f"Error fetching model {model_id}: {e}")
            return {"error": str(e)}

    def get_model_files(self, model_id: str) -> Dict[str, Any]:
        """
        获取模型文件列表

        Args:
            model_id: 模型ID

        Returns:
            API响应数据
        """
        try:
            response = self.session.get(
                f"{self.api_base}/models/{model_id}/files",
                timeout=30
            )

            if response.status_code == 200:
                return response.json()
            else:
                return {"error": response.text, "files": []}

        except Exception as e:
            self.logger.error(f"Error fetching files for {model_id}: {e}")
            return {"error": str(e), "files": []}

    def search_models(
        self,
        query: str,
        framework: str = "all",
        page: int = 1,
        page_size: int = 20
    ) -> Dict[str, Any]:
        """
        搜索模型

        Args:
            query: 搜索查询
            framework: 框架类型
            page: 页码
            page_size: 每页数量

        Returns:
            API响应数据
        """
        return self.get_models(
            framework=framework,
            search=query,
            page=page,
            page_size=page_size
        )

    def get_trending_models(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        获取热门模型

        Args:
            limit: 返回数量限制

        Returns:
            热门模型列表
        """
        try:
            response = self.session.get(
                f"{self.api_base}/models",
                params={
                    "search": "",
                    "order": "most_downloaded",
                    "page": 1,
                    "page_size": limit
                },
                timeout=30
            )

            if response.status_code == 200:
                data = response.json()
                return data.get("items", [])
            else:
                return []

        except Exception as e:
            self.logger.error(f"Error fetching trending models: {e}")
            return []

    def get_model_stats(self, model_id: str) -> Dict[str, Any]:
        """
        获取模型统计信息

        Args:
            model_id: 模型ID

        Returns:
            统计信息
        """
        try:
            response = self.session.get(
                f"{self.api_base}/models/{model_id}/stats",
                timeout=30
            )

            if response.status_code == 200:
                return response.json()
            else:
                return {}

        except Exception as e:
            self.logger.error(f"Error fetching stats for {model_id}: {e}")
            return {}


class ModelScopeService:
    """
    ModelScope服务层

    提供更高级的模型管理功能
    """

    def __init__(self, config: Dict[str, Any]):
        """
        初始化服务

        Args:
            config: 配置字典
        """
        self.config = config
        self.logger = logging.getLogger(__name__)
        self.api = ModelScopeAPI(config)

        # 本地缓存
        self._cache: Dict[str, Any] = {}
        self._cache_time: Dict[str, float] = {}

    def fetch_latest_models(self) -> List[Dict[str, Any]]:
        """
        获取最新的模型列表

        Returns:
            模型列表
        """
        self.logger.info("Fetching latest models from ModelScope...")

        # 获取不同类型的模型
        all_models = []

        # 代码相关模型
        code_models = self.api.search_models(
            query="code",
            framework="all",
            page_size=20
        )
        all_models.extend(code_models.get("items", []))

        # 通用大模型
        general_models = self.api.get_models(
            framework="all",
            page_size=30
        )
        all_models.extend(general_models.get("items", []))

        # 去重
        seen = set()
        unique_models = []
        for model in all_models:
            model_id = model.get("id")
            if model_id and model_id not in seen:
                seen.add(model_id)
                unique_models.append(model)

        self.logger.info(f"Fetched {len(unique_models)} unique models")
        return unique_models

    def parse_model_info(self, model_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        解析模型信息

        Args:
            model_data: 原始模型数据

        Returns:
            解析后的模型信息
        """
        return {
            "name": model_data.get("id", ""),
            "display_name": model_data.get("name", ""),
            "provider": "modelscope",
            "description": model_data.get("description", ""),
            "tags": model_data.get("tags", []),
            "downloads": model_data.get("downloads", 0),
            "likes": model_data.get("likes", 0),
            "framework": model_data.get("framework", ""),
            "task": model_data.get("task", ""),
            "metadata": model_data
        }

    def filter_code_models(
        self,
        models: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        过滤出代码相关模型

        Args:
            models: 模型列表

        Returns:
            代码模型列表
        """
        code_keywords = [
            "code", "coder", "programming", "dev",
            "instruct", "chat", "llm"
        ]

        filtered = []
        for model in models:
            name = model.get("name", "").lower()
            desc = model.get("description", "").lower()
            tags = [t.lower() for t in model.get("tags", [])]

            if any(keyword in name or keyword in desc
                   for keyword in code_keywords):
                filtered.append(model)
            elif any("code" in tag for tag in tags):
                filtered.append(model)

        return filtered

    def get_model_priority(
        self,
        model_info: Dict[str, Any]
    ) -> int:
        """
        计算模型优先级

        Args:
            model_info: 模型信息

        Returns:
            优先级（数字越小优先级越高）
        """
        priority = 100

        name = model_info.get("name", "").lower()
        downloads = model_info.get("downloads", 0)
        likes = model_info.get("likes", 0)
        tags = [t.lower() for t in model_info.get("tags", [])]

        # 代码模型优先
        code_keywords = ["code", "coder", "programming"]
        if any(kw in name for kw in code_keywords):
            priority -= 30

        # 知名模型优先
        known_models = ["qwen", "deepseek", "yi"]
        if any(m in name for m in known_models):
            priority -= 20

        # 下载量越高优先级越高
        if downloads > 100000:
            priority -= 15
        elif downloads > 10000:
            priority -= 10
        elif downloads > 1000:
            priority -= 5

        # 受欢迎程度
        if likes > 1000:
            priority -= 5

        return max(1, priority)  # 最小优先级为1

    def should_update_cache(self, cache_key: str) -> bool:
        """
        检查是否需要更新缓存

        Args:
            cache_key: 缓存键

        Returns:
            是否需要更新
        """
        last_update = self._cache_time.get(cache_key, 0)
        elapsed = time.time() - last_update
        return elapsed >= self._cache_ttl

    def get_cached_models(self, cache_key: str) -> Optional[List[Dict[str, Any]]]:
        """
        获取缓存的模型列表

        Args:
            cache_key: 缓存键

        Returns:
            缓存的模型列表，如果不存在则返回None
        """
        if not self.should_update_cache(cache_key):
            return self._cache.get(cache_key)
        return None

    def set_cached_models(
        self,
        cache_key: str,
        models: List[Dict[str, Any]]
    ):
        """
        缓存模型列表

        Args:
            cache_key: 缓存键
            models: 模型列表
        """
        self._cache[cache_key] = models
        self._cache_time[cache_key] = time.time()

    def get_health_check(self) -> Dict[str, Any]:
        """
        健康检查

        Returns:
            健康检查结果
        """
        try:
            # 尝试获取模型列表
            result = self.api.get_models(page_size=1)
            success = "error" not in result

            return {
                "status": "healthy" if success else "unhealthy",
                "api_available": success,
                "timestamp": datetime.now().isoformat(),
                "cache_ttl": self._cache_ttl
            }

        except Exception as e:
            return {
                "status": "unhealthy",
                "error": str(e),
                "timestamp": datetime.now().isoformat()
            }
