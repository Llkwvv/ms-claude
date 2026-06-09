#!/usr/bin/env python3
"""Test @model switch command against the running proxy."""

import json
import sys
import time
import requests


def test_non_stream():
    """Test @model command with stream=false."""
    print("=" * 50)
    print("Test 1: Non-stream @model command")
    print("=" * 50)

    payload = {
        "model": "claude-sonnet-4-20250514",
        "messages": [
            {"role": "user", "content": "@model Qwen/Qwen3-235B-A22B-Instruct-2507"}
        ],
        "stream": False,
        "max_tokens": 1024,
    }

    try:
        resp = requests.post(
            "http://127.0.0.1:8081/v1/messages",
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=10,
        )
        print(f"Status: {resp.status_code}")
        print(f"Content-Type: {resp.headers.get('content-type')}")

        if resp.status_code == 200:
            data = resp.json()
            text = data.get("content", [{}])[0].get("text", "")
            print(f"Response text: {text}")

            if "Switched to model" in text or "switched" in text.lower():
                print("PASS: @model command handled correctly (non-stream)")
                return True
            else:
                print("FAIL: Response does not indicate model switch")
                print(f"Full response: {json.dumps(data, indent=2)}")
                return False
        else:
            print(f"FAIL: HTTP {resp.status_code}")
            print(resp.text[:500])
            return False

    except Exception as e:
        print(f"ERROR: {e}")
        return False


def test_stream():
    """Test @model command with stream=true."""
    print("\n" + "=" * 50)
    print("Test 2: Stream @model command")
    print("=" * 50)

    payload = {
        "model": "claude-sonnet-4-20250514",
        "messages": [
            {"role": "user", "content": "@model Qwen/Qwen3-235B-A22B-Instruct-2507"}
        ],
        "stream": True,
        "max_tokens": 1024,
    }

    events = []
    text_parts = []

    try:
        resp = requests.post(
            "http://127.0.0.1:8081/v1/messages",
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=(5, 10),  # (connect, read) timeout
            stream=True,
        )
        print(f"Status: {resp.status_code}")
        print(f"Content-Type: {resp.headers.get('content-type')}")

        if resp.status_code != 200:
            print(f"FAIL: HTTP {resp.status_code}")
            print(resp.text[:500])
            return False

        # Parse SSE events with timeout handling
        start_time = time.time()
        max_wait = 5  # seconds to wait for SSE events

        for chunk in resp.iter_content(chunk_size=512, decode_unicode=True):
            if not chunk:
                continue
            for line in chunk.split("\n"):
                line = line.strip()
                if line.startswith("data: "):
                    data_str = line[6:]
                    if data_str.strip():
                        try:
                            event = json.loads(data_str)
                            events.append(event)
                            if event.get("type") == "content_block_delta":
                                delta = event.get("delta", {})
                                if delta.get("type") == "text_delta":
                                    text_parts.append(delta.get("text", ""))
                        except json.JSONDecodeError:
                            pass
            # Check if we got the message_stop event
            if events and events[-1].get("type") == "message_stop":
                break
            if time.time() - start_time > max_wait:
                print("WARN: Read timeout, stopping SSE read")
                break

    except Exception as e:
        print(f"WARN: Exception during stream read: {e}")
        # Still check what we collected so far

    full_text = "".join(text_parts)
    print(f"Streamed text: {full_text[:200]}")

    if "Switched to model" in full_text or "switched" in full_text.lower():
        print("PASS: @model command handled correctly (stream)")
        return True
    else:
        print("FAIL: Stream response does not indicate model switch")
        print(f"Events collected: {len(events)}")
        for ev in events[:10]:
            print(f"  - type={ev.get('type')}, keys={list(ev.keys())}")
        return False


def test_with_system_reminder():
    """Test @model command when content contains system reminder text."""
    print("\n" + "=" * 50)
    print("Test 3: @model with system reminder appended")
    print("=" * 50)

    # Simulate what Claude CLI actually sends
    payload = {
        "model": "claude-sonnet-4-20250514",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "@model Qwen/Qwen3-235B-A22B-Instruct-2507\n\n<system-reminder>\n# MCP Server Instructions\n..."
                    }
                ]
            }
        ],
        "stream": False,
        "max_tokens": 1024,
    }

    try:
        resp = requests.post(
            "http://127.0.0.1:8081/v1/messages",
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=10,
        )
        print(f"Status: {resp.status_code}")

        if resp.status_code == 200:
            data = resp.json()
            text = data.get("content", [{}])[0].get("text", "")
            print(f"Response text: {text}")

            if "Switched to model" in text or "switched" in text.lower():
                print("PASS: @model command works with system reminder (non-stream)")
                return True
            else:
                print("FAIL: Model switch not detected")
                return False
        else:
            print(f"FAIL: HTTP {resp.status_code}")
            return False

    except Exception as e:
        print(f"ERROR: {e}")
        return False


def main():
    print("Testing ms-claude @model command")
    print(f"Proxy: http://127.0.0.1:8081")
    print()

    # Check proxy is running
    try:
        requests.get("http://127.0.0.1:8081/v1/models", timeout=2)
    except Exception:
        print("ERROR: Proxy is not running on port 8081")
        print("Please start it first: ms-claude --model <model>")
        sys.exit(1)

    results = []
    results.append(("Non-stream", test_non_stream()))
    time.sleep(0.5)
    results.append(("Stream", test_stream()))
    time.sleep(0.5)
    results.append(("With reminder", test_with_system_reminder()))

    print("\n" + "=" * 50)
    print("SUMMARY")
    print("=" * 50)
    for name, passed in results:
        status = "PASS" if passed else "FAIL"
        print(f"  {name}: {status}")

    all_pass = all(r[1] for r in results)
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
