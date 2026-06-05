"""
失败记录服务 - 负责记录和管理模型失败情况
"""

import json
import logging
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional


class FailureRecord:
    """失败记录条目"""

    def __init__(
        self,
        model_name: str,
        error_message: str,
        request_context: Dict[str, Any],
        timestamp: Optional[float] = None
    ):
        """
        初始化失败记录

        Args:
            model_name: 模型名称
            error_message: 错误消息
            request_context: 请求上下文
            timestamp: 时间戳
        """
        self.model_name = model_name
        self.error_message = error_message
        self.request_context = request_context
        self.timestamp = timestamp or time.time()
        self.error_type = self._classify_error(error_message)

    def _classify_error(self, error_message: str) -> str:
        """
        分类错误类型

        Args:
            error_message: 错误消息

        Returns:
            错误类型
        """
        error_lower = error_message.lower()

        error_patterns = {
            "quota": ["quota", "limit", "exceeded"],
            "rate_limit": ["rate", "frequency", "too many"],
            "timeout": ["timeout", "timed out"],
            "connection": ["connection", "network", "refused"],
            "authentication": ["auth", "token", "credential"],
            "server": ["server", "internal", "5xx"],
            "client": ["client", "bad request", "4xx"],
            "parsing": ["parse", "format", "invalid"],
        }

        for error_type, patterns in error_patterns.items():
            if any(pattern in error_lower for pattern in patterns):
                return error_type

        return "unknown"

    def to_dict(self) -> Dict[str, Any]:
        """
        转换为字典

        Returns:
            字典表示
        """
        return {
            "model_name": self.model_name,
            "error_message": self.error_message,
            "error_type": self.error_type,
            "request_context": self.request_context,
            "timestamp": self.timestamp,
            "datetime": datetime.fromtimestamp(
                self.timestamp
            ).isoformat()
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FailureRecord":
        """
        从字典创建失败记录

        Args:
            data: 字典数据

        Returns:
            失败记录实例
        """
        record = cls(
            model_name=data["model_name"],
            error_message=data["error_message"],
            request_context=data["request_context"],
            timestamp=data["timestamp"]
        )
        record.error_type = data.get("error_type", "unknown")
        return record


class FailureTracker:
    """
    失败跟踪器

    负责记录模型失败情况，分析失败模式，提供优化建议
    """

    def __init__(self, config: Any):
        """
        初始化失败跟踪器

        Args:
            config: 配置对象
        """
        self.config = config
        self.logger = logging.getLogger(__name__)

        # 线程锁
        self._lock = Lock()

        # 失败记录
        self._failures: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=100)  # 每个模型最多保留100条记录
        )

        # 失败统计
        self._stats: Dict[str, Dict[str, int]] = defaultdict(
            lambda: defaultdict(int)
        )

        # 最近失败时间
        self._recent_failures: deque = deque(maxlen=1000)

        # 配置文件
        self.reload_config()

    def reload_config(self) -> None:
        """重新加载失败追踪配置（支持热重载）。"""
        failure_config = self.config.get("failure_tracking", {})
        self._enabled = failure_config.get("enabled", True)
        self._log_file = self.config.resolve_path(
            failure_config.get("log_file", "logs/failures.jsonl"),
            "logs/failures.jsonl"
        )
        self._base_log_file = self._log_file
        self._failure_threshold = failure_config.get(
            "failure_threshold", 5
        )
        self._time_window = failure_config.get(
            "time_window", 3600
        )

        # 确保日志目录存在
        self._log_file.parent.mkdir(parents=True, exist_ok=True)

    def record_failure(
        self,
        model_name: str,
        error_message: str,
        request_context: Dict[str, Any]
    ) -> FailureRecord:
        """
        记录失败

        Args:
            model_name: 模型名称
            error_message: 错误消息
            request_context: 请求上下文

        Returns:
            失败记录
        """
        if not self._enabled:
            return

        with self._lock:
            # 创建失败记录
            record = FailureRecord(
                model_name, error_message, request_context
            )

            # 添加到记录队列
            self._failures[model_name].append(record)

            # 更新统计
            self._stats[model_name][record.error_type] += 1
            self._stats[model_name]["total"] += 1

            # 添加到最近失败列表
            self._recent_failures.append(record)

            # 写入日志文件
            self._write_to_log(record)

            self.logger.warning(
                f"Recorded failure for model {model_name}: "
                f"{record.error_type} - {error_message[:100]}"
            )

            return record

    def record_success(self, model_name: str):
        """
        记录成功请求

        Args:
            model_name: 模型名称
        """
        with self._lock:
            self._stats[model_name]["success"] += 1

    def is_over_threshold(self, model_name: str) -> bool:
        """
        检查模型是否超过失败阈值

        Args:
            model_name: 模型名称

        Returns:
            是否超过阈值
        """
        with self._lock:
            # 获取时间窗口内的失败次数
            recent_count = self._get_recent_failure_count(
                model_name, self._time_window
            )

            return recent_count >= self._failure_threshold

    def _get_recent_failure_count(
        self,
        model_name: str,
        time_window: float
    ) -> int:
        """
        获取时间窗口内的失败次数

        Args:
            model_name: 模型名称
            time_window: 时间窗口（秒）

        Returns:
            失败次数
        """
        current_time = time.time()
        count = 0

        for record in self._failures.get(model_name, []):
            if current_time - record.timestamp <= time_window:
                count += 1

        return count

    def get_failure_stats(self, model_name: str) -> Dict[str, Any]:
        """
        获取失败统计信息

        Args:
            model_name: 模型名称

        Returns:
            统计信息字典
        """
        with self._lock:
            stats = dict(self._stats.get(model_name, {}))

            # 添加额外信息
            failures = self._failures.get(model_name, [])
            if failures:
                stats["last_failure"] = failures[-1].to_dict()
                stats["recent_failures"] = len([
                    f for f in failures
                    if time.time() - f.timestamp <= self._time_window
                ])
            else:
                stats["last_failure"] = None
                stats["recent_failures"] = 0

            # 计算成功率
            total = stats.get("total", 0)
            success = stats.get("success", 0)
            if total > 0:
                stats["success_rate"] = success / (total + success)
            else:
                stats["success_rate"] = 1.0

            return stats

    def get_all_stats(self) -> Dict[str, Dict[str, Any]]:
        """
        获取所有模型的失败统计

        Returns:
            统计信息字典
        """
        with self._lock:
            stats = {}
            for model_name in self._failures.keys():
                stats[model_name] = self.get_failure_stats(model_name)
            return stats

    def get_failure_patterns(
        self,
        model_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        获取失败模式分析

        Args:
            model_name: 模型名称（可选）

        Returns:
            失败模式分析
        """
        with self._lock:
            if model_name:
                failures = list(self._failures.get(model_name, []))
            else:
                failures = list(self._recent_failures)

            if not failures:
                return {"patterns": {}, "recommendations": []}

            # 分析错误类型分布
            error_types = defaultdict(int)
            error_messages = defaultdict(list)

            for failure in failures:
                error_types[failure.error_type] += 1
                error_messages[failure.error_type].append(
                    failure.error_message
                )

            # 生成建议
            recommendations = self._generate_recommendations(
                error_types, error_messages
            )

            return {
                "patterns": dict(error_types),
                "error_samples": {
                    etype: msgs[:3]  # 每种类型最多3个样本
                    for etype, msgs in error_messages.items()
                },
                "recommendations": recommendations,
                "total_failures": len(failures)
            }

    def _generate_recommendations(
        self,
        error_types: Dict[str, int],
        error_messages: Dict[str, List[str]]
    ) -> List[str]:
        """
        生成优化建议

        Args:
            error_types: 错误类型统计
            error_messages: 错误消息

        Returns:
            建议列表
        """
        recommendations = []

        total = sum(error_types.values())
        if total == 0:
            return recommendations

        # 额度问题
        quota_ratio = error_types.get("quota", 0) / total
        if quota_ratio > 0.3:
            recommendations.append(
                "Quota exhaustion detected ({}%). "
                "Consider upgrading plan or adding more models.".format(
                    int(quota_ratio * 100)
                )
            )

        # 限频问题
        rate_limit_ratio = error_types.get("rate_limit", 0) / total
        if rate_limit_ratio > 0.2:
            recommendations.append(
                "Rate limiting detected ({}%). "
                "Consider implementing request throttling.".format(
                    int(rate_limit_ratio * 100)
                )
            )

        # 超时问题
        timeout_ratio = error_types.get("timeout", 0) / total
        if timeout_ratio > 0.2:
            recommendations.append(
                "Timeout issues detected ({}%). "
                "Consider increasing timeout or optimizing requests.".format(
                    int(timeout_ratio * 100)
                )
            )

        # 连接问题
        connection_ratio = error_types.get("connection", 0) / total
        if connection_ratio > 0.1:
            recommendations.append(
                "Connection issues detected ({}%). "
                "Check network connectivity.".format(
                    int(connection_ratio * 100)
                )
            )

        return recommendations

    def clear_failures(self, model_name: str):
        """
        清除模型的失败记录

        Args:
            model_name: 模型名称
        """
        with self._lock:
            if model_name in self._failures:
                self._failures[model_name].clear()
                self._stats[model_name].clear()
                self.logger.info(
                    f"Cleared failures for model {model_name}"
                )

    def _get_current_log_file(self) -> Path:
        """获取当前日期的日志文件路径（按日期切割）。"""
        date_str = datetime.now().strftime("%Y%m%d")
        base = self._base_log_file
        if base.suffix == ".jsonl":
            return base.with_suffix(f".{date_str}.jsonl")
        return base.parent / f"{base.stem}-{date_str}{base.suffix}"

    def _get_recent_log_files(self, days: int = 7) -> List[Path]:
        """获取最近 N 天的日志文件路径列表。"""
        files = []
        for i in range(days):
            date_str = (datetime.now() - timedelta(days=i)).strftime("%Y%m%d")
            base = self._base_log_file
            if base.suffix == ".jsonl":
                files.append(base.with_suffix(f".{date_str}.jsonl"))
            else:
                files.append(base.parent / f"{base.stem}-{date_str}{base.suffix}")
        return files

    def _write_to_log(self, record: FailureRecord):
        """
        写入日志文件（按日期自动切割）。

        Args:
            record: 失败记录
        """
        try:
            log_file = self._get_current_log_file()
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(record.to_dict(), ensure_ascii=False))
                f.write('\n')
        except Exception as e:
            self.logger.error(f"Error writing to failure log: {e}")

    def load_from_log(self) -> int:
        """
        从日志文件加载失败记录（加载最近7天的日志）。

        Returns:
            加载的记录数
        """
        count = 0
        for log_file in self._get_recent_log_files(days=7):
            if not log_file.exists():
                continue
            try:
                with open(log_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue

                        try:
                            data = json.loads(line)
                            record = FailureRecord.from_dict(data)

                            with self._lock:
                                self._failures[record.model_name].append(record)
                                self._stats[record.model_name][
                                    record.error_type
                                ] += 1
                                self._stats[record.model_name]["total"] += 1

                            count += 1
                        except Exception as e:
                            self.logger.error(
                                f"Error parsing log line: {e}"
                            )

            except Exception as e:
                self.logger.error(f"Error loading failure log {log_file}: {e}")

        self.logger.info(f"Loaded {count} failure records from log")
        return count

    def get_recent_failures(
        self,
        limit: int = 100
    ) -> List[FailureRecord]:
        """
        获取最近的失败记录

        Args:
            limit: 返回数量限制

        Returns:
            失败记录列表
        """
        with self._lock:
            return list(self._recent_failures)[-limit:]

    def export_statistics(self, output_path: Path) -> bool:
        """
        导出统计信息

        Args:
            output_path: 输出文件路径

        Returns:
            是否成功
        """
        try:
            stats = {
                "generated_at": datetime.now().isoformat(),
                "model_stats": self.get_all_stats(),
                "failure_patterns": self.get_failure_patterns(),
                "recent_failures": [
                    f.to_dict() for f in self.get_recent_failures(100)
                ]
            }

            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(stats, f, ensure_ascii=False, indent=2)

            self.logger.info(f"Exported statistics to {output_path}")
            return True

        except Exception as e:
            self.logger.error(f"Error exporting statistics: {e}")
            return False

    def reset_stats(self):
        """重置所有统计信息"""
        with self._lock:
            self._failures.clear()
            self._stats.clear()
            self._recent_failures.clear()
        self.logger.info("Reset all failure statistics")

    def get_stats(self) -> Dict[str, Any]:
        """获取所有统计信息"""
        with self._lock:
            return dict(self._stats)

    def __len__(self) -> int:
        """总失败记录数"""
        with self._lock:
            return sum(len(f) for f in self._failures.values())
