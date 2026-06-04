"""
配置管理模块
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml


class Config:
    """
    配置管理器

    负责加载和管理配置文件
    """

    def __init__(
        self,
        config_path: str = "src/config/config.yaml",
        env_path: Optional[str] = None,
        home_path: Optional[str] = None
    ):
        """
        初始化配置管理器

        Args:
            config_path: 配置文件路径
            env_path: 环境变量文件路径
        """
        self.logger = logging.getLogger(__name__)

        # 应用根目录（用于隔离 data/log/config）
        self.home_path = Path(
            home_path
            or os.environ.get("MS_CLAUDE_HOME")
            or Path.cwd()
        ).expanduser().resolve()

        # 配置文件路径
        self.config_path = Path(config_path)
        self.env_path = Path(env_path) if env_path else Path(".env")

        # 配置数据
        self._config: Dict[str, Any] = {}

        # 加载配置
        self._load_config()
        self._load_env()
        self._apply_defaults()

    def _load_config(self):
        """加载YAML配置文件"""
        try:
            if self.config_path.exists():
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    self._config = yaml.safe_load(f) or {}
                self.logger.info(f"Loaded config from {self.config_path}")
            else:
                self.logger.warning(f"Config file not found: {self.config_path}")
                self._config = {}
        except Exception as e:
            self.logger.error(f"Error loading config: {e}")
            self._config = {}

    def _load_env(self):
        """加载环境变量"""
        try:
            # 从.env文件加载
            if self.env_path.exists():
                with open(self.env_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#'):
                            if '=' in line:
                                key, value = line.split('=', 1)
                                os.environ[key.strip()] = value.strip()
                self.logger.info(f"Loaded env from {self.env_path}")

            # 从系统环境变量加载
            log_level = os.environ.get('LOG_LEVEL')
            if log_level:
                self._config.setdefault("logging", {})["level"] = log_level

            config_path = os.environ.get('CONFIG_PATH')
            if config_path:
                self.config_path = Path(config_path)
                self._load_config()

            home_path = os.environ.get("MS_CLAUDE_HOME")
            if home_path:
                self.home_path = Path(home_path).expanduser().resolve()

            app_config_path = os.environ.get("MS_CLAUDE_CONFIG")
            if app_config_path:
                self.config_path = Path(app_config_path)
                self._load_config()

            data_dir = os.environ.get("MS_CLAUDE_DATA_DIR")
            if data_dir:
                self.set("app.data_dir", data_dir)

            log_dir = os.environ.get("MS_CLAUDE_LOG_DIR")
            if log_dir:
                self.set("app.log_dir", log_dir)

        except Exception as e:
            self.logger.error(f"Error loading env: {e}")

    def _apply_defaults(self):
        """应用默认配置并补齐缺失字段"""
        defaults = self._get_default_config()
        self._deep_update(defaults, self._config)
        self._config = defaults
        self._config.setdefault("app", {})["home"] = str(self.home_path)

    def _deep_update(self, target: Dict[str, Any], source: Dict[str, Any]):
        """递归合并字典"""
        for key, value in source.items():
            if (
                key in target
                and isinstance(target[key], dict)
                and isinstance(value, dict)
            ):
                self._deep_update(target[key], value)
            else:
                target[key] = value

    def _get_default_config(self) -> Dict[str, Any]:
        """
        获取默认配置

        Returns:
            默认配置字典
        """
        return {
            "app": {
                "home": str(self.home_path),
                "data_dir": "data",
                "log_dir": "logs",
                "model_cache_file": "data/models_cache.json",
                "failure_log_file": "logs/failures.jsonl",
                "proxy_log_file": "logs/proxy.log",
            },
            "model_priority": [
                "qwen-max",
                "qwen-plus",
                "deepseek-coder-v2",
                "yi-large",
                "qwen-turbo"
            ],
            "modelscope": {
                "api_base": "https://modelscope.cn/api/v1",
                "update_schedule": "0 2 * * *",
                "cache_ttl": 86400
            },
            "quota": {
                "error_codes": [
                    "quota_exceeded",
                    "rate_limit_exceeded",
                    "insufficient_quota"
                ],
                "max_retries": 3,
                "retry_delay": 1
            },
            "failure_tracking": {
                "enabled": True,
                "log_file": "logs/failures.jsonl",
                "failure_threshold": 5,
                "time_window": 3600
            },
            "streaming": {
                "enabled": True,
                "timeout": 300,
                "buffer_size": 1024
            },
            "logging": {
                "level": "INFO",
                "log_file": "logs/proxy.log",
                "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            },
            "proxy": {
                "timeout": 60,
                "connect_timeout": 10,
                "auto_retry": True,
                "host": "127.0.0.1",
                "port": 8080,
                "upstream_base_url": "https://api-inference.modelscope.cn",
                "upstream_api_key_env": "MS_CLAUDE_UPSTREAM_API_KEY",
                "upstream_api_key": "",
                "upstream_type": "openai"
            }
        }

    def resolve_path(self, value: Optional[str], default: str) -> Path:
        """
        解析路径，默认相对于应用 home 目录。

        Args:
            value: 配置值
            default: 默认相对路径

        Returns:
            解析后的绝对路径
        """
        raw_path = value or default
        path = Path(raw_path).expanduser()
        if path.is_absolute():
            return path
        return (self.home_path / path).resolve()

    def get(self, key: str, default: Any = None) -> Any:
        """
        获取配置值

        Args:
            key: 配置键（支持点分隔，如 'modelscope.api_base'）
            default: 默认值

        Returns:
            配置值
        """
        keys = key.split('.')
        value = self._config

        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default

        return value

    def set(self, key: str, value: Any):
        """
        设置配置值

        Args:
            key: 配置键
            value: 配置值
        """
        keys = key.split('.')
        config = self._config

        for k in keys[:-1]:
            if k not in config:
                config[k] = {}
            config = config[k]

        config[keys[-1]] = value

    def update(self, updates: Dict[str, Any]):
        """
        更新配置

        Args:
            updates: 更新的配置字典
        """
        def deep_update(d, u):
            for k, v in u.items():
                if isinstance(v, dict) and k in d and isinstance(d[k], dict):
                    deep_update(d[k], v)
                else:
                    d[k] = v

        deep_update(self._config, updates)

    def save(self, path: Optional[str] = None):
        """
        保存配置到文件

        Args:
            path: 保存路径（可选）
        """
        save_path = Path(path) if path else self.config_path

        try:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            with open(save_path, 'w', encoding='utf-8') as f:
                yaml.dump(self._config, f, allow_unicode=True, default_flow_style=False)
            self.logger.info(f"Config saved to {save_path}")
        except Exception as e:
            self.logger.error(f"Error saving config: {e}")

    def to_dict(self) -> Dict[str, Any]:
        """
        转换为字典

        Returns:
            配置字典
        """
        return self._config.copy()

    def __getitem__(self, key: str) -> Any:
        """支持字典式访问"""
        return self.get(key)

    def __setitem__(self, key: str, value: Any):
        """支持字典式设置"""
        self.set(key, value)

    def __contains__(self, key: str) -> bool:
        """检查配置键是否存在"""
        try:
            value = self.get(key)
            return value is not None
        except Exception:
            return False

    def __str__(self) -> str:
        """字符串表示"""
        return f"Config(path={self.config_path})"

    def __repr__(self) -> str:
        """详细字符串表示"""
        return f"Config(path={self.config_path}, keys={list(self._config.keys())})"
