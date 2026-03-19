"""
Claude Code 会话代理 - 插件入口

使用方法：
1. 将此目录复制到 cc-connect 的 plugins 目录
2. 修改 config.yaml 填入 API Key
3. 重启 cc-connect

配置方式（优先级从高到低）：
1. 环境变量: LLM_API_KEY, LLM_API_BASE, LLM_MODEL
2. 配置文件: config.yaml
3. 默认值
"""

from plugin.claude_handler import ClaudePluginHandler
from plugin.config import get_config

# 全局处理器实例
_handler = None

def handle(msg):
    """
    cc-connect 插件入口
    
    此函数会被 cc-connect 调用处理消息
    """
    global _handler
    
    if _handler is None:
        config = get_config()
        _handler = ClaudePluginHandler(config)
    
    # 工作目录 - 可以改为从消息或配置中获取
    work_dir = _handler.config.work_dir or "/root/.openclaw/workspace"
    
    return _handler.handle(msg, work_dir)


# 可选：程序退出时清理
def on_exit():
    global _handler
    if _handler:
        _handler.close_all()
