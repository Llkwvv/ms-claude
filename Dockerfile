# ModelScope Claude Code Proxy - Docker 部署
FROM python:3.12-slim

WORKDIR /app

# 安装系统依赖（playwright 需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    libatspi2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖
COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt

# 安装 playwright（可选，用于模型抓取）
RUN pip install --no-cache-dir playwright && playwright install chromium

# 复制代码
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY ms-claude ./

# 创建数据/日志目录
RUN mkdir -p /app/data /app/logs

# 暴露端口
EXPOSE 8080

# 默认环境变量（部署时覆盖）
ENV MS_CLAUDE_HOME=/app \
    PYTHONUNBUFFERED=1

# 启动代理
ENTRYPOINT ["python3", "-m", "src.main"]
CMD ["--serve", "--host", "0.0.0.0", "--port", "8080"]
