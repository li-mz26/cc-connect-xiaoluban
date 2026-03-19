"""
Agent 管理器 - 多会话管理和块流式响应
"""
import threading
import queue
import time
import logging
from typing import Optional, Callable, Dict, Any, List
from dataclasses import dataclass, field

from plugin.config import Config
from plugin.session import ClaudeSession, Event


logger = logging.getLogger(__name__)


@dataclass
class ChunkBuffer:
    """块流式缓冲区"""
    content: str = ""
    last_sent_time: float = field(default_factory=time.time)
    last_sent_content: str = ""
    
    def should_flush(self, interval: float, max_chars: int) -> bool:
        """检查是否应该刷新"""
        if not self.content:
            return False
        
        elapsed = time.time() - self.last_sent_time
        new_content = self.content[len(self.last_sent_content):]
        
        if len(new_content) >= max_chars:
            return True
        
        if elapsed >= interval and new_content:
            return True
        
        return False
    
    def flush(self) -> str:
        """刷新并返回新内容"""
        new_content = self.content[len(self.last_sent_content):]
        self.last_sent_content = self.content
        self.last_sent_time = time.time()
        return new_content
    
    def append(self, text: str):
        self.content += text
    
    def reset(self):
        self.content = ""
        self.last_sent_content = ""
        self.last_sent_time = time.time()


EventHandler = Callable[[str, str, Any], None]


