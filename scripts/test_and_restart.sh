#!/bin/bash
# Auto-restart proxy with new code and run model switch tests

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "=========================================="
echo "  Restart proxy + test @model command"
echo "=========================================="

# 1. Kill existing proxy
echo "[1/4] Stopping existing proxy..."
pkill -f "python3 -m src.main --serve" 2>/dev/null || true
sleep 2

# 2. Start proxy in background
echo "[2/4] Starting proxy with new code..."
cd "$REPO_ROOT"
python3 -m src.main --serve \
    --config ~/.config/ms-claude/config.yaml \
    --host 127.0.0.1 --port 8081 \
    --home ~/.config/ms-claude \
    > /tmp/proxy_test.log 2>&1 &
PROXY_PID=$!
echo "Proxy PID: $PROXY_PID"

# 3. Wait for proxy to be ready
echo "[3/4] Waiting for proxy to start..."
for i in {1..15}; do
    if curl -s http://127.0.0.1:8081/v1/models > /dev/null 2>&1; then
        echo "Proxy ready!"
        break
    fi
    sleep 1
    if [ $i -eq 15 ]; then
        echo "ERROR: Proxy failed to start"
        cat /tmp/proxy_test.log
        exit 1
    fi
done

# 4. Run tests
echo "[4/4] Running tests..."
python3 "$REPO_ROOT/scripts/test_model_switch.py"

TEST_EXIT=$?

if [ $TEST_EXIT -eq 0 ]; then
    echo ""
    echo "All tests PASSED!"
else
    echo ""
    echo "Some tests FAILED. Check proxy logs:"
    tail -n 30 /tmp/proxy_test.log
fi

# Cleanup
kill $PROXY_PID 2>/dev/null || true
exit $TEST_EXIT
