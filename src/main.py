#!/usr/bin/env python3
"""
ModelScope Claude Code Proxy - 主程序

轻量级模型代理，用于自动切换和调度多个模型
"""

import argparse
import json
import logging
import os
import shlex
import sys
import time
import subprocess
import socket
import shutil
from pathlib import Path
from typing import Optional

_INTERACTIVE_HELP = """\nCommands:
  status  - Show proxy status
  test    - Test proxy with sample request
  update  - Update model list from ModelScope
  help    - Show this help message
  quit    - Exit
"""

# 确保可以导入src模块
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.proxy import ModelProxy
from src.server.proxy_server import run_proxy_server
from src.utils.config import Config
from src.utils.logger import setup_logger


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description="ModelScope Claude Code Proxy - 模型代理"
    )
    parser.add_argument(
        "--config",
        default=None,
        help="配置文件路径"
    )
    parser.add_argument(
        "--home",
        default=os.environ.get("MS_CLAUDE_HOME"),
        help="应用 home 目录（用于隔离配置、数据和日志）"
    )
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("MS_CLAUDE_DATA_DIR"),
        help="数据目录（优先于配置文件中的 app.data_dir）"
    )
    parser.add_argument(
        "--log-dir",
        default=os.environ.get("MS_CLAUDE_LOG_DIR"),
        help="日志目录（优先于配置文件中的 app.log_dir）"
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="从ModelScope更新模型列表"
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="显示代理状态"
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="测试代理功能"
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="详细输出"
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="启动 HTTP 代理服务"
    )
    parser.add_argument(
        "--connect",
        action="store_true",
        help="启动代理并自动启动 Claude 客户端"
    )
    parser.add_argument(
        "--prepare-claude",
        action="store_true",
        help="备份并修改 Claude 配置，使其指向本地 8080 代理"
    )
    parser.add_argument(
        "--claude-settings",
        default=os.environ.get("MS_CLAUDE_SETTINGS"),
        help="Claude settings.json 路径"
    )
    parser.add_argument(
        "--client-cmd",
        default=os.environ.get("MS_CLAUDE_CLIENT_CMD", "claude"),
        help="要启动的 Claude 客户端命令"
    )
    parser.add_argument(
        "--client-args",
        default=os.environ.get("MS_CLAUDE_CLIENT_ARGS", ""),
        help="传给 Claude 客户端的额外参数"
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("MS_CLAUDE_HOST"),
        help="HTTP 服务监听地址"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MS_CLAUDE_PORT", "0") or "0"),
        help="HTTP 服务监听端口"
    )

    args = parser.parse_args()

    # 设置日志级别
    log_level = "DEBUG" if args.verbose else "INFO"

    # 计算隔离后的默认路径
    home_path = Path(args.home).expanduser().resolve() if args.home else None
    config_path = Path(args.config).expanduser() if args.config else None
    if config_path is None:
        config_path = (
            home_path / "config.yaml"
            if home_path else Path("src/config/config.yaml")
        )

    # 初始化配置
    config = Config(
        str(config_path),
        home_path=str(home_path) if home_path else None
    )
    claude_settings_path = Path(args.claude_settings).expanduser() if args.claude_settings else (
        config.home_path / ".claude" / "settings.json"
    )
    if args.data_dir:
        config.set("app.data_dir", args.data_dir)
        config.set(
            "app.model_cache_file",
            str(Path(args.data_dir).expanduser() / "models_cache.json")
        )
    if args.log_dir:
        config.set("app.log_dir", args.log_dir)
        log_dir_path = Path(args.log_dir).expanduser()
        config.set(
            "failure_tracking.log_file",
            str(log_dir_path / "failures.jsonl")
        )
        config.set(
            "app.proxy_log_file",
            str(log_dir_path / "proxy.log")
        )
    if args.host:
        config.set("proxy.host", args.host)
    if args.port:
        config.set("proxy.port", args.port)
    if args.connect and not args.serve:
        args.serve = True

    if args.prepare_claude:
        _prepare_claude_settings(
            settings_path=claude_settings_path,
            host=config.get("proxy.host", "127.0.0.1"),
            port=int(config.get("proxy.port", 8080)),
        )
        return

    proxy_log_file = config.resolve_path(
        config.get("app.proxy_log_file", "logs/proxy.log"),
        "logs/proxy.log"
    )
    setup_logger(
        "ModelProxy",
        level=log_level,
        log_file=str(proxy_log_file),
        format_str=config.get("logging.format")
    )
    logger = logging.getLogger("ModelProxy")
    logger.info("=" * 60)
    logger.info("ModelScope Claude Code Proxy")
    logger.info("=" * 60)
    logger.info(f"Config loaded from: {config_path}")
    logger.info(f"Home directory: {config.home_path}")

    if args.serve:
        if args.connect:
            _prepare_claude_settings(
                settings_path=claude_settings_path,
                host=config.get("proxy.host", "127.0.0.1"),
                port=int(config.get("proxy.port", 8080)),
            )
            _run_connect_mode(
                config=config,
                config_path=str(config_path),
                claude_settings_path=str(claude_settings_path),
                home_path=str(home_path) if home_path else None,
                data_dir=args.data_dir,
                log_dir=args.log_dir,
                verbose=args.verbose,
                client_cmd=args.client_cmd,
                client_args=args.client_args,
                logger=logger,
            )
            return

        logger.info(
            "Starting HTTP proxy at %s:%s",
            config.get("proxy.host", "127.0.0.1"),
            config.get("proxy.port", 8080),
        )
        run_proxy_server(
            config,
            host=config.get("proxy.host", "127.0.0.1"),
            port=config.get("proxy.port", 8080)
        )
        return

    # 初始化代理
    proxy = ModelProxy(config)

    # 执行命令
    if args.update:
        # 更新模型列表
        logger.info("Updating model list from ModelScope...")
        success = proxy.model_manager.update_from_modelscope()
        if success:
            logger.info("Model list updated successfully")
        else:
            logger.error("Failed to update model list")
            sys.exit(1)

    elif args.status:
        # 显示状态
        status = proxy.get_status()
        print("\n" + "=" * 60)
        print("Model Proxy Status")
        print("=" * 60)
        print(f"\nTotal models: {status['total_models']}")
        print(f"Available models: {status['available_models']}")
        print(f"\nModel Details:")
        print("-" * 60)
        for model in status['model_details']:
            status_icon = "✓" if model['status'] == 'available' else "✗"
            print(
                f"  {status_icon} {model['name']:30s} "
                f"(priority: {model['priority']}, "
                f"status: {model['status']})"
            )
        print()

    elif args.test:
        # 测试代理功能
        print("\n" + "=" * 60)
        print("Testing Model Proxy")
        print("=" * 60)

        # 获取可用模型
        available_models = proxy.model_manager.get_available_models()
        if not available_models:
            logger.error("No available models found")
            sys.exit(1)

        print(f"\nFound {len(available_models)} available models:")
        for model in available_models:
            print(f"  - {model.name} (priority: {model.priority})")

        # 测试模型选择
        print("\nTesting model selection...")
        model = proxy.get_available_model()
        if model:
            print(f"Selected model: {model.name}")
        else:
            print("Failed to select model")

        # 测试失败跟踪
        print("\nTesting failure tracking...")
        proxy.failure_tracker.record_failure(
            "test-model",
            "Test error message",
            {"request_id": "test-123"}
        )
        stats = proxy.failure_tracker.get_failure_stats("test-model")
        print(f"Failure recorded: {stats['total']} total failures")

        # 测试状态报告
        print("\nGetting health report...")
        from src.core.scheduler import PriorityScheduler
        scheduler = PriorityScheduler(config)
        health = scheduler.get_health_report(
            proxy.model_manager.get_all_models()
        )
        print(f"Health: {health['available']}/{health['total']} models available")

        print("\n✓ All tests passed!")

    else:
        # 交互模式
        print("\n" + "=" * 60)
        print("Model Proxy - Interactive Mode")
        print("=" * 60)
        print(_INTERACTIVE_HELP)

        while True:
            try:
                command = input("proxy> ").strip().lower()

                if command in ("quit", "exit", "q"):
                    break
                elif command == "status":
                    status = proxy.get_status()
                    print(f"\nModels: {status['total_models']} total, "
                          f"{status['available_models']} available")
                    for model in status['model_details']:
                        print(f"  {model['name']}: {model['status']}")
                elif command == "test":
                    model = proxy.get_available_model()
                    if model:
                        print(f"Selected model: {model.name}")
                    else:
                        print("No available model")
                elif command == "update":
                    print("Updating model list...")
                    proxy.model_manager.update_from_modelscope()
                    print("Done")
                elif command == "help":
                    print(_INTERACTIVE_HELP)
                else:
                    print(f"Unknown command: {command}")

            except KeyboardInterrupt:
                print("\nExiting...")
                break
            except Exception as e:
                print(f"Error: {e}")

    logger.info("Proxy shutdown")


