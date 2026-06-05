#!/usr/bin/env python3
"""
批量测试 ModelScope 模型连通性
轻量级调用，仅验证模型是否可访问，不消耗大量额度
"""

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import yaml


def load_models(config_path: str) -> list[str]:
    with open(config_path) as f:
        cfg = yaml.safe_load(f)
    return cfg.get("model_priority", [])


def test_single_model(
    model: str,
    api_base: str,
    api_key: str,
    timeout: int = 30,
) -> dict:
    """
    对单个模型发送最小化请求，验证连通性。
    使用极短的 max_tokens 和简单的 prompt，降低额度消耗。
    """
    url = f"{api_base.rstrip('/')}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 2,
        "stream": False,
    }

    start = time.time()
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        latency = round(time.time() - start, 2)

        if resp.status_code == 200:
            try:
                data = resp.json()
                choice = (data.get("choices") or [{}])[0]
                content = choice.get("message", {}).get("content", "")
                return {
                    "model": model,
                    "status": "OK",
                    "http_code": resp.status_code,
                    "latency_s": latency,
                    "response_snippet": (content[:60] + "...") if len(content) > 60 else content,
                    "error": "",
                }
            except Exception as e:
                return {
                    "model": model,
                    "status": "PARSE_ERROR",
                    "http_code": resp.status_code,
                    "latency_s": latency,
                    "response_snippet": resp.text[:200],
                    "error": str(e),
                }
        else:
            try:
                err_body = resp.json()
                err_msg = err_body.get("error", {}).get("message", resp.text[:200])
            except Exception:
                err_msg = resp.text[:200]
            return {
                "model": model,
                "status": f"HTTP_{resp.status_code}",
                "http_code": resp.status_code,
                "latency_s": latency,
                "response_snippet": "",
                "error": err_msg,
            }
    except requests.exceptions.Timeout:
        return {
            "model": model,
            "status": "TIMEOUT",
            "http_code": 0,
            "latency_s": timeout,
            "response_snippet": "",
            "error": f"Request timeout after {timeout}s",
        }
    except Exception as e:
        return {
            "model": model,
            "status": "EXCEPTION",
            "http_code": 0,
            "latency_s": round(time.time() - start, 2),
            "response_snippet": "",
            "error": str(e),
        }


def main():
    config_path = Path(__file__).parent.parent / "src" / "config" / "config.yaml"
    models = load_models(str(config_path))

    api_base = os.environ.get("MS_CLAUDE_UPSTREAM_API_BASE", "https://api-inference.modelscope.cn")
    api_key = os.environ.get("MS_CLAUDE_UPSTREAM_API_KEY", "")

    if not api_key:
        print("错误：未设置 MS_CLAUDE_UPSTREAM_API_KEY 环境变量", file=sys.stderr)
        print("请执行：export MS_CLAUDE_UPSTREAM_API_KEY='your-modelscope-token'", file=sys.stderr)
        sys.exit(1)

    print(f"开始连通性测试 …")
    print(f"  上游: {api_base}")
    print(f"  模型数: {len(models)}")
    print(f"  请求间隔: 1.5s（避免限频）\n")

    results: list[dict] = []
    ok_count = 0
    fail_count = 0

    for idx, model in enumerate(models, 1):
        print(f"[{idx:2d}/{len(models)}] 测试 {model} ...", end=" ", flush=True)
        result = test_single_model(model, api_base, api_key)
        results.append(result)

        if result["status"] == "OK":
            ok_count += 1
            print(f"OK ({result['latency_s']}s) {result['response_snippet'][:40]}")
        else:
            fail_count += 1
            print(f"{result['status']} - {result['error'][:80]}")

        # 限频保护：模型间间隔 1.5 秒
        if idx < len(models):
            time.sleep(1.5)

    # 汇总
    print("\n" + "=" * 80)
    print("测试汇总")
    print("=" * 80)
    print(f"总计: {len(models)} | 成功: {ok_count} | 失败: {fail_count}")
    print()

    if fail_count:
        print("失败模型列表:")
        for r in results:
            if r["status"] != "OK":
                print(f"  - {r['model']}: {r['status']} | {r['error'][:120]}")
        print()

    # 保存结果
    result_file = Path("data/model_connectivity_test.json")
    result_file.parent.mkdir(parents=True, exist_ok=True)
    with open(result_file, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"详细结果已保存至: {result_file}")


if __name__ == "__main__":
    main()
