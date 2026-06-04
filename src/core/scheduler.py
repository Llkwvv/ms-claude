"""
优先级调度器 - 负责模型选择和切换策略
"""

import logging
import time
from typing import List, Optional
from dataclasses import dataclass

from ..models.model import Model, ModelStatus


@dataclass
class ScheduleResult:
    """调度结果"""
    model: Optional[Model]
    reason: str
    priority_index: int


class PriorityScheduler:
    """
    优先级调度器

    根据模型优先级和状态选择最优模型
    """

    def __init__(self, config):
        """初始化调度器"""
        self.config = config
        self.logger = logging.getLogger(__name__)

        # 模型优先级缓存
        self._priority_cache: List[Model] = []
        self._cache_timestamp = 0
        self._cache_ttl = 300  # 5分钟

    def select_model(
        self,
        models: List[Model],
        exclude_models: Optional[List[str]] = None
    ) -> ScheduleResult:
        """
        选择最优模型

        Args:
            models: 模型列表
            exclude_models: 排除的模型名称列表

        Returns:
            调度结果
        """
        if exclude_models is None:
            exclude_models = []

        # 按优先级排序
        sorted_models = self._sort_by_priority(models)

        for index, model in enumerate(sorted_models):
            # 检查是否排除
            if model.name in exclude_models:
                continue

            # 检查状态
            if model.status != ModelStatus.AVAILABLE:
                self.logger.debug(
                    f"Model {model.name} not available: {model.status}"
                )
                continue

            # 检查冷却时间
            if self._is_in_cooldown(model):
                self.logger.debug(
                    f"Model {model.name} in cooldown"
                )
                continue

            return ScheduleResult(
                model=model,
                reason="Available and highest priority",
                priority_index=index
            )

        return ScheduleResult(
            model=None,
            reason="No available model found",
            priority_index=-1
        )

    def _sort_by_priority(self, models: List[Model]) -> List[Model]:
        """
        按优先级排序模型

        Args:
            models: 模型列表

        Returns:
            排序后的模型列表
        """
        return sorted(
            models,
            key=lambda m: (
                m.priority,
                # 同优先级时，按失败次数升序
                m.failure_count,
                # 按响应时间升序
                m.avg_response_time
            )
        )

    def _is_in_cooldown(self, model: Model) -> bool:
        """
        检查模型是否在冷却期

        Args:
            model: 模型实例

        Returns:
            是否在冷却期
        """
        if model.last_failure_time is None:
            return False

        # 计算冷却时间（指数退避）
        base_cooldown = 60  # 基础冷却60秒
        multiplier = 2 ** min(model.failure_count, 5)  # 最大32倍
        cooldown = base_cooldown * multiplier

        elapsed = time.time() - model.last_failure_time
        return elapsed < cooldown

    def mark_success(self, model: Model):
        """
        标记请求成功

        Args:
            model: 模型实例
        """
        model.failure_count = max(0, model.failure_count - 1)
        model.consecutive_failures = 0

    def mark_failure(self, model: Model, error_type: str = "unknown"):
        """
        标记请求失败

        Args:
            model: 模型实例
            error_type: 错误类型
        """
        model.failure_count += 1
        model.consecutive_failures += 1
        model.last_failure_time = time.time()

        # 更新状态
        if model.consecutive_failures >= 5:
            model.status = ModelStatus.TEMPORARILY_DISABLED
            self.logger.warning(
                f"Model {model.name} disabled after "
                f"{model.consecutive_failures} consecutive failures"
            )

    def get_next_priority_index(
        self,
        models: List[Model],
        current_index: int
    ) -> int:
        """
        获取下一个优先级索引

        Args:
            models: 模型列表
            current_index: 当前索引

        Returns:
            下一个优先级索引
        """
        sorted_models = self._sort_by_priority(models)
        next_index = (current_index + 1) % len(sorted_models)
        return next_index

    def get_fallback_model(
        self,
        models: List[Model],
        exclude_models: Optional[List[str]] = None
    ) -> Optional[Model]:
        """
        获取备用模型

        Args:
            models: 模型列表
            exclude_models: 排除的模型

        Returns:
            备用模型
        """
        if exclude_models is None:
            exclude_models = []

        sorted_models = self._sort_by_priority(models)

        # 寻找第一个可用的备用模型
        for model in sorted_models:
            if model.name not in exclude_models:
                if model.status == ModelStatus.AVAILABLE:
                    return model
                # 即使状态不是AVAILABLE，如果是配额问题也可以作为最后选择
                if model.status == ModelStatus.QUOTA_EXHAUSTED:
                    self.logger.debug(
                        f"Using {model.name} as last resort (quota exhausted)"
                    )
                    return model

        return None

    def reset_cooldowns(self):
        """重置所有模型的冷却时间"""
        self.logger.info("Resetting all cooldowns")
        # 实际实现中需要访问模型列表
        pass

    def should_update_model_list(self, last_update: float) -> bool:
        """
        检查是否需要更新模型列表

        Args:
            last_update: 上次更新时间戳

        Returns:
            是否需要更新
        """
        update_interval = self.config.get(
            "modelscope.cache_ttl", 86400
        )
        elapsed = time.time() - last_update
        return elapsed >= update_interval

    def get_health_report(self, models: List[Model]) -> dict:
        """
        获取健康报告

        Args:
            models: 模型列表

        Returns:
            健康报告字典
        """
        report = {
            "total": len(models),
            "available": 0,
            "unavailable": 0,
            "quota_exhausted": 0,
            "disabled": 0,
            "models": {}
        }

        for model in models:
            status = model.status.value
            report["models"][model.name] = {
                "status": status,
                "priority": model.priority,
                "failure_count": model.failure_count,
                "consecutive_failures": model.consecutive_failures,
                "avg_response_time": model.avg_response_time
            }

            if model.status == ModelStatus.AVAILABLE:
                report["available"] += 1
            elif model.status == ModelStatus.QUOTA_EXHAUSTED:
                report["quota_exhausted"] += 1
            elif model.status == ModelStatus.DISABLED:
                report["disabled"] += 1
            else:
                report["unavailable"] += 1

        return report
