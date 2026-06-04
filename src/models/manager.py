"""
模型列表管理 - 负责模型列表的加载、更新和维护
"""

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from threading import Lock, Thread

from ..models.model import Model, ModelStatus
from ..utils.config import Config


class ModelManager:
    """
    模型列表管理器

    负责：
    1. 从配置文件加载初始模型列表
    2. 从ModelScope API获取最新模型列表
    3. 模型列表缓存和更新
    4. 按优先级过滤模型
    """

    def __init__(self, config: Config):
        """初始化模型管理器"""
        self.config = config
        self.logger = logging.getLogger(__name__)

        # 线程锁
        self._lock = Lock()

        # 模型缓存
        self._models: List[Model] = []
        self._models_by_name: Dict[str, Model] = {}
        self._last_update: float = 0
        self._fetch_script_path = self._resolve_fetch_script_path()
        self._update_thread: Optional[Thread] = None

        # 优先级配置
        self._priority_config = config.get("model_priority", [])

        # 缓存配置
        self._cache_ttl = config.get("modelscope.cache_ttl", 86400)

        # 数据目录
        self._data_dir = config.resolve_path(
            config.get("app.data_dir", "data"),
            "data"
        )
        self._data_dir.mkdir(parents=True, exist_ok=True)

        self._cache_file = config.resolve_path(
            config.get(
                "app.model_cache_file",
                str(self._data_dir / "models_cache.json")
            ),
            str(self._data_dir / "models_cache.json")
        )

        # 初始化模型列表
        self._initialize_models()

    def _resolve_fetch_script_path(self) -> Optional[Path]:
        """解析模型抓取脚本路径。"""
        explicit_path = (
            self.config.get("modelscope.fetch_script_path")
            or os.environ.get("MS_CLAUDE_MODEL_FETCHER")
        )
        if explicit_path:
            script_path = Path(explicit_path).expanduser()
            return script_path if script_path.exists() else None

        repo_script = Path(__file__).resolve().parents[3] / "fetch_text_generation_models.py"
        return repo_script if repo_script.exists() else None

    @staticmethod
    def _strip_proxy_env(env: Dict[str, str]) -> Dict[str, str]:
        """移除全局代理变量，避免依赖宿主机的 cc-switch/HTTP 代理。"""
        cleaned = dict(env)
        proxy_keys = [
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "NO_PROXY",
            "no_proxy",
        ]
        for key in proxy_keys:
            cleaned.pop(key, None)
        return cleaned

    def _normalize_display_name(self, model_name: str) -> str:
        """为纯字符串模型名生成可读名称。"""
        leaf_name = model_name.split("/")[-1].replace("_", " ").replace("-", " ")
        return " ".join(part.capitalize() for part in leaf_name.split())

    def _build_model_from_name(self, model_name: str, priority: int, item: Optional[Dict[str, Any]] = None) -> Model:
        """从脚本输出构造模型对象。"""
        metadata = dict(item or {})
        display_name = (
            metadata.get("display_name")
            or metadata.get("name")
            or self._normalize_display_name(model_name)
        )
        provider = metadata.get("provider", "modelscope")
        description = metadata.get("description", "")
        capabilities = metadata.get("capabilities") or ["text-generation"]

        if isinstance(capabilities, str):
            capabilities = [capabilities]

        return Model(
            name=model_name,
            display_name=display_name,
            provider=provider,
            priority=priority,
            capabilities=capabilities,
            description=description,
            metadata=metadata,
        )

    def _apply_priority_overrides(self, models: List[Model]) -> List[Model]:
        """按配置显式置顶模型，并重新编号优先级。"""
        if not models:
            return models

        priority_names = list(self._priority_config or [])
        if not priority_names:
            for index, model in enumerate(models):
                model.priority = index
            return models

        ordered: List[Model] = []
        seen = set()
        model_map = {model.name: model for model in models}

        for model_name in priority_names:
            model = model_map.get(model_name)
            if model and model.name not in seen:
                ordered.append(model)
                seen.add(model.name)

        for model in models:
            if model.name not in seen:
                ordered.append(model)

        for index, model in enumerate(ordered):
            model.priority = index
        return ordered

    def _load_models_from_script(self) -> List[Model]:
        """调用 fetch_text_generation_models.py 获取模型列表。"""
        if not self._fetch_script_path:
            self.logger.warning("Model fetch script not found")
            return []

        env = self._strip_proxy_env(os.environ.copy())
        env.setdefault("PYTHONUNBUFFERED", "1")

        result = subprocess.run(
            [sys.executable, str(self._fetch_script_path)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            self.logger.error(
                "Model fetch script failed with exit code %s: %s",
                result.returncode,
                stderr[-500:] if stderr else "(no stderr)"
            )
            return []

        payload = (result.stdout or "").strip()
        if not payload:
            self.logger.warning("Model fetch script returned empty output")
            return []

        try:
            raw_models = json.loads(payload)
        except json.JSONDecodeError as exc:
            self.logger.error("Failed to parse model fetch output: %s", exc)
            self.logger.debug("Raw fetch output: %s", payload[:2000])
            return []

        if not isinstance(raw_models, list):
            self.logger.error("Model fetch output is not a list")
            return []

        models: List[Model] = []
        seen = set()
        for index, item in enumerate(raw_models):
            if isinstance(item, str):
                model_name = item.strip()
                model_item = None
            elif isinstance(item, dict):
                model_name = (
                    str(item.get("id") or item.get("name") or item.get("model_id") or "").strip()
                )
                model_item = item
            else:
                continue

            if not model_name or model_name in seen:
                continue

            seen.add(model_name)
            models.append(self._build_model_from_name(model_name, index, model_item))

        return models

    def _replace_models(self, models: List[Model]) -> None:
        """用新模型列表替换当前缓存并重建索引。"""
        with self._lock:
            self._models = self._apply_priority_overrides(models)
            self._build_name_index()

    def _schedule_background_update(self) -> None:
        """后台刷新模型列表，避免阻塞代理启动。"""
        if self._update_thread and self._update_thread.is_alive():
            return

        def worker() -> None:
            try:
                self.update_from_modelscope()
            except Exception as exc:
                self.logger.error("Background model refresh failed: %s", exc)

        self._update_thread = Thread(
            target=worker,
            daemon=True,
            name="ms-claude-model-refresh",
        )
        self._update_thread.start()

    def _initialize_models(self):
        """初始化模型列表"""
        self.logger.info("Initializing model list...")

        # 尝试从缓存加载
        cached_models = self._load_from_cache()
        if cached_models:
            self._models = cached_models
            self._build_name_index()
            self.logger.info(f"Loaded {len(self._models)} models from cache")
        else:
            # 从配置文件加载
            self._load_from_config()

        # 设置优先级
        self._replace_models(self._models)

        # 检查是否需要更新
        if self._should_update():
            self.logger.info("Model list needs update, scheduling background refresh...")
            self._schedule_background_update()

    def _load_from_config(self):
        """从配置文件加载模型列表"""
        # 使用用户提供的 ModelScope 实际可用模型
        default_models = [
            Model(name="deepseek-ai/DeepSeek-R1-0528", display_name="DeepSeek R1 0528", provider="modelscope", priority=0, capabilities=["text-generation"]),
            Model(name="Qwen/Qwen3-235B-A22B-Instruct-2507", display_name="Qwen3 235B", provider="modelscope", priority=1, capabilities=["text-generation"]),
            Model(name="Qwen/Qwen3-Coder-480B-A35B-Instruct", display_name="Qwen3 Coder 480B", provider="modelscope", priority=2, capabilities=["text-generation", "code-generation"]),
            Model(name="deepseek-ai/DeepSeek-V4-Pro", display_name="DeepSeek V4 Pro", provider="modelscope", priority=3, capabilities=["text-generation"]),
            Model(name="deepseek-ai/DeepSeek-V4-Flash", display_name="DeepSeek V4 Flash", provider="modelscope", priority=4, capabilities=["text-generation"]),
            Model(name="ZhipuAI/GLM-5", display_name="GLM 5", provider="modelscope", priority=5, capabilities=["text-generation"]),
            Model(name="ZhipuAI/GLM-5.1", display_name="GLM 5.1", provider="modelscope", priority=6, capabilities=["text-generation"]),
            Model(name="Qwen/Qwen3-Next-80B-A3B-Instruct", display_name="Qwen3 Next 80B", provider="modelscope", priority=7, capabilities=["text-generation"]),
            Model(name="Qwen/Qwen3-32B", display_name="Qwen3 32B", provider="modelscope", priority=8, capabilities=["text-generation"]),
            Model(name="deepseek-ai/DeepSeek-R1-Distill-Qwen-32B", display_name="DeepSeek R1 Distill Qwen 32B", provider="modelscope", priority=9, capabilities=["text-generation"]),
            Model(name="Qwen/Qwen3-30B-A3B-Instruct-2507", display_name="Qwen3 30B", provider="modelscope", priority=10, capabilities=["text-generation", "code-generation"]),
            Model(name="deepseek-ai/DeepSeek-V3.2", display_name="DeepSeek V3.2", provider="modelscope", priority=11, capabilities=["text-generation"]),
            Model(name="inclusionAI/Ling-2.6-1T", display_name="Ling 2.6 1T", provider="modelscope", priority=12, capabilities=["text-generation"]),
            Model(name="inclusionAI/Ring-2.6-1T", display_name="Ring 2.6 1T", provider="modelscope", priority=13, capabilities=["text-generation"]),
            Model(name="MiniMax/MiniMax-M2.7", display_name="MiniMax M2.7", provider="modelscope", priority=14, capabilities=["text-generation"]),
            Model(name="MiniMax/MiniMax-M2.5", display_name="MiniMax M2.5", provider="modelscope", priority=15, capabilities=["text-generation"]),
            Model(name="MiniMax/MiniMax-M1-80k", display_name="MiniMax M1 80k", provider="modelscope", priority=16, capabilities=["text-generation"]),
            Model(name="inclusionAI/Ling-2.6-flash", display_name="Ling 2.6 Flash", provider="modelscope", priority=17, capabilities=["text-generation"]),
            Model(name="ZhipuAI/GLM-4.7", display_name="GLM 4.7", provider="modelscope", priority=18, capabilities=["text-generation"]),
            Model(name="ZhipuAI/GLM-4.7-Flash", display_name="GLM 4.7 Flash", provider="modelscope", priority=19, capabilities=["text-generation"]),
            Model(name="Qwen/Qwen3-Coder-30B-A3B-Instruct", display_name="Qwen3 Coder 30B", provider="modelscope", priority=20, capabilities=["text-generation", "code-generation"]),
            Model(name="deepseek-ai/DeepSeek-R1-Distill-Llama-70B", display_name="DeepSeek R1 Llama 70B", provider="modelscope", priority=21, capabilities=["text-generation"]),
            Model(name="Qwen/Qwen3-14B", display_name="Qwen3 14B", provider="modelscope", priority=22, capabilities=["text-generation"]),
            Model(name="deepseek-ai/DeepSeek-R1-Distill-Qwen-14B", display_name="DeepSeek R1 Qwen 14B", provider="modelscope", priority=23, capabilities=["text-generation"]),
            Model(name="Qwen/Qwen3-8B", display_name="Qwen3 8B", provider="modelscope", priority=24, capabilities=["text-generation"]),
            Model(name="Qwen/QwQ-32B", display_name="QwQ 32B", provider="modelscope", priority=25, capabilities=["text-generation"]),
            Model(name="Qwen/QwQ-32B-Preview", display_name="QwQ 32B Preview", provider="modelscope", priority=26, capabilities=["text-generation"]),
            Model(name="XiaomiMiMo/MiMo-V2-Flash", display_name="MiMo V2 Flash", provider="modelscope", priority=27, capabilities=["text-generation"]),
            Model(name="Qwen/Qwen3-4B", display_name="Qwen3 4B", provider="modelscope", priority=28, capabilities=["text-generation"]),
            Model(name="deepseek-ai/DeepSeek-R1-Distill-Qwen-7B", display_name="DeepSeek R1 Qwen 7B", provider="modelscope", priority=29, capabilities=["text-generation"]),
            Model(name="deepseek-ai/DeepSeek-R1-Distill-Llama-8B", display_name="DeepSeek R1 Llama 8B", provider="modelscope", priority=30, capabilities=["text-generation"]),
            Model(name="deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B", display_name="DeepSeek R1 Qwen 1.5B", provider="modelscope", priority=31, capabilities=["text-generation"]),
            Model(name="Qwen/Qwen3-1.7B", display_name="Qwen3 1.7B", provider="modelscope", priority=32, capabilities=["text-generation"]),
            Model(name="Qwen/Qwen3-0.6B", display_name="Qwen3 0.6B", provider="modelscope", priority=33, capabilities=["text-generation"]),
            Model(name="ZhipuAI/GLM-4.6", display_name="GLM 4.6", provider="modelscope", priority=34, capabilities=["text-generation"]),
            Model(name="ZhipuAI/GLM-4.5", display_name="GLM 4.5", provider="modelscope", priority=35, capabilities=["text-generation"]),
            Model(name="MedAIBase/AntAngelMed", display_name="AntAngelMed", provider="modelscope", priority=36, capabilities=["text-generation"]),
            Model(name="LLM-Research/Llama-4-Maverick-17B-128E-Instruct", display_name="Llama 4 Maverick", provider="modelscope", priority=37, capabilities=["text-generation"]),
            Model(name="mistralai/Mistral-Large-Instruct-2407", display_name="Mistral Large", provider="modelscope", priority=38, capabilities=["text-generation"]),
            Model(name="mistralai/Ministral-8B-Instruct-2410", display_name="Ministral 8B", provider="modelscope", priority=39, capabilities=["text-generation"]),
            Model(name="meituan-longcat/LongCat-Flash-Lite", display_name="LongCat Flash Lite", provider="modelscope", priority=40, capabilities=["text-generation"]),
            Model(name="XGenerationLab/XiYanSQL-QwenCoder-32B-2504", display_name="XiYanSQL 32B", provider="modelscope", priority=41, capabilities=["text-generation"]),
            Model(name="XGenerationLab/XiYanSQL-QwenCoder-32B-2412", display_name="XiYanSQL 32B 2412", provider="modelscope", priority=42, capabilities=["text-generation"]),
            Model(name="PaddlePaddle/ERNIE-4.5-21B-A3B-PT", display_name="ERNIE 4.5 21B", provider="modelscope", priority=43, capabilities=["text-generation"]),
            Model(name="PaddlePaddle/ERNIE-4.5-300B-A47B-PT", display_name="ERNIE 4.5 300B", provider="modelscope", priority=44, capabilities=["text-generation"]),
            Model(name="PaddlePaddle/ERNIE-4.5-0.3B-PT", display_name="ERNIE 4.5 0.3B", provider="modelscope", priority=45, capabilities=["text-generation"]),
            Model(name="Qwen/Qwen3-30B-A3B-Thinking-2507", display_name="Qwen3 30B Thinking", provider="modelscope", priority=46, capabilities=["text-generation"]),
            Model(name="Qwen/Qwen3-235B-A22B-Thinking-2507", display_name="Qwen3 235B Thinking", provider="modelscope", priority=47, capabilities=["text-generation"]),
            Model(name="Qwen/Qwen3-Next-80B-A3B-Thinking", display_name="Qwen3 Next 80B Thinking", provider="modelscope", priority=48, capabilities=["text-generation"]),
            Model(name="LLM-Research/c4ai-command-r-plus-08-2024", display_name="Command R+", provider="modelscope", priority=49, capabilities=["text-generation"]),
            Model(name="Qwen/Qwen3-235B-A22B", display_name="Qwen3 235B", provider="modelscope", priority=50, capabilities=["text-generation"]),
            Model(name="Qwen/Qwen3-30B-A3B", display_name="Qwen3 30B Base", provider="modelscope", priority=51, capabilities=["text-generation"]),
        ]

        self._models = default_models
        self._build_name_index()
        self.logger.info(f"Loaded {len(self._models)} default models")

    def _build_name_index(self):
        """构建名称索引"""
        self._models_by_name = {model.name: model for model in self._models}

    def get_all_models(self) -> List[Model]:
        """
        获取所有模型

        Returns:
            模型列表
        """
        with self._lock:
            return self._models.copy()

    def get_model(self, name: str) -> Optional[Model]:
        """
        获取指定模型

        Args:
            name: 模型名称

        Returns:
            模型实例，如果不存在则返回None
        """
        with self._lock:
            return self._models_by_name.get(name)

    def get_models_by_priority(self) -> List[Model]:
        """
        获取按优先级排序的模型列表

        Returns:
            排序后的模型列表
        """
        with self._lock:
            return sorted(self._models, key=lambda m: m.priority)

    def get_available_models(self) -> List[Model]:
        """
        获取可用模型列表

        Returns:
            可用模型列表
        """
        with self._lock:
            return [
                model for model in self._models
                if model.status == ModelStatus.AVAILABLE
            ]

    def get_code_models(self) -> List[Model]:
        """
        获取代码专用模型列表

        Returns:
            代码模型列表
        """
        with self._lock:
            return [
                model for model in self._models
                if model.is_code_model()
                and model.status == ModelStatus.AVAILABLE
            ]

    def add_model(self, model: Model):
        """
        添加新模型

        Args:
            model: 模型实例
        """
        with self._lock:
            if model.name not in self._models_by_name:
                self._models.append(model)
                self._models_by_name[model.name] = model
                self.logger.info(f"Added new model: {model.name}")

    def update_model(self, name: str, **kwargs):
        """
        更新模型属性

        Args:
            name: 模型名称
            **kwargs: 要更新的属性
        """
        with self._lock:
            model = self._models_by_name.get(name)
            if model:
                for key, value in kwargs.items():
                    if hasattr(model, key):
                        setattr(model, key, value)
                self.logger.debug(f"Updated model {name}: {kwargs}")

    def remove_model(self, name: str) -> bool:
        """
        移除模型

        Args:
            name: 模型名称

        Returns:
            是否成功移除
        """
        with self._lock:
            if name in self._models_by_name:
                model = self._models_by_name.pop(name)
                self._models.remove(model)
                self.logger.info(f"Removed model: {name}")
                return True
        return False

    def update_from_modelscope(self) -> bool:
        """
        从 fetch_text_generation_models.py 更新模型列表

        Returns:
            是否更新成功
        """
        self.logger.info("Fetching model list from fetch_text_generation_models.py...")

        try:
            new_models = self._load_models_from_script()
            if not new_models:
                self.logger.warning("Model fetch script returned no models")
                return False

            self._replace_models(new_models)
            self._last_update = time.time()
            self._save_to_cache()
            self.logger.info(
                "Updated model list from fetch_text_generation_models.py: %s models",
                len(self._models),
            )
            return True

        except Exception as e:
            self.logger.error(
                "Error updating model list from fetch_text_generation_models.py: %s",
                e
            )
            return False

    def _should_update(self) -> bool:
        """
        检查是否需要更新模型列表

        Returns:
            是否需要更新
        """
        elapsed = time.time() - self._last_update
        return elapsed >= self._cache_ttl

    def _load_from_cache(self) -> Optional[List[Model]]:
        """
        从缓存文件加载模型列表

        Returns:
            模型列表，如果缓存不存在则返回None
        """
        if not self._cache_file.exists():
            return None

        try:
            with open(self._cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            models = []
            for item in data.get("models", []):
                model = Model.from_dict(item)
                models.append(model)

            self._last_update = data.get("timestamp", 0)
            return models

        except Exception as e:
            self.logger.error(f"Error loading cache: {e}")
            return None

    def _save_to_cache(self):
        """保存模型列表到缓存文件"""
        try:
            data = {
                "timestamp": self._last_update,
                "models": [
                    model.to_dict() for model in self._models
                ]
            }

            with open(self._cache_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            self.logger.debug("Model list saved to cache")

        except Exception as e:
            self.logger.error(f"Error saving cache: {e}")

    def get_statistics(self) -> Dict[str, Any]:
        """
        获取模型统计信息

        Returns:
            统计信息字典
        """
        with self._lock:
            stats = {
                "total": len(self._models),
                "available": len(self.get_available_models()),
                "code_models": len(self.get_code_models()),
                "last_update": datetime.fromtimestamp(
                    self._last_update
                ).isoformat() if self._last_update else None,
                "models": {}
            }

            for model in self._models:
                stats["models"][model.name] = {
                    "priority": model.priority,
                    "status": model.status.value,
                    "failure_count": model.failure_count,
                    "success_rate": (
                        model.successful_requests / model.total_requests
                        if model.total_requests > 0 else 0
                    )
                }

            return stats

    def get_priority_order(self) -> List[str]:
        """
        获取模型优先级顺序

        Returns:
            按优先级排序的模型名称列表
        """
        sorted_models = self.get_models_by_priority()
        return [model.name for model in sorted_models]

    def reset_all_stats(self):
        """重置所有模型的统计信息"""
        with self._lock:
            for model in self._models:
                model.reset_stats()
        self.logger.info("Reset all model statistics")

    def set_model_status(self, name: str, status: ModelStatus) -> bool:
        """
        设置模型状态

        Args:
            name: 模型名称
            status: 新状态

        Returns:
            是否成功
        """
        with self._lock:
            model = self._models_by_name.get(name)
            if model:
                old_status = model.status
                model.status = status
                self.logger.info(
                    f"Model {name} status changed: "
                    f"{old_status.value} -> {status.value}"
                )
                return True
        return False

    def enable_model(self, name: str) -> bool:
        """
        启用模型

        Args:
            name: 模型名称

        Returns:
            是否成功
        """
        return self.set_model_status(name, ModelStatus.AVAILABLE)

    def disable_model(self, name: str) -> bool:
        """
        禁用模型

        Args:
            name: 模型名称

        Returns:
            是否成功
        """
        return self.set_model_status(name, ModelStatus.DISABLED)

    def __len__(self) -> int:
        """模型数量"""
        with self._lock:
            return len(self._models)

    def __contains__(self, name: str) -> bool:
        """检查模型是否存在"""
        with self._lock:
            return name in self._models_by_name

    def __iter__(self):
        """迭代器"""
        with self._lock:
            return iter(self._models.copy())
