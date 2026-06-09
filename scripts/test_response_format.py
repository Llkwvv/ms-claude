#!/usr/bin/env python3
"""
测试所有可用模型的响应格式一致性。
发送一个复杂问题，验证各模型返回的 JSON 结构是否符合 OpenAI chat.completion 标准格式。
结果保存到 data/model_format_test.json。
"""

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import yaml

API_KEY = "ms-043daf20-531c-4180-a99d-5852a7fafc46"
API_BASE = "https://api-inference.modelscope.cn"
TIMEOUT = 120
MAX_WORKERS = 5

# 复杂问题，能触发模型生成有结构的回答
COMPLEX_PROMPT = (
    "请解释贝尔不等式在量子力学中的意义，"
    "并用一个日常生活中的类比来说明量子纠缠为什么如此反直觉。"
    "要求回答分为三部分：1) 贝尔不等式的数学直觉 2) 实验验证的关键结论 3) 日常生活类比。"
)


def load_models(config_path: str) -> tuple[list[str], set[str]]:
    with open(config_path) as f:
        cfg = yaml.safe_load(f)
    priority = cfg.get("model_priority", [])
    blacklist = {entry["model"] for entry in cfg.get("blacklist", []) if isinstance(entry, dict)}
    return priority, blacklist


def test_model(model: str) -> dict:
    url = f"{API_BASE.rstrip('/')}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": COMPLEX_PROMPT}],
        "max_tokens": 256,
        "stream": False,
    }

    start = time.time()
    result = {
        "model": model,
        "status": None,
        "latency": None,
        "ok": False,
        "error": None,
        "structure": {},
        "content_preview": "",
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=TIMEOUT)
        result["latency"] = round(time.time() - start, 2)
        result["status"] = resp.status_code

        if resp.status_code != 200:
            result["error"] = f"HTTP {resp.status_code}: {resp.text[:200]}"
            return result

        data = resp.json()

        # 结构分析
        raw_choices = data.get("choices")
        choices = raw_choices if isinstance(raw_choices, list) else []

        structure = {
            "has_choices": isinstance(raw_choices, list),
            "choices_len": len(choices),
            "has_message": False,
            "message_type": None,
            "has_content": False,
            "content_type": None,
            "has_role": False,
            "role_value": None,
            "has_finish_reason": False,
            "finish_reason_value": None,
            "has_usage": isinstance(data.get("usage"), dict),
            "usage_keys": list(data.get("usage", {}).keys()) if isinstance(data.get("usage"), dict) else [],
            "has_id": "id" in data,
            "has_model": "model" in data,
            "has_object": "object" in data,
            "has_created": "created" in data,
            "extra_top_keys": [k for k in data.keys() if k not in {
                "id", "object", "created", "model", "choices", "usage"
            }],
        }

        if choices and isinstance(choices, list):
            msg = choices[0].get("message") if isinstance(choices[0], dict) else None
            structure["has_message"] = isinstance(msg, dict)
            structure["message_type"] = type(msg).__name__ if msg is not None else "missing"
            if isinstance(msg, dict):
                structure["has_content"] = "content" in msg
                content = msg.get("content")
                structure["content_type"] = type(content).__name__ if content is not None else "missing"
                structure["has_role"] = "role" in msg
                structure["role_value"] = msg.get("role")
            structure["has_finish_reason"] = isinstance(choices[0], dict) and "finish_reason" in choices[0]
            structure["finish_reason_value"] = choices[0].get("finish_reason") if isinstance(choices[0], dict) else None

        result["structure"] = structure
        result["ok"] = all([
            structure["has_choices"],
            structure["has_message"],
            structure["has_content"],
            structure["has_role"],
            structure["has_usage"],
            structure["has_finish_reason"],
        ])
        result["content_preview"] = str(
            choices[0].get("message", {}).get("content", "")[:80]
        ) if choices else ""

    except requests.Timeout:
        result["error"] = "timeout"
    except Exception as e:
        result["error"] = str(e)

    return result


def main():
    config_path = Path(__file__).resolve().parents[1] / "src" / "config" / "config.yaml"
    models, blacklist = load_models(str(config_path))
    available = [m for m in models if m not in blacklist]

    print(f"共 {len(available)} 个可用模型待测试（已排除 {len(blacklist)} 个黑名单）")
    print(f"并发数: {MAX_WORKERS}, 超时: {TIMEOUT}s, max_tokens: 256")
    print("=" * 80)

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_model = {executor.submit(test_model, m): m for m in available}
        for future in as_completed(future_to_model):
            res = future.result()
            results.append(res)
            marker = "✓" if res["ok"] else "✗"
            status_str = str(res['status']) if res['status'] is not None else "N/A"
            lat_str = str(res['latency']) if res['latency'] is not None else "N/A"
            print(
                f"{marker} {res['model']:<55} status={status_str:>4} "
                f"lat={lat_str:>6}s  preview={res['content_preview'][:30]}..."
            )

    # 统计
    ok_count = sum(1 for r in results if r["ok"])
    fail_count = len(results) - ok_count
    print("=" * 80)
    print(f"格式完全一致的模型: {ok_count}/{len(results)}")
    print(f"结构异常的模型: {fail_count}/{len(results)}")

    if fail_count > 0:
        print("\n异常详情:")
        for r in results:
            if not r["ok"]:
                print(f"  - {r['model']}: {r.get('error') or 'structure mismatch'}")
                print(f"    structure: {json.dumps(r.get('structure', {}), ensure_ascii=False)}")

    # 提取所有 response 的顶层 key 集合做交叉对比
    print("\n各模型返回 JSON 顶层键对比（抽样）:")
    for r in results[:5]:
        s = r.get("structure", {})
        print(f"  {r['model']}: extra_keys={s.get('extra_top_keys', [])}")

    # 保存结果
    data_dir = Path(__file__).resolve().parents[1] / "data"
    data_dir.mkdir(exist_ok=True)
    out_file = data_dir / "model_format_test.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump({
            "test_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "prompt": COMPLEX_PROMPT,
            "total_models": len(available),
            "ok_count": ok_count,
            "fail_count": fail_count,
            "results": results,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n详细结果已保存: {out_file}")


if __name__ == "__main__":
    main()
