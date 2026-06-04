"""
模型实体类
"""

from enum import Enum
from typing import Optional, Dict, Any
from dataclasses import dataclass, field
import time


class ModelStatus(Enum):
    """模型状态枚举"""
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    QUOTA_EXHAUSTED = "quota_exhausted"
    TEMPORARILY_DISABLED = "temporarily_disabled"
    DISABLED = "disabled"


@dataclass
class Model:
    """
    模型实体

    Attributes:
        name: 模型名称/标识符
        display_name: 显示名称
        provider: 提供商
        priority: 优先级（数字越小优先级越高）
        status: 当前状态
        capabilities: 支持的能力列表
        description: 描述
        api_endpoint: API端点
        metadata: 其他元数据
    """
    name: str
    display_name: str
    provider: str = "modelscope"
    priority: int = 999
    status: ModelStatus = ModelStatus.AVAILABLE
    capabilities: list = field(default_factory=lambda: ["text-generation"])
    description: str = ""
    api_endpoint: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    # 运行时统计
    failure_count: int = 0
    consecutive_failures: int = 0
    total_requests: int = 0
    successful_requests: int = 0
    last_failure_time: Optional[float] = None
    last_success_time: Optional[float] = None
    avg_response_time: float = 0.0

    def __post_init__(self):
        """初始化后处理"""
        # 确保 capabilities 是列表
        if isinstance(self.capabilities, str):
            self.capabilities = [self.capabilities]

    def to_dict(self) -> Dict[str, Any]:
        """
        转换为字典

        Returns:
            模型字典表示
        """
        return {
            "name": self.name,
            "display_name": self.display_name,
            "provider": self.provider,
            "priority": self.priority,
            "status": self.status.value,
            "capabilities": self.capabilities,
            "description": self.description,
            "api_endpoint": self.api_endpoint,
            "metadata": self.metadata,
            "failure_count": self.failure_count,
            "consecutive_failures": self.consecutive_failures,
            "total_requests": self.total_requests,
            "successful_requests": self.successful_requests,
            "last_failure_time": self.last_failure_time,
            "last_success_time": self.last_success_time,
            "avg_response_time": self.avg_response_time
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Model":
        """
        从字典创建模型实例

        Args:
            data: 模型数据字典

        Returns:
            模型实例
        """
        # 处理状态
        status_value = data.get("status", "available")
        if isinstance(status_value, ModelStatus):
            status = status_value
        else:
            try:
                status = ModelStatus(status_value)
            except ValueError:
                status = ModelStatus.AVAILABLE

        return cls(
            name=data.get("name", ""),
            display_name=data.get("display_name", data.get("name", "")),
            provider=data.get("provider", "modelscope"),
            priority=data.get("priority", 999),
            status=status,
            capabilities=data.get("capabilities", ["text-generation"]),
            description=data.get("description", ""),
            api_endpoint=data.get("api_endpoint"),
            metadata=data.get("metadata", {})
        )

    def update_stats(
        self,
        success: bool,
        response_time: Optional[float] = None
    ):
        """
        更新模型统计信息

        Args:
            success: 请求是否成功
            response_time: 响应时间（秒）
        """
        self.total_requests += 1

        if success:
            self.successful_requests += 1
            self.last_success_time = time.time()
            self.consecutive_failures = 0
        else:
            self.failure_count += 1
            self.consecutive_failures += 1
            self.last_failure_time = time.time()

        # 更新平均响应时间
        if response_time is not None:
            if self.avg_response_time == 0:
                self.avg_response_time = response_time
            else:
                # 指数移动平均
                alpha = 0.1
                self.avg_response_time = (
                    alpha * response_time +
                    (1 - alpha) * self.avg_response_time
                )

    def can_handle_capability(self, capability: str) -> bool:
        """
        检查模型是否支持指定能力

        Args:
            capability: 能力名称

        Returns:
            是否支持
        """
        return capability in self.capabilities

    def is_code_model(self) -> bool:
        """
        检查是否为代码专用模型

        Returns:
            是否为代码模型
        """
        code_keywords = ["code", "coder", "programming", "dev"]
        name_lower = self.name.lower()
        display_lower = self.display_name.lower()

        return any(
            keyword in name_lower or keyword in display_lower
            for keyword in code_keywords
        ) or "code" in self.capabilities

    def __str__(self) -> str:
        """字符串表示"""
        return f"Model({self.name}, priority={self.priority}, status={self.status.value})"

    def __repr__(self) -> str:
        """详细字符串表示"""
        return (
            f"Model(name='{self.name}', display_name='{self.display_name}', "
            f"priority={self.priority}, status={self.status.value}, "
            f"capabilities={self.capabilities})"
        )

    def clone(self) -> "Model":
        """
        克隆模型实例

        Returns:
            新的模型实例
        """
        return Model(
            name=self.name,
            display_name=self.display_name,
            provider=self.provider,
            priority=self.priority,
            status=self.status,
            capabilities=self.capabilities.copy(),
            description=self.description,
            api_endpoint=self.api_endpoint,
            metadata=self.metadata.copy()
        )

    def reset_stats(self):
        """重置统计信息"""
        self.failure_count = 0
        self.consecutive_failures = 0
        self.total_requests = 0
        self.successful_requests = 0
        self.last_failure_time = None
        self.last_success_time = None
        self.avg_response_time = 0.0

    def reset_status(self):
        """重置状态"""
        self.status = ModelStatus.AVAILABLE
        self.consecutive_failures = 0
        self.last_failure_time = None
"""
模型状态转换图：

AVAILABLE ──[失败]──> TEMPORARILY_DISABLED
    │                         │
    │                         │[持续失败]
    │                         ▼
    │                    DISABLED
    │
    │[配额耗尽]
    ▼
QUOTA_EXHAUSTED ──[配额恢复]──> AVAILABLE
    │
    │[不可用]
    ▼
UNAVAILABLE ──[恢复]──> AVAILABLE
"""
