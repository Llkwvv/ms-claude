# ms-claude — 模型故障转移代理

轻量级 HTTP 代理，为 Claude Code / OpenAI 客户端提供**多模型自动故障转移**。当某个模型额度耗尽、限频或不可用时，自动按优先级切换到下一个可用模型。

## 特性

| 特性 | 说明 |
|------|------|
| **自动故障转移** | 单模型失败时，自动尝试下一个，最多重试 3 次 |
| **黑名单** | 配置中一键禁用问题模型 |
| **失败阈值** | 1 小时内连续失败 ≥5 次自动禁用该模型 |
| **指数退避冷却** | 失败模型进入冷却期（60s → 120s → 240s…），避免反复碰撞 |
| **流式兼容** | 支持 SSE 流式与非流式响应 |
| **热重载配置** | 修改 `config.yaml` 无需重启 |
| **Claude 集成** | 一键改写 Claude 配置，零成本接入 |

## 项目结构

```
ms-claude/
├── src/
│   ├── core/              # 代理核心（调度、故障转移）
│   ├── models/            # 模型管理（加载、分组、状态）
│   ├── server/            # HTTP 代理服务器
│   ├── services/          # 失败追踪、配额检查
│   ├── utils/             # 配置、日志
│   └── config/
│       └── config.yaml    # 主配置文件
├── scripts/               # 辅助脚本（模型抓取、测试）
├── tests/                   # pytest 测试套件
├── Dockerfile               # 容器化部署
├── docker-compose.yml       # Docker Compose 编排
├── install.sh               # 一键安装脚本
├── ms-claude                # 主入口脚本
├── Makefile                 # 常用命令快捷方式
├── requirements.txt         # 核心依赖
├── requirements-dev.txt     # 开发依赖
└── .env.example             # 环境变量模板
```

---

## 部署方式一：一键脚本（推荐）

```bash
curl -fsSL https://raw.githubusercontent.com/Llkwvv/ms-claude/main/install.sh | bash
```

脚本会交互式让你选择：
- **本地 Python 部署** — 自动创建虚拟环境、systemd 服务、快捷命令
- **Docker 部署** — 自动构建镜像、生成 docker-compose 配置

### 手动运行脚本

```bash
chmod +x install.sh

# 本地部署
./install.sh local

# Docker 部署
./install.sh docker
```

---

## 部署方式二：手动本地部署

### 1. 克隆项目

```bash
git clone https://github.com/Llkwvv/ms-claude.git
cd ms-claude
```

### 2. 安装依赖

```bash
# 基础环境
pip install -r requirements.txt

# 开发测试（可选）
pip install -r requirements-dev.txt
```

### 3. 配置 API Key

```bash
cp .env.example .env
# 编辑 .env，填入你的上游 API Key
export MS_CLAUDE_UPSTREAM_API_KEY="your-key-here"
```

### 4. 启动代理

```bash
# 方式 A：直接运行
python3 -m src.main --serve --host 127.0.0.1 --port 8080

# 方式 B：使用快捷脚本
chmod +x ms-claude
./ms-claude --serve

# 方式 C：使用 Makefile
make run
```

### 5. 连接 Claude Code（可选）

```bash
./ms-claude --connect --client-cmd claude
```

这会自动把 Claude 的 API 地址指向本地代理，然后拉起 Claude。

---

## 部署方式三：Docker 部署

### 1. 准备环境变量

```bash
cp .env.example .env
# 编辑 .env，设置 MS_CLAUDE_UPSTREAM_API_KEY
```

### 2. 启动

```bash
# 使用 docker-compose
docker compose up -d

# 或使用传统 docker-compose
docker-compose up -d
```

### 3. 查看状态

```bash
docker compose logs -f
curl http://localhost:8080/v1/models
```

### 4. 停止

```bash
docker compose down
```

### Makefile 快捷命令

```bash
make docker-build   # 构建镜像
make docker-up      # 启动容器
make docker-down    # 停止容器
```

---

## 配置说明

### 环境变量（优先级最高）

| 变量 | 必填 | 说明 |
|------|------|------|
| `MS_CLAUDE_UPSTREAM_API_KEY` | ✅ | 上游模型服务的 API Key |
| `MS_CLAUDE_UPSTREAM_BASE_URL` | ❌ | 上游地址，默认 ModelScope |
| `MS_CLAUDE_HOME` | ❌ | 应用主目录，隔离配置/缓存/日志 |
| `MS_CLAUDE_MODEL` | ❌ | 强制指定默认模型，跳过优先级列表 |
| `LOG_LEVEL` | ❌ | 日志级别，默认 INFO |

### 配置文件 `src/config/config.yaml`

```yaml
# 模型优先级（按顺序尝试）
model_priority:
  - deepseek-ai/DeepSeek-R1-0528
  - Qwen/Qwen3-235B-A22B-Instruct-2507
  - deepseek-ai/DeepSeek-V4-Pro

# 黑名单（直接跳过这些模型）
blacklist:
  - model: "某个坏模型"
    reason: "额度已用完"

# 代理监听地址
proxy:
  host: "127.0.0.1"
  port: 8080
  upstream_base_url: "https://api-inference.modelscope.cn"
  upstream_api_key: ""           # 留空，从环境变量读取
  upstream_api_key_env: "MS_CLAUDE_UPSTREAM_API_KEY"
  upstream_type: "openai"

# 失败阈值ailure_tracking:
  enabled: true
  failure_threshold: 5
  time_window: 3600              # 1 小时
```

### 模型分组

```yaml
model_groups:
  opus:    # 顶级模型组
    models:
      - Qwen/Qwen3-235B-A22B-Instruct-2507
      - deepseek-ai/DeepSeek-V4-Pro
  sonnet:  # 均衡模型组
    models:
      - deepseek-ai/DeepSeek-V4-Flash
      - ZhipuAI/GLM-5
```

Claude 客户端请求 `"model": "claude-sonnet"` 时，代理会自动映射到 `sonnet` 组内的模型。

---

## 常用命令

```bash
# 查看模型状态
./ms-claude --status

# 更新模型列表
./ms-claude --update

# 运行测试
make test

# 代码格式化
make format

# 代码检查
make lint
```

---

## 架构设计

```
┌────────────────────────────────────────────┐
│          Claude Code / OpenAI Client       │
│         (ANTHROPIC_BASE_URL=127.0.0.1:8080) │
└────────────────────────────────────────────┘
                     │
                     ▼
┌──────────────────────────────────────────────────┐
│              ProxyServer (HTTP Proxy)             │
│  ┌────────────┐  ┌──────────┐  ┌──────────────┐   │
│  │ 模型选择    │  │ 故障转移  │  │ 失败追踪    │   │
│  │ _select... │  │ 重试循环  │  │ 阈值/冷却   │   │
│  └────────────┘  └──────────┘  └──────────────┘   │
└──────────────────────────────────────────────────┘
                     │
        ┌────────────┼────────────┐
        ▼            ▼            ▼
   ┌────────┐  ┌────────┐  ┌────────┐
   │ Model A │  │ Model B │  │ Model C │
   │ 失败→切换│  │ 成功→返回│  │ 备用    │
   └────────┘  └────────┘  └────────┘
```

---

## 许可证

MIT
