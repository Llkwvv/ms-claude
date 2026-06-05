#!/usr/bin/env python3
"""
更新模型列表脚本

从ModelScope拉取最新模型列表并更新缓存
"""

import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.models.manager import ModelManager
from src.utils.config import Config

def main():
    """主函数"""
    print("=" * 60)
    print("ModelScope Model List Updater")
    print("=" * 60)

    home_path = os.environ.get("MS_CLAUDE_HOME")
    config_path = os.environ.get("MS_CLAUDE_CONFIG")
    if config_path is None:
        config_path = (
            str(Path(home_path).expanduser().resolve() / "config.yaml")
            if home_path else "src/config/config.yaml"
        )

    # 加载配置
    config = Config(config_path, home_path=home_path)
    print(f"Config loaded from {config_path}")

    # 初始化模型管理器
    manager = ModelManager(config)
    print(f"Current models: {len(manager.get_all_models())}")

    # 更新模型列表
    print("\nFetching from ModelScope...")
    success = manager.update_from_modelscope()

    if success:
        print("✓ Update successful!")
        print(f"Total models: {len(manager.get_all_models())}")
        print(f"Available: {len(manager.get_available_models())}")

        # 显示模型列表
        print("\nModel list (by priority):")
        for model in manager.get_models_by_priority():
            print(f"  {model.priority:2d}. {model.name}")
    else:
        print("✗ Update failed")
        sys.exit(1)

if __name__ == "__main__":
    main()