def _wait_for_port(host: str, port: int, timeout_seconds: int = 15) -> bool:
    """等待本地端口可用。"""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def _run_connect_mode(
    config: Config,
    config_path: str,
    claude_settings_path: str,
    home_path: Optional[str],
    data_dir: Optional[str],
    log_dir: Optional[str],
    verbose: bool,
    client_cmd: str,
    client_args: str,
    logger: logging.Logger,
) -> None:
    """启动代理并自动启动 Claude 客户端。"""
    host = config.get("proxy.host", "127.0.0.1")
    port = int(config.get("proxy.port", 8080))

    proxy_args = [
        sys.executable,
        "-m",
        "src.main",
        "--serve",
        "--config",
        config_path,
        "--host",
        str(host),
        "--port",
        str(port),
    ]
    if home_path:
        proxy_args.extend(["--home", home_path])
    if data_dir:
        proxy_args.extend(["--data-dir", data_dir])
    if log_dir:
        proxy_args.extend(["--log-dir", log_dir])
    if verbose:
        proxy_args.append("--verbose")

    proxy_env = os.environ.copy()
    proxy_env["MS_CLAUDE_HOME"] = str(config.home_path)
    proxy_env["MS_CLAUDE_HOST"] = str(host)
    proxy_env["MS_CLAUDE_PORT"] = str(port)
    proxy_env["MS_CLAUDE_SETTINGS"] = claude_settings_path

    logger.info("Launching proxy process: %s", " ".join(proxy_args))
    proxy_process = subprocess.Popen(proxy_args, env=proxy_env)
    try:
        if not _wait_for_port(host, port):
            raise RuntimeError(f"Proxy did not become ready on {host}:{port}")

        client_command = shlex.split(client_cmd)
        if client_args:
            client_command.extend(shlex.split(client_args))

        client_env = os.environ.copy()
        # 强制 UTF-8 locale，防止 Claude CLI 在非 UTF-8 环境下输出乱码
        for key in ("LC_ALL", "LANG"):
            val = client_env.get(key, "")
            if "utf" not in val.lower().replace("-", "").replace("_", ""):
                client_env[key] = "C.utf8"

        client_env["ANTHROPIC_BASE_URL"] = f"http://{host}:{port}"
        client_env["OPENAI_BASE_URL"] = f"http://{host}:{port}/v1"
        client_env["OPENAI_API_BASE"] = f"http://{host}:{port}/v1"
        client_env["MS_CLAUDE_SETTINGS"] = claude_settings_path
        client_env.pop("ANTHROPIC_API_KEY", None)
        client_env.pop("OPENAI_API_KEY", None)

        logger.info("Launching Claude client: %s", " ".join(client_command))
        client_cwd = os.environ.get("MS_CLAUDE_WORKSPACE")
        if client_cwd:
            logger.info("Claude client cwd: %s", client_cwd)
        client_result = subprocess.run(
            client_command,
            env=client_env,
            cwd=client_cwd if client_cwd else None
        )
        if client_result.returncode != 0:
            raise SystemExit(client_result.returncode)
    finally:
        proxy_process.terminate()
        try:
            proxy_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proxy_process.kill()
            proxy_process.wait(timeout=5)


