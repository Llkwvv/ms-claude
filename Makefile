# Makefile for ms-claude

.PHONY: help install install-dev test run update clean format lint status docker-build docker-up docker-down

# 默认目标
help:
	@echo "ms-claude - Model Failover Proxy"
	@echo ""
	@echo "Local targets:"
	@echo "  install      - Install production dependencies"
	@echo "  install-dev  - Install + dev dependencies (pytest, black, flake8)"
	@echo "  test         - Run pytest test suite"
	@echo "  run          - Run the proxy server"
	@echo "  update       - Update model list from upstream"
	@echo "  format       - Format code with black"
	@echo "  lint         - Lint code with flake8"
	@echo "  clean        - Clean up generated files"
	@echo "  status       - Show project status"
	@echo ""
	@echo "Docker targets:"
	@echo "  docker-build - Build Docker image"
	@echo "  docker-up    - Start with docker-compose"
	@echo "  docker-down  - Stop docker-compose"
	@echo ""

install:
	pip install -r requirements.txt
	@echo "✓ Dependencies installed"

install-dev:
	pip install -r requirements.txt
	pip install -r requirements-dev.txt
	@echo "✓ Dev dependencies installed"

test:
	python3 -m pytest tests/ -v --tb=short

run:
	./ms-claude --serve

update:
	python3 scripts/update_models.py

format:
	black src/ tests/ scripts/*.py

lint:
	flake8 src/ tests/ --max-line-length=100

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	find . -type f -name "*.pyd" -delete
	rm -rf .coverage htmlcov
	@echo "✓ Cleaned up generated files"

status:
	@echo "Project Status"
	@echo "=============="
	@echo "Python: $$(python3 --version)"
	@echo "Files: $$(find src -name '*.py' | wc -l) Python files"
	@echo "Lines: $$(find src -name '*.py' -exec cat {} \; | wc -l) lines of code"

# Docker targets
docker-build:
	docker build -t ms-claude:latest .

docker-up:
	docker-compose up -d

docker-down:
	docker-compose down
