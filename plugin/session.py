"""
Claude Code 会话管理核心
"""
import subprocess
import json
import threading
import queue
import logging
from typing import Optional, Dict, Any, List
from dataclasses import dataclass

from plugin.config import Config


logger = logging.getLogger(__name__)


@dataclass
class Event:
    """Claude 事件"""
    type: str  # text, thinking, tool_use, permission_request, result, error, system
    content: str = ""
    tool_name: str = ""
    tool_input: str = ""
    request_id: str = ""
    session_id: str = ""
    done: bool = False
    error: Optional[Exception] = None


class ClaudeSession:
    """单个 Claude Code 会话"""
    
    def __init__(self, session_key: str, work_dir: str, config: Config):
        self.session_key = session_key
        self.work_dir = work_dir
        self.config = config
        self.session_id = ""
        
        self.process: Optional[subprocess.Popen] = None
        self.events_queue: queue.Queue = queue.Queue()
        self._alive = False
        self._reader_thread: Optional[threading.Thread] = None
        self._pending_permission: Optional[Dict] = None
        
    def start(self) -> bool:
        """启动 Claude 进程"""
        args = [
            self.config.cli_path,
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--permission-mode", self.config.permission_mode,
            "--verbose",
        ]
        
        if self.config.model:
            args.extend(["--model", self.config.model])
        
        work_dir = self.work_dir or self.config.work_dir or "/tmp"
        
        logger.info(f"启动 Claude: {self.session_key}")
        
        try:
            self.process = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=1,
                text=True,
                cwd=work_dir,
                env=self.config.get_env()
            )
            self._alive = True
            self._reader_thread = threading.Thread(
                target=self._read_loop,
                daemon=True,
                name=f"Reader-{self.session_key}"
            )
            self._reader_thread.start()
            
            logger.info(f"Claude 已启动: PID={self.process.pid}")
            return True
            
        except Exception as e:
            logger.error(f"启动失败: {e}")
            self.events_queue.put(Event(type="error", error=e))
            return False
    
    def _read_loop(self):
        """后台读取线程"""
        try:
            for line in self.process.stdout:
                if not line.strip():
                    continue
                
                try:
                    raw = json.loads(line)
                    event = self._parse_event(raw)
                    self.events_queue.put(event)
                except json.JSONDecodeError:
                    continue
                    
        except Exception as e:
            logger.error(f"读取异常: {e}")
        finally:
            self._alive = False
            exit_code = self.process.poll()
            logger.info(f"Claude 结束: exit_code={exit_code}")
            self.events_queue.put(Event(
                type="error",
                error=Exception(f"Process exited: {exit_code}")
            ))
    
    def _parse_event(self, raw: Dict) -> Event:
        """解析 Claude 事件"""
        event_type = raw.get("type", "")
        
        if event_type == "assistant":
            msg = raw.get("message", {})
            contents = msg.get("content", [])
            texts = []
            
            for c in contents:
                if c.get("type") == "text":
                    texts.append(c.get("text", ""))
                elif c.get("type") == "tool_use":
                    return Event(
                        type="tool_use",
                        tool_name=c.get("name", ""),
                        tool_input=json.dumps(c.get("input", {}), ensure_ascii=False)
                    )
            
            return Event(type="text", content="".join(texts))
            
        elif event_type == "thinking":
            return Event(type="thinking", content=raw.get("thinking", ""))
            
        elif event_type == "control_request":
            req = raw.get("request", {})
            return Event(
                type="permission_request",
                request_id=raw.get("request_id", ""),
                tool_name=req.get("tool_name", ""),
                tool_input=json.dumps(req.get("input", {}), ensure_ascii=False)
            )
            
        elif event_type == "result":
            return Event(
                type="result",
                content=raw.get("result", ""),
                session_id=raw.get("session_id", ""),
                done=True
            )
            
        elif event_type == "system":
            return Event(type="system", session_id=raw.get("session_id", ""))
            
        return Event(type="unknown")
    
    def send(self, content: str) -> bool:
        """发送消息"""
        if not self._alive or not self.process:
            return False
        
        try:
            msg = {
                "type": "user",
                "message": {"role": "user", "content": content}
            }
            data = json.dumps(msg, ensure_ascii=False) + "\n"
            self.process.stdin.write(data)
            self.process.stdin.flush()
            return True
        except Exception as e:
            logger.error(f"发送失败: {e}")
            return False
    
    def respond_permission(self, request_id: str, allow: bool, message: str = "") -> bool:
        """响应权限请求"""
        if not self._alive:
            return False
        
        try:
            if allow:
                response = {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": request_id,
                        "response": {"behavior": "allow", "updatedInput": {}}
                    }
                }
            else:
                response = {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": request_id,
                        "response": {
                            "behavior": "deny",
                            "message": message or "User denied."
                        }
                    }
                }
            
            data = json.dumps(response, ensure_ascii=False) + "\n"
            self.process.stdin.write(data)
            self.process.stdin.flush()
            return True
        except Exception as e:
            logger.error(f"权限响应失败: {e}")
            return False
    
    def events(self) -> queue.Queue:
        return self.events_queue
    
    def alive(self) -> bool:
        if not self._alive:
            return False
        if self.process and self.process.poll() is not None:
            self._alive = False
            return False
        return True
    
    def close(self):
        """关闭会话"""
        logger.info(f"关闭会话: {self.session_key}")
        self._alive = False
        
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except:
                self.process.kill()