def _prepare_claude_settings(
    settings_path: Path,
    host: str,
    port: int,
) -> None:
    """备份并修改 Claude settings.json，使其指向本地代理。"""
    settings_path = settings_path.expanduser()
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    if settings_path.exists():
        with open(settings_path, "r", encoding="utf-8") as file_handle:
            settings_data = json.load(file_handle)

        backup_path = settings_path.with_suffix(settings_path.suffix + ".bak")
        shutil.copy2(settings_path, backup_path)
    else:
        settings_data = {
            "env": {},
            "includeCoAuthoredBy": False,
            "permissions": {"defaultMode": "auto"},
            "hooks": {},
        }
        backup_path = None

    env = settings_data.setdefault("env", {})
    env["ANTHROPIC_BASE_URL"] = f"http://{host}:{port}"
    env["ANTHROPIC_AUTH_TOKEN"] = env.get("ANTHROPIC_AUTH_TOKEN", "PROXY_MANAGED")

    # 清理旧的 cc-switch hooks，避免找不到脚本时报错
    hooks = settings_data.get("hooks", {})
    hooks.pop("UserPromptSubmit", None)
    hooks.pop("Stop", None)
    settings_data["hooks"] = hooks

    with open(settings_path, "w", encoding="utf-8") as file_handle:
        json.dump(settings_data, file_handle, ensure_ascii=False, indent=2)
        file_handle.write("\n")

    print(f"Updated Claude settings: {settings_path}")
    if backup_path:
        print(f"Backup created: {backup_path}")
    print(f"Claude base URL -> http://{host}:{port}")


if __name__ == "__main__":
    main()
