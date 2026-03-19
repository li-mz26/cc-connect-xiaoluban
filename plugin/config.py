"""
配置管理
支持多 LLM 提供商（Anthropic 协议兼容）
"""
import os
import yaml
from pathlib import Path
from typing import Optional, Dict, Any
from dataclasses import dataclass, field


@dataclass
class Config:
    """插件配置"""
    # LLM 配置
    api_key: str = ""
    api_base: str = "https://api.anthropic.com"
    model: str = "claude-sonnet-4-20250514"
    
    # Claude Code CLI 配置
    cli_path: str = "claude"
    permission_mode: str = "acceptEdits"  # default, acceptEdits, bypassPermissions
    work_dir: Optional[str] = None
    session_timeout: int = 3600
    
    # 流式响应配置
    streaming_mode: str = "chunk"  # none, chunk
    chunk_interval: float = 0.5    # 块流式最小间隔（秒）
    chunk_max_chars: int = 500     # 块流式最大字符数
    
    @classmethod
    def load(cls) -> "Config":
        """加载配置"""
        config = cls()
        
        # 1. 从配置文件加载
        config_path = cls._find_config_file()
        if config_path:
            with open(config_path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
            
            if 'llm' in data:
                llm = data['llm']
                config.api_key = llm.get('api_key', config.api_key)
                config.api_base = llm.get('api_base', config.api_base)
                config.model = llm.get('model', config.model)
            
            if 'claude_code' in data:
                cc = data['claude_code']
                config.cli_path = cc.get('cli_path', config.cli_path)
                config.permission_mode = cc.get('permission_mode', config.permission_mode)
                config.work_dir = cc.get('work_dir', config.work_dir)
                config.session_timeout = cc.get('session_timeout', config.session_timeout)
            
            if 'streaming' in data:
                st = data['streaming']
                config.streaming_mode = st.get('mode', config.streaming_mode)
                config.chunk_interval = st.get('chunk_interval', config.chunk_interval)
                config.chunk_max_chars = st.get('chunk_max_chars', config.chunk_max_chars)
        
        # 2. 环境变量覆盖（优先级最高）
        config._load_from_env()
        
        # 展开路径
        if config.work_dir:
            config.work_dir = os.path.expanduser(config.work_dir)
        
        return config
    
    @classmethod
    def _find_config_file(cls) -> Optional[Path]:
        """查找配置文件"""
        # 先查找插件目录下的 config.yaml
        plugin_dir = Path(__file__).parent.parent
        local_config = plugin_dir / "config.yaml"
        if local_config.exists():
            return local_config
        
        # 再查找用户目录
        user_config = Path.home() / ".cc-connect-xiaoluban" / "config.yaml"
        if user_config.exists():
            return user_config
        
        return None
    
    def _load_from_env(self):
        """从环境变量加载"""
        if os.getenv('LLM_API_KEY'):
            self.api_key = os.getenv('LLM_API_KEY')
        if os.getenv('LLM_API_BASE'):
            self.api_base = os.getenv('LLM_API_BASE')
        if os.getenv('LLM_MODEL'):
            self.model = os.getenv('LLM_MODEL')
        if os.getenv('CLAUDE_CLI_PATH'):
            self.cli_path = os.getenv('CLAUDE_CLI_PATH')
        if os.getenv('CLAUDE_PERMISSION_MODE'):
            self.permission_mode = os.getenv('CLAUDE_PERMISSION_MODE')
        if os.getenv('CLAUDE_WORK_DIR'):
            self.work_dir = os.getenv('CLAUDE_WORK_DIR')
    
    def validate(self) -> tuple[bool, list[str]]:
        """验证配置"""
        errors = []
        
        if not self.api_key:
            errors.append("缺少 API Key，请设置 llm.api_key 或环境变量 LLM_API_KEY")
        
        import shutil
        if not shutil.which(self.cli_path):
            errors.append(f"找不到 Claude CLI: {self.cli_path}")
        
        valid_modes = ["default", "acceptEdits", "bypassPermissions", "yolo"]
        if self.permission_mode not in valid_modes:
            errors.append(f"无效权限模式: {self.permission_mode}")
        
        return len(errors) == 0, errors
    
    def get_env(self) -> Dict[str, str]:
        """获取 Claude CLI 环境变量"""
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)  # 防止嵌套检测
        
        if self.api_key:
            env['ANTHROPIC_API_KEY'] = self.api_key
        if self.api_base:
            env['ANTHROPIC_API_BASE'] = self.api_base
        
        return env


# 全局配置实例
_config: Optional[Config] = None


def get_config() -> Config:
    """获取全局配置"""
    global _config
    if _config is None:
        _config = Config.load()
    return _config
