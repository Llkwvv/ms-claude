#!/usr/bin/env python3
"""
代理功能测试脚本
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.core.proxy import ModelProxy, RequestContext
from src.models.model import Model, ModelStatus
from src.utils.config import Config

def test_basic_functionality():
    """测试基础功能"""
    print("\n" + "=" * 60)
    print("Test 1: Basic Functionality")
    print("=" * 60)

    # 初始化代理
    proxy = ModelProxy()
    print("✓ Proxy initialized")

    # 检查模型列表
    models = proxy.model_manager.get_all_models()
    print(f"✓ Loaded {len(models)} models")

    # 获取可用模型
    available = proxy.model_manager.get_available_models()
    print(f"✓ Available models: {len(available)}")

    # 选择模型
    selected = proxy.get_available_model()
    if selected:
        print(f"✓ Selected model: {selected.name}")
    else:
        print("✗ No model selected")
        return False

    return True


def test_priority_scheduler():
    """测试优先级调度器"""
    print("\n" + "=" * 60)
    print("Test 2: Priority Scheduler")
    print("=" * 60)

    from src.core.scheduler import PriorityScheduler

    config = Config()
    scheduler = PriorityScheduler(config)

    models = config.get("model_priority", [])
    print(f"Configured priority: {models}")

    # 创建测试模型
    test_models = []
    for i, name in enumerate(models[:3]):
        model = Model(
            name=name,
            display_name=f"Test {name}",
            priority=i
        )
        test_models.append(model)

    # 测试选择
    result = scheduler.select_model(test_models)
    if result.model:
        print(f"✓ Selected: {result.model.name} (priority_index: {result.priority_index})")
    else:
        print("✗ Selection failed")
        return False

    # 测试失败标记
    scheduler.mark_failure(result.model)
    print(f"✓ Failure marked (count: {result.model.failure_count})")

    # 测试成功标记
    scheduler.mark_success(result.model)
    print(f"✓ Success marked (count: {result.model.failure_count})")

    return True


def test_failure_tracking():
    """测试失败跟踪"""
    print("\n" + "=" * 60)
    print("Test 3: Failure Tracking")
    print("=" * 60)

    config = Config()
    proxy = ModelProxy()

    # 记录失败
    context = RequestContext(request_id="test-001")
    proxy.failure_tracker.record_failure(
        "test-model",
        "Test quota error",
        {"request_id": "test-001"}
    )
    print("✓ Failure recorded")

    # 获取统计
    stats = proxy.failure_tracker.get_failure_stats("test-model")
    print(f"✓ Total failures: {stats.get('total', 0)}")

    # 检查阈值
    over_threshold = proxy.failure_tracker.is_over_threshold("test-model")
    print(f"✓ Over threshold: {over_threshold}")

    # 记录成功
    proxy.failure_tracker.record_success("test-model")
    print("✓ Success recorded")

    # 获取模式
    patterns = proxy.failure_tracker.get_failure_patterns()
    print(f"✓ Failure patterns: {patterns.get('patterns', {})}")

    return True


def test_model_status():
    """测试模型状态管理"""
    print("\n" + "=" * 60)
    print("Test 4: Model Status Management")
    print("=" * 60)

    proxy = ModelProxy()

    # 测试状态切换
    model = proxy.model_manager.get_model("qwen-max")
    if model:
        print(f"✓ Original status: {model.status.value}")

        # 禁用模型
        proxy.model_manager.set_model_status(
            "qwen-max",
            ModelStatus.DISABLED
        )
        print(f"✓ Status after disable: {model.status.value}")

        # 启用模型
        proxy.model_manager.enable_model("qwen-max")
        print(f"✓ Status after enable: {model.status.value}")

    return True


def test_config():
    """测试配置管理"""
    print("\n" + "=" * 60)
    print("Test 5: Configuration Management")
    print("=" * 60)

    config = Config()

    # 测试读取
    api_base = config.get("modelscope.api_base")
    print(f"✓ API base: {api_base}")

    # 测试默认值
    missing = config.get("nonexistent.key", "default")
    print(f"✓ Default value: {missing}")

    # 测试设置
    config.set("test.key", "value")
    retrieved = config.get("test.key")
    print(f"✓ Set and get: {retrieved}")

    return True


def test_streaming_compatibility():
    """测试流式兼容性"""
    print("\n" + "=" * 60)
    print("Test 6: Streaming Compatibility")
    print("=" * 60)

    config = Config()
    streaming_enabled = config.get("streaming.enabled", False)

    print(f"✓ Streaming enabled: {streaming_enabled}")
    print(f"✓ Streaming timeout: {config.get('streaming.timeout')}")
    print(f"✓ Buffer size: {config.get('streaming.buffer_size')}")

    return True


def run_all_tests():
    """运行所有测试"""
    print("\n" + "#" * 60)
    print("# Model Proxy Test Suite")
    print("#" * 60)

    tests = [
        ("Basic Functionality", test_basic_functionality),
        ("Priority Scheduler", test_priority_scheduler),
        ("Failure Tracking", test_failure_tracking),
        ("Model Status Management", test_model_status),
        ("Configuration Management", test_config),
        ("Streaming Compatibility", test_streaming_compatibility),
    ]

    results = []
    for name, test_func in tests:
        try:
            success = test_func()
            results.append((name, success))
        except Exception as e:
            print(f"✗ Test failed with exception: {e}")
            import traceback
            traceback.print_exc()
            results.append((name, False))

    # 总结
    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)

    for name, success in results:
        status = "✓ PASS" if success else "✗ FAIL"
        print(f"{status:10s} | {name}")

    total = len(results)
    passed = sum(1 for _, s in results if s)
    print(f"\n{passed}/{total} tests passed")

    return passed == total


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
