"""
插件处理器 - 集成到 cc-connect
"""
import logging
from typing import Optional

# 导入 cc-connect 的 API（假设这些存在）
try:
    from util.api.by_token.send_msg import send_msg
    from util.api.by_token.api import recv_next_msg
    CC_CONNECT_AVAILABLE = True
except ImportError:
    CC_CONNECT_AVAILABLE = False
    # 定义存根函数用于测试
    def send_msg(text: str, receiver: str):
        print(f"[TO {receiver}] {text}")
    
    def recv_next_msg(msg):
        pass

from plugin.config import Config
from plugin.manager import AgentManager


logger = logging.getLogger(__name__)

# 用户状态存储
_user_states = {}


class ClaudePluginHandler:
    """Claude Code 插件处理器"""
    
    def __init__(self, config: Config):
        self.config = config
        self.manager = AgentManager(config)
        
        # 验证配置
        valid, errors = config.validate()
        if not valid:
            raise RuntimeError(f"配置错误: {errors}")
    
    def handle(self, msg, work_dir: Optional[str] = None) -> bool:
        """
        处理 IM 消息
        
        参数:
            msg: cc-connect 消息对象
            work_dir: 工作目录
        """
        session_key = getattr(msg, 'receiver', 'default')
        
        # 首次输入
        if getattr(msg, 'is_first_input', lambda: False)():
            return self._handle_first(session_key, msg, work_dir)
        
        # 后续输入
        return self._handle_follow(session_key, msg)
    
    def _handle_first(self, session_key: str, msg, work_dir: Optional[str]) -> bool:
        """处理首次输入"""
        
        def on_event(event_type: str, content: str, metadata: dict):
            receiver = getattr(msg, 'receiver', 'default')
            
            if event_type == "error":
                send_msg(f'❌ 错误: {content}', receiver)
                
            elif event_type == "tool":
                send_msg(content, receiver)
                
            elif event_type == "permission":
                _user_states[session_key] = {
                    "state": "awaiting_permission",
                    "request_id": metadata.get("request_id")
                }
                send_msg(content, receiver)
                
            elif event_type == "chunk":
                # 块流式响应
                is_partial = metadata.get("is_partial", True)
                if is_partial:
                    send_msg(f'{content}\n\n_生成中..._', receiver)
                else:
                    send_msg(content, receiver)
                    
            elif event_type == "complete":
                _user_states.pop(session_key, None)
                # 如果块流式已经发送过，这里不重复发送
                if not metadata.get("is_partial"):
                    send_msg(content, receiver)
        
        try:
            send_msg('🚀 启动 Claude Code...', msg.receiver)
            
            success = self.manager.start_session(
                session_key=session_key,
                work_dir=work_dir or self.config.work_dir or "/tmp",
                handler=on_event
            )
            
            if not success:
                send_msg('❌ 启动失败', msg.receiver)
                return False
            
            send_msg('✅ 已启动，处理中...', msg.receiver)
            self.manager.send_message(session_key, msg.params)
            
            recv_next_msg(msg)
            return True
            
        except Exception as e:
            logger.exception("处理失败")
            send_msg(f'错误: {e}', msg.receiver)
            return False
    
    def _handle_follow(self, session_key: str, msg) -> bool:
        """处理后续输入"""
        
        # 检查权限响应
        state = _user_states.get(session_key, {})
        if state.get("state") == "awaiting_permission":
            lower = msg.params.lower().strip()
            if lower in ['y', 'yes', '允许', '同意', '好']:
                self.manager.respond_permission(session_key, True)
                send_msg("✅ 已允许", msg.receiver)
            else:
                self.manager.respond_permission(session_key, False)
                send_msg("❌ 已拒绝", msg.receiver)
            
            _user_states.pop(session_key, None)
            recv_next_msg(msg)
            return True
        
        # 处理命令
        if msg.params.startswith('/'):
            return self._handle_command(session_key, msg)
        
        # 普通消息
        success = self.manager.send_message(session_key, msg.params)
        if success:
            recv_next_msg(msg)
        else:
            send_msg("❌ 会话断开，请重启", msg.receiver)
        
        return success
    
    def _handle_command(self, session_key: str, msg) -> bool:
        """处理命令"""
        cmd = msg.params.strip()
        
        if cmd == '/new':
            self.manager.close_session(session_key)
            send_msg("🆕 已关闭，输入消息开始新会话", msg.receiver)
            
        elif cmd == '/status':
            info = self.manager.get_session_info(session_key)
            if info:
                text = f"状态: {'运行中' if info['alive'] else '已停止'}\nPID: {info.get('pid', 'N/A')}"
            else:
                text = "无活跃会话"
            send_msg(text, msg.receiver)
            
        elif cmd == '/close':
            self.manager.close_session(session_key)
            send_msg("👋 已关闭", msg.receiver)
            
        elif cmd == '/help':
            send_msg(
                "/new - 新会话\n"
                "/status - 状态\n"
                "/close - 关闭\n"
                "/help - 帮助\n\n"
                "工具请求时回复 'y' 或 'n'",
                msg.receiver
            )
        else:
            send_msg(f"未知命令: {cmd}", msg.receiver)
        
        recv_next_msg(msg)
        return True
    
    def close_all(self):
        """关闭所有会话"""
        self.manager.close_all()
