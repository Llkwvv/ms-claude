# 快速开始指南

## 安装

### 1. 安装依赖

```bash
cd ms-claude-proxy
pip install -r requirements.txt
```

### 2. 配置环境

复制示例环境文件：

```bash
cp .env.example .env
```

根据需要编辑 `.env` 文件：

```bash
# 日志级别
LOG_LEVEL=INFO

# 配置文件路径
CONFIG_PATH=src/config/config.yaml
```

## 使用

### 方式一：命令行

```bash
# 显示帮助
./ms-claude --help

# 启动本地代理服务
export MS_CLAUDE_HOME="$HOME/.config/ms-claude"
./ms-claude --serve

# 一键修正 Claude 配置、启动代理并拉起 Claude 客户端
./ms-claude --connect --client-cmd claude

# 查看状态
./ms-claude --status
```

### 方式二：Python API

```python
from src.core.proxy import ModelProxy

# 初始化代理
proxy = ModelProxy()

# 发送请求
response = proxy.request("写一个快速排序算法", stream=False)
print(response)

# 流式请求
for chunk in proxy.request("写一个故事", stream=True):
    print(chunk, end="")
```

### 方式三：作为模块导入

```python
from src.models.manager import ModelManager
from src.utils.config import Config

# 加载配置
config = Config()

# 初始化管理器
manager = ModelManager(config)

# 获取模型
models = manager.get_available_models()
for model in models:
    print(f"{model.name}: {model.description}")
```

## 配置

编辑 `src/config/config.yaml` 文件：

```yaml
# 模型优先级（按顺序优先使用）
model_priority:
  - qwen-max
  - qwen-plus
  - deepseek-coder-v2
  - yi-large
  - qwen-turbo

# ModelScope API配置
modelscope:
  api_base: "https://modelscope.cn/api/v1"
  update_schedule: "0 2 * * *"  # 每天2点更新
  cache_ttl: 86400  # 缓存24小时

# 额度配置
quota:
  error_codes:
    - "quota_exceeded"
    - "rate_limit_exceeded"
    - "insufficient_quota"
  max_retries: 3
  retry_delay: 1

# 失败跟踪
failure_tracking:
  enabled: true
  log_file: "logs/failures.jsonl"
  failure_threshold: 5
  time_window: 3600

proxy:
  host: "127.0.0.1"
  port: 8080
  upstream_base_url: "https://your-openai-compatible-endpoint"
  upstream_api_key_env: "OPENAI_API_KEY"
  upstream_type: "openai"
```

## 功能演示

### 1. 查看状态

```bash
./ms-claude --status
```

输出示例：

```
============================================================
Model Proxy Status
============================================================

Total models: 5
Available models: 5

Model Details:
------------------------------------------------------------
  ✓ qwen-max                   (priority: 0, status: available)
  ✓ qwen-plus                  (priority: 1, status: available)
  ✓ deepseek-coder-v2          (priority: 2, status: available)
  ✓ yi-large                   (priority: 3, status: available)
  ✓ qwen-turbo                 (priority: 4, status: available)
```

### 2. 更新模型列表

```bash
./ms-claude --update
```

### 3. 运行测试

```bash
./ms-claude --test
```

## 集成到项目中

### 独立启动

直接使用 `ms-claude` 启动服务。

```bash
export MS_CLAUDE_HOME="$HOME/.config/ms-claude"
./ms-claude --serve
```

如果想连同 Claude 客户端一起启动，使用：

```bash
./ms-claude --connect --client-cmd claude
```

它会自动把 `~/.claude/settings.json` 指向本地 `8080`。

### 自定义请求处理

```python
from src.core.proxy import ModelProxy

proxy = ModelProxy()

# 包装你的请求
def chat_with_model(messages):
    prompt = "\n".join([m["content"] for m in messages])
    response = proxy.request(prompt, stream=False)
    return response.get("content", "")

# 使用
reply = chat_with_model([
    {"role": "user", "content": "你好"}
])
```

## 故障排除

### 问题：模型列表为空

**解决方案**：
1. 运行更新命令：`./ms-claude --update`
2. 检查网络连接
3. 验证配置文件

### 问题：请求频繁失败

**解决方案**：
1. 查看失败日志：`cat logs/failures.jsonl`
2. 检查额度状态
3. 调整模型优先级

### 问题：流式响应不工作

**解决方案**：
1. 确认配置中启用流式：`streaming.enabled: true`
2. 检查模型是否支持流式
3. 增加超时时间

## 下一步

- 阅读 [架构文档](ARCHITECTURE.md)
- 查看 [API 参考](API.md)
- 贡献代码：阅读 [CONTRIBUTING.md](../CONTRIBUTING.md)
