# ModelScope Claude Code Proxy

轻量级模型代理项目，用于自动切换和调度多个模型

## 特性

- **自动切换**: 按优先级顺序自动选择可用模型
- **额度感知**: 检测额度不足/限频/失败情况，自动切换到下一个模型
- **失败记录**: 记录解析失败情况，便于后续优化
- **流式兼容**: 同时支持流式和非流式响应
- **自动更新**: 每日首次启动时从 ModelScope 拉取最新模型列表
- **优先级调度**: 按配置文件中的列表顺序决定优先级

## 项目结构

```
ms-claude-proxy/
├── src/
│   ├── core/           # 核心模块
│   │   ├── __init__.py
│   │   ├── proxy.py    # 主代理类
│   │   └── scheduler.py # 优先级调度器
│   ├── models/         # 模型管理
│   │   ├── __init__.py
│   │   ├── manager.py  # 模型列表管理
│   │   └── model.py    # 模型实体
│   ├── services/       # 服务层
│   │   ├── __init__.py
│   │   ├── modelscope.py # ModelScope API 服务
│   │   └── failure.py  # 失败记录服务
│   ├── utils/          # 工具类
│   │   ├── __init__.py
│   │   ├── config.py   # 配置管理
│   │   └── logger.py   # 日志工具
│   └── config/         # 配置文件
│       └── config.yaml
├── tests/              # 测试
├── logs/               # 日志目录
└── docs/               # 文档
```

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 如果要跑测试/格式化，再装开发依赖
pip install -r requirements-dev.txt

# 安装浏览器内核（fetch_text_generation_models.py 需要）
playwright install chromium

# 启动本地代理服务
export MS_CLAUDE_HOME="$HOME/.config/ms-claude"
./ms-claude --serve

# 一键修正 Claude 配置、启动代理并拉起 Claude 客户端
./ms-claude --connect --client-cmd claude
```

## 配置

编辑 `src/config/config.yaml` 配置模型优先级和其他选项。

如果要独立运行，建议给本项目单独一个 home 目录，例如：

```bash
export MS_CLAUDE_HOME="$HOME/.config/ms-claude"
./ms-claude --status
```

这样它会把配置、缓存和日志放到独立目录。

`ms-claude` 会把 Claude settings 放到 `<MS_CLAUDE_HOME>/.claude/settings.json`，不会再默认修改全局 `~/.claude/settings.json`。

模型列表默认通过 [`fetch_text_generation_models.py`](../fetch_text_generation_models.py) 获取；如果要替换抓取脚本，可以设置 `MS_CLAUDE_MODEL_FETCHER` 或 `modelscope.fetch_script_path`。

如果你要让 Claude 直接走这个代理，还需要在配置里指定上游模型服务：

```yaml
proxy:
  upstream_base_url: "https://your-openai-compatible-endpoint"
  upstream_api_key_env: "OPENAI_API_KEY"
  upstream_type: "openai"
```

如果你不想手动改 Claude 配置，可以直接用：

```bash
./ms-claude --connect --client-cmd claude
```

它会先把 `~/.claude/settings.json` 里的 `ANTHROPIC_BASE_URL` 改成 `http://127.0.0.1:8080`，再启动本地代理和 Claude。
如果设置了 `MS_CLAUDE_HOME`，则会改写该 home 下的 `.claude/settings.json`。

## 架构设计

```
┌─────────────────────────────────────────────────────────┐
│                    Claude Code                           │
└─────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────┐
│               Model Proxy（代理层）                        │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐      │
│  │ 模型列表管理 │  │ 优先级调度  │  │ 失败记录    │      │
│  │ 每日自动更新 │  │ 额度感知切换 │  │ 规则优化    │      │
│  └─────────────┘  └─────────────┘  └─────────────┘      │
└─────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────┐
│                    ModelScope API                        │
│   Qwen │ DeepSeek │ Yi │ ... (50+ 模型)                  │
└─────────────────────────────────────────────────────────┘
```
