"""
主代理类 - 负责协调各个模块，处理请求转发和模型切换
"""

import logging
import time
import uuid
from typing import Any, Dict, Optional, Union
from dataclasses import dataclass, field

from ..models.manager import ModelManager
from ..models.model import Model, ModelStatus
from ..services.failure import FailureTracker
from ..utils.config import Config


@dataclass
class RequestContext:
    """请求上下文"""
    request_id: str
    model_used: Optional[str] = None
    attempt_count: int = 0
    start_time: float = field(default_factory=time.time)
    is_streaming: bool = False
    error: Optional[str] = None


class ModelProxy:
    """
    模型代理主类

    负责：
    1. 按优先级选择可用模型
    2. 处理请求并转发到目标模型
    3. 额度感知和自动切换
    4. 失败记录和重试
    """

    def __init__(
        self,
        config: Optional[Union[Config, str]] = None,
        home_path: Optional[str] = None
    ):
        """初始化代理"""
        if isinstance(config, Config):
            self.config = config
        else:
            config_path = config or "src/config/config.yaml"
            self.config = Config(config_path, home_path=home_path)
        self.logger = logging.getLogger(__name__)

        # 初始化组件
        self.model_manager = ModelManager(self.config)
        self.failure_tracker = FailureTracker(self.config)

        # 当前使用的模型索引
        self._current_priority_index = 0

        # 注册配置变更回调
        if isinstance(config, Config):
            self.config.register_change_callback(self._on_config_changed)

        if self.config.get("proxy.upstream_base_url", ""):
            self.logger.info("Scheduling model list refresh from configured upstream")
            self.model_manager._schedule_background_update()

        self.logger.info("ModelProxy initialized")

    def _on_config_changed(self, old_config: Dict[str, Any], new_config: Dict[str, Any]) -> None:
        """配置变更回调：重新加载子组件配置。"""
        self.logger.info("Config hot-reload: updating ModelProxy settings")
        self.model_manager.reload_config()
        self.failure_tracker.reload_config()

    def get_available_model(self) -> Optional[Model]:
        """
        按优先级获取可用模型

        Returns:
            可用的模型实例，如果没有可用模型则返回None
        """
        models = self.model_manager.get_models_by_priority()

        for model in models:
            if self._is_model_available(model):
                return model

        self.logger.warning("No available model found")
        return None

    def _is_model_available(self, model: Model) -> bool:
        """
        检查模型是否可用

        Args:
            model: 模型实例

        Returns:
            是否可用
        """
        # 检查模型状态
        if model.status != ModelStatus.AVAILABLE:
            self.logger.debug(f"Model {model.name} status: {model.status}")
            return False

        # 检查失败记录
        if self.failure_tracker.is_over_threshold(model.name):
            self.logger.warning(
                f"Model {model.name} exceeded failure threshold, skipping"
            )
            model.status = ModelStatus.TEMPORARILY_DISABLED
            return False

        # 检查额度
        if not self._check_quota(model):
            self.logger.debug(f"Model {model.name} quota insufficient")
            return False

        return True

    def _check_quota(self, model: Model) -> bool:
        """
        检查模型额度

        Args:
            model: 模型实例

        Returns:
            额度是否充足
        """
        # 这里可以实现具体的额度检查逻辑
        # 例如调用模型的额度查询API
        return True  # 默认返回True

    def request(
        self,
        prompt: str,
        stream: bool = False,
        **kwargs: Any
    ) -> Union[Dict[str, Any], str]:
        """
        发送请求到模型

        Args:
            prompt: 输入提示
            stream: 是否使用流式响应
            **kwargs: 其他参数

        Returns:
            模型响应（流式返回生成器，非流式返回字典）

        Raises:
            RuntimeError: 所有模型都不可用时抛出
        """
        context = RequestContext(
            request_id=self._generate_request_id(),
            is_streaming=stream
        )

        max_retries = self.config.get("quota.max_retries", 3)

        for attempt in range(max_retries):
            context.attempt_count = attempt + 1

            model = self.get_available_model()
            if not model:
                self.logger.error("All models are unavailable")
                raise RuntimeError("No available model to process request")

            context.model_used = model.name
            self.logger.info(
                f"Attempt {attempt + 1}: Using model {model.name}"
            )

            try:
                if stream:
                    return self._stream_request(model, prompt, context, **kwargs)
                else:
                    return self._regular_request(model, prompt, context, **kwargs)

            except Exception as e:
                error_msg = str(e)
                context.error = error_msg
                self.logger.warning(
                    f"Model {model.name} failed: {error_msg}"
                )

                # 记录失败
                self.failure_tracker.record_failure(
                    model.name, error_msg, context
                )

                # 检查是否为额度/限频错误
                if self._is_quota_error(error_msg):
                    self.logger.info(
                        f"Quota error detected for {model.name}, marking as unavailable"
                    )
                    model.status = ModelStatus.QUOTA_EXHAUSTED

                # 尝试下一个模型
                continue

        raise RuntimeError(
            f"All {max_retries} attempts failed for request {context.request_id}"
        )

    def _regular_request(
        self,
        model: Model,
        prompt: str,
        context: RequestContext,
        **kwargs: Any
    ) -> Dict[str, Any]:
        """
        非流式请求

        Args:
            model: 模型实例
            prompt: 输入提示
            context: 请求上下文
            **kwargs: 其他参数

        Returns:
            模型响应字典
        """
        # 这里实现具体的API调用逻辑
        # 示例：
        response = model.call_api(prompt, stream=False, **kwargs)

        # 检查响应是否成功
        if self._is_success_response(response):
            # 清除失败记录
            self.failure_tracker.record_success(model.name)
            return response
        else:
            raise RuntimeError(
                f"API call failed: {response.get('error', 'Unknown error')}"
            )

    def _stream_request(
        self,
        model: Model,
        prompt: str,
        context: RequestContext,
        **kwargs: Any
    ):
        """
        流式请求

        Args:
            model: 模型实例
            prompt: 输入提示
            context: 请求上下文
            **kwargs: 其他参数

        Yields:
            流式响应片段
        """
        # 这里实现流式API调用逻辑
        # 示例：
        try:
            for chunk in model.call_api(prompt, stream=True, **kwargs):
                yield chunk

            # 流式完成，清除失败记录
            self.failure_tracker.record_success(model.name)

        except Exception as e:
            raise RuntimeError(f"Stream request failed: {str(e)}")

    def _is_quota_error(self, error_msg: str) -> bool:
        """
        检查错误是否为额度/限频相关

        Args:
            error_msg: 错误消息

        Returns:
            是否为额度错误
        """
        quota_errors = self.config.get("quota.error_codes", [])
        return any(error_code in error_msg.lower()
                   for error_code in quota_errors)

    def _is_success_response(self, response: Dict[str, Any]) -> bool:
        """
        检查响应是否成功

        Args:
            response: API响应

        Returns:
            是否成功
        """
        # 检查响应格式
        return (
            response is not None
            and "error" not in response
            and ("content" in response or "choices" in response)
        )

    def _generate_request_id(self) -> str:
        """生成请求ID"""
        return str(uuid.uuid4())[:8]

    def get_status(self) -> Dict[str, Any]:
        """
        获取代理状态

        Returns:
            状态字典
        """
        models = self.model_manager.get_all_models()
        return {
            "total_models": len(models),
            "available_models": len([
                m for m in models
                if m.status == ModelStatus.AVAILABLE
            ]),
            "model_details": [
                {
                    "name": m.name,
                    "status": m.status.value,
                    "priority": m.priority
                }
                for m in models
            ],
            "failure_stats": self.failure_tracker.get_stats()
        }

    def reset_model_status(self, model_name: str) -> bool:
        """
        重置模型状态

        Args:
            model_name: 模型名称

        Returns:
            是否成功
        """
        model = self.model_manager.get_model(model_name)
        if model:
            model.status = ModelStatus.AVAILABLE
            self.failure_tracker.clear_failures(model_name)
            self.logger.info(f"Reset model {model_name} status")
            return True
        return False