class AgentManager:
    """管理多个 Claude 会话"""
    
    def __init__(self, config: Config):
        self.config = config
        self.sessions: Dict[str, ClaudeSession] = {}
        self.handlers: Dict[str, EventHandler] = {}
        self._lock = threading.RLock()
        self._chunk_buffers: Dict[str, ChunkBuffer] = {}
        
        # 启动清理线程
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleanup_thread.start()
    
    def start_session(self, session_key: str, work_dir: str,
                      handler: EventHandler) -> bool:
        """启动会话"""
        with self._lock:
            # 检查现有会话
            if session_key in self.sessions:
                session = self.sessions[session_key]
                if session.alive():
                    self.handlers[session_key] = handler
                    return True
                else:
                    self._close_internal(session_key)
            
            # 创建新会话
            session = ClaudeSession(session_key, work_dir, self.config)
            if not session.start():
                return False
            
            self.sessions[session_key] = session
            self.handlers[session_key] = handler
            
            # 初始化块流式缓冲区
            if self.config.streaming_mode == "chunk":
                self._chunk_buffers[session_key] = ChunkBuffer()
            
            # 启动事件处理器
            threading.Thread(
                target=self._event_processor,
                args=(session_key,),
                daemon=True
            ).start()
            
            return True
    
    def send_message(self, session_key: str, content: str) -> bool:
        """发送消息"""
        with self._lock:
            session = self.sessions.get(session_key)
            if not session or not session.alive():
                return False
        return session.send(content)
    
    def respond_permission(self, session_key: str, allow: bool, message: str = "") -> bool:
        """响应权限请求"""
        with self._lock:
            session = self.sessions.get(session_key)
            if not session:
                return False
            
            pending = getattr(session, '_pending_permission', None)
            if not pending:
                return False
            
            request_id = pending['request_id']
            session._pending_permission = None
            
            # 重置块流式缓冲区
            if session_key in self._chunk_buffers:
                self._chunk_buffers[session_key].reset()
        
        return session.respond_permission(request_id, allow, message)
    
    def close_session(self, session_key: str):
        """关闭会话"""
        with self._lock:
            self._close_internal(session_key)
    
    def close_all(self):
        """关闭所有会话"""
        with self._lock:
            for key in list(self.sessions.keys()):
                self._close_internal(key)
    
    def _close_internal(self, session_key: str):
        """内部关闭"""
        session = self.sessions.pop(session_key, None)
        if session:
            session.close()
        self.handlers.pop(session_key, None)
        self._chunk_buffers.pop(session_key, None)
    
    def get_session_info(self, session_key: str) -> Optional[Dict]:
        """获取会话信息"""
        with self._lock:
            session = self.sessions.get(session_key)
            if not session:
                return None
            return {
                "session_key": session_key,
                "alive": session.alive(),
                "pid": session.process.pid if session.process else None,
                "model": self.config.model,
            }
    
    def _event_processor(self, session_key: str):
        """事件处理器（支持块流式）"""
        session = self.sessions.get(session_key)
        if not session:
            return
        
        event_queue = session.events()
        response_parts: List[str] = []
        chunk_buffer = self._chunk_buffers.get(session_key)
        streaming_mode = self.config.streaming_mode
        
        while session.alive():
            try:
                # 等待事件（带超时）
                timeout = 0.5 if (streaming_mode == "chunk" and chunk_buffer) else 1.0
                event = event_queue.get(timeout=timeout)
                
                # 处理事件
                if event.type == "error":
                    self._notify(session_key, "error", str(event.error))
                    break
                    
                elif event.type == "text":
                    response_parts.append(event.content)
                    
                    if streaming_mode == "chunk" and chunk_buffer:
                        chunk_buffer.append(event.content)
                        
                elif event.type == "thinking":
                    if streaming_mode == "chunk" and chunk_buffer:
                        chunk_buffer.append(f"\n💭 {event.content}\n")
                        
                elif event.type == "tool_use":
                    info = f"🔧 Tool: {event.tool_name}\n```\n{event.tool_input}\n```"
                    
                    if streaming_mode == "chunk" and chunk_buffer:
                        # 刷新当前内容
                        if chunk_buffer.content:
                            self._notify(session_key, "chunk", chunk_buffer.content,
                                       {"is_partial": True})
                        # 发送工具信息
                        self._notify(session_key, "tool", info, {})
                        chunk_buffer.reset()
                    else:
                        self._notify(session_key, "tool", info, {})
                        
                elif event.type == "permission_request":
                    session._pending_permission = {
                        "request_id": event.request_id,
                        "tool_name": event.tool_name
                    }
                    
                    msg = f"⛔ 请求执行: {event.tool_name}\n回复 'y' 允许，'n' 拒绝"
                    
                    if streaming_mode == "chunk" and chunk_buffer:
                        if chunk_buffer.content:
                            self._notify(session_key, "chunk", chunk_buffer.content,
                                       {"is_partial": True})
                    
                    self._notify(session_key, "permission", msg,
                               {"request_id": event.request_id})
                               
                elif event.type == "result":
                    full_response = event.content or "".join(response_parts)
                    if not full_response.strip():
                        full_response = "（无内容）"
                    
                    if event.session_id:
                        session.session_id = event.session_id
                    
                    if streaming_mode == "chunk" and chunk_buffer:
                        self._notify(session_key, "complete", full_response,
                                   {"is_partial": False})
                        chunk_buffer.reset()
                    else:
                        self._notify(session_key, "complete", full_response, {})
                    
                    response_parts = []
                    
                elif event.type == "system":
                    if event.session_id:
                        session.session_id = event.session_id
                        
            except queue.Empty:
                # 检查块流式刷新
                if streaming_mode == "chunk" and chunk_buffer:
                    if chunk_buffer.should_flush(self.config.chunk_interval,
                                                  self.config.chunk_max_chars):
                        self._notify(session_key, "chunk", chunk_buffer.content,
                                   {"is_partial": True})
                        chunk_buffer.flush()
                        
            except Exception as e:
                logger.error(f"处理器异常: {e}")
                self._notify(session_key, "error", str(e))
                break
    
    def _notify(self, session_key: str, event_type: str, content: str, metadata: Any = None):
        """通知处理器"""
        handler = self.handlers.get(session_key)
        if handler:
            try:
                handler(event_type, content, metadata or {})
            except Exception as e:
                logger.error(f"通知失败: {e}")
    
    def _cleanup_loop(self):
        """清理循环"""
        while True:
            time.sleep(60)
            with self._lock:
                dead = [k for k, s in self.sessions.items() if not s.alive()]
                for key in dead:
                    logger.info(f"清理会话: {key}")
                    self._close_internal(key)
