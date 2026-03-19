# cc-connect-xiaoluban

Claude Code 会话代理插件，用于 cc-connect 或其他 IM 插件框架。

## 特点

- **即插即用** - 直接复制到 plugins 目录即可使用，无需 pip 安装
- **多提供商支持** - 支持 Anthropic 及兼容 API（OpenRouter、自定义代理等）
- **块流式响应** - 每个思考步骤返回一次消息，平衡实时性和稳定性
- **常驻会话** - 进程保持运行，避免重复启动开销
- **权限控制** - 支持手动/自动处理工具调用权限

## 安装

```bash
# 1. 克隆到 cc-connect 的 plugins 目录
cd /path/to/cc-connect/plugins
git clone git@github.com:li-mz26/cc-connect-xiaoluban.git

# 2. 安装依赖（只需要 PyYAML）
pip install pyyaml

# 3. 配置 API Key
vim cc-connect-xiaoluban/config.yaml

# 4. 重启 cc-connect
```

## 配置

编辑 `config.yaml`：

```yaml
llm:
  api_key: "sk-ant-xxxxx"           # 必填
  api_base: "https://api.anthropic.com"  # 支持其他兼容端点
  model: "claude-sonnet-4-20250514"

claude_code:
  work_dir: "/path/to/project"      # 工作目录
  permission_mode: "acceptEdits"    # 自动允许文件编辑
```

或使用环境变量（优先级更高）：

```bash
export LLM_API_KEY="sk-ant-xxxxx"
export LLM_API_BASE="https://api.anthropic.com"
export LLM_MODEL="claude-sonnet-4-20250514"
```

## 项目结构

```
cc-connect-xiaoluban/
├── plugin.py              # 入口点，cc-connect 调用此文件
├── config.yaml            # 配置文件
├── plugin/
│   ├── __init__.py
│   ├── config.py          # 配置管理
│   ├── session.py         # Claude 会话（进程管理）
│   ├── manager.py         # 多会话管理 + 块流式
│   └── claude_handler.py  # 插件集成层
└── README.md
```

## 使用方法

启动对话：
```
用户: 你好
Bot: 🚀 启动 Claude Code...
Bot: ✅ 已启动，处理中...
Bot: 你好！有什么我可以帮助你的？
```

工具调用：
```
用户: 列出当前目录
Bot: 🔧 Tool: bash
     ```
     {"command": "ls -la"}
     ```
Bot: （等待用户输入 y 或 n）
用户: y
Bot: 当前目录包含以下文件...
```

命令：
- `/new` - 开始新会话
- `/status` - 查看会话状态
- `/close` - 关闭会话
- `/help` - 帮助

## 块流式响应

当 `streaming.mode: chunk` 时，消息会分块返回：

```
User: 写一个快速排序
Bot: 我来实现一个快速排序算法

_生成中..._

Bot: 我来实现一个快速排序算法。

首先选择基准值...

_生成中..._

Bot: 我来实现一个快速排序算法。

首先选择基准值。然后分区...

_生成中..._

Bot: （完整代码）
```

这样可以及时反馈进度，同时避免消息刷屏。

## 支持的 API 提供商

任何兼容 Anthropic API 协议的提供商：

- **Anthropic** (官方): `https://api.anthropic.com`
- **OpenRouter**: `https://openrouter.ai/api/v1`
- **自定义代理**: 你自己的中转服务

## License

MIT
