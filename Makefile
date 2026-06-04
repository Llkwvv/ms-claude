# Makefile for Model Proxy Project

.PHONY: help install test run update clean format lint

# 默认目标
help:
	@echo "Model Scope Claude Code Proxy - Makefile"
	@echo ""
	@echo "Available targets:"
	@echo "  install    - Install dependencies"
	@echo "  test       - Run tests"
	@echo "  run        - Run the proxy (interactive mode)"
	@echo "  update     - Update model list from ModelScope"
	@echo "  format     - Format code with black"
	@echo "  lint       - Lint code with flake8"
	@echo "  clean      - Clean up generated files"
	@echo "  status     - Show project status"
	@echo ""

install:
	pip install -r requirements.txt
	@echo "✓ Dependencies installed"

test:
	python3 test_proxy.py

run:
	./ms-claude --serve

update:
	python3 update_models.py

format:
	black src/ tests/ *.py

lint:
	flake8 src/ tests/ --max-line-length=100

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	find . -type f -name "*.pyd" -delete
	rm -rf .pytest_cache .coverage htmlcov
	@echo "✓ Cleaned up generated files"

status:
	@echo "Project Status"
	@echo "=============="
	@echo "Python: $$(python3 --version)"
	@echo "Files: $$(find src -name '*.py' | wc -l) Python files"
	@echo "Lines: $$(find src -name '*.py' -exec cat {} \; | wc -l) lines of code"
