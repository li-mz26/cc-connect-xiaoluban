import json
import os
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from util.msg import Msg
from util.api.by_token.send_msg import send_msg
from util.api.by_token.api import recv_next_msg


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
SESSIONS_PATH = BASE_DIR / "sessions.json"
TRANSCRIPT_DIR = BASE_DIR / "transcripts"


@dataclass
class SessionMeta:
    owner: str
    name: str
    workdir: str
    command: List[str]
    created_at: float
    updated_at: float
    status: str  # running / stopped
    pid: Optional[int] = None


@dataclass
class RuntimeSession:
    meta: SessionMeta
    process: subprocess.Popen
    stdin_lock: threading.Lock
    alive: bool = True


SESSIONS_LOCK = threading.Lock()
SESSIONS: Dict[Tuple[str, str], RuntimeSession] = {}
ACTIVE_SESSION: Dict[str, str] = {}


def _load_config() -> dict:
    default = {
        "claude_command": "claude code",
        "max_sessions_per_user": 10,
        "transcript_context_lines": 40,
    }
    if not CONFIG_PATH.exists():
        return default

    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            loaded = json.load(f)
            if isinstance(loaded, dict):
                default.update(loaded)
    except Exception:
        pass
    return default


CONFIG = _load_config()


def _load_all_meta() -> Dict[Tuple[str, str], SessionMeta]:
    if not SESSIONS_PATH.exists():
        return {}
    try:
        with SESSIONS_PATH.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        result = {}
        for item in raw:
            m = SessionMeta(**item)
            result[(m.owner, m.name)] = m
        return result
    except Exception:
        return {}


def _save_all_meta(meta_map: Dict[Tuple[str, str], SessionMeta]) -> None:
    SESSIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = [asdict(v) for v in meta_map.values()]
    with SESSIONS_PATH.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _upsert_meta(meta: SessionMeta) -> None:
    all_meta = _load_all_meta()
    all_meta[(meta.owner, meta.name)] = meta
    _save_all_meta(all_meta)


def _remove_meta(owner: str, name: str) -> None:
    all_meta = _load_all_meta()
    all_meta.pop((owner, name), None)
    _save_all_meta(all_meta)


def _get_meta(owner: str, name: str) -> Optional[SessionMeta]:
    return _load_all_meta().get((owner, name))


def _list_user_meta(owner: str) -> List[SessionMeta]:
    return [m for (o, _), m in _load_all_meta().items() if o == owner]


def _normalize_workdir(raw: str) -> str:
    p = Path(raw).expanduser().resolve()
    if not p.exists() or not p.is_dir():
        raise ValueError(f"工作目录不存在: {p}")
    return str(p)


def _transcript_file(owner: str, name: str) -> Path:
    safe_owner = owner.replace("/", "_")
    safe_name = name.replace("/", "_")
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    return TRANSCRIPT_DIR / f"{safe_owner}__{safe_name}.log"


def _append_transcript(owner: str, name: str, role: str, text: str) -> None:
    fp = _transcript_file(owner, name)
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    with fp.open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] [{role}] {text}\n")


def _load_recent_transcript(owner: str, name: str, max_lines: int) -> str:
    fp = _transcript_file(owner, name)
    if not fp.exists():
        return ""
    with fp.open("r", encoding="utf-8") as f:
        lines = f.readlines()
    return "".join(lines[-max_lines:]).strip()


def _reader_loop(rt: RuntimeSession) -> None:
    owner = rt.meta.owner
    name = rt.meta.name
    receiver = owner
    cache: List[str] = []
    last_flush = time.time()

    try:
        while rt.alive and rt.process.poll() is None:
            line = rt.process.stdout.readline()
            if not line:
                break

            line = line.rstrip("\n")
            if line:
                cache.append(line)

            now = time.time()
            if cache and (len(cache) >= 6 or now - last_flush > 0.8):
                text = "\n".join(cache)
                send_msg(f"[{name}]\n{text}", receiver)
                _append_transcript(owner, name, "AI", text)
                cache.clear()
                last_flush = now

        if cache:
            text = "\n".join(cache)
            send_msg(f"[{name}]\n{text}", receiver)
            _append_transcript(owner, name, "AI", text)

        code = rt.process.poll()
        send_msg(f"[{name}] Claude会话已结束，exit_code={code}", receiver)
    except Exception as e:
        send_msg(f"[{name}] 读取Claude输出异常: {e}", receiver)
    finally:
        with SESSIONS_LOCK:
            rt.alive = False
            key = (owner, name)
            cur = SESSIONS.get(key)
            if cur is rt:
                SESSIONS.pop(key, None)

        meta = _get_meta(owner, name)
        if meta:
            meta.status = "stopped"
            meta.pid = None
            meta.updated_at = time.time()
            _upsert_meta(meta)


def _start_runtime(owner: str, name: str, workdir: str, command: Optional[List[str]] = None) -> RuntimeSession:
    cmd = command or shlex.split(CONFIG["claude_command"])
    proc = subprocess.Popen(
        cmd,
        cwd=workdir,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    now = time.time()
    meta = SessionMeta(
        owner=owner,
        name=name,
        workdir=workdir,
        command=cmd,
        created_at=now,
        updated_at=now,
        status="running",
        pid=proc.pid,
    )
    _upsert_meta(meta)

    rt = RuntimeSession(meta=meta, process=proc, stdin_lock=threading.Lock())
    t = threading.Thread(target=_reader_loop, args=(rt,), daemon=True)
    t.start()
    return rt


def _stop_runtime(owner: str, name: str) -> bool:
    key = (owner, name)
    with SESSIONS_LOCK:
        rt = SESSIONS.get(key)
    if not rt:
        return False

    rt.alive = False
    try:
        rt.process.terminate()
        rt.process.wait(timeout=3)
    except Exception:
        try:
            rt.process.kill()
        except Exception:
            pass

    with SESSIONS_LOCK:
        if SESSIONS.get(key) is rt:
            SESSIONS.pop(key, None)

    meta = _get_meta(owner, name)
    if meta:
        meta.status = "stopped"
        meta.pid = None
        meta.updated_at = time.time()
        _upsert_meta(meta)

    return True


def _send_to_runtime(rt: RuntimeSession, text: str) -> None:
    if rt.process.poll() is not None:
        raise RuntimeError("Claude进程已退出")
    with rt.stdin_lock:
        rt.process.stdin.write(text + "\n")
        rt.process.stdin.flush()

    rt.meta.updated_at = time.time()
    _upsert_meta(rt.meta)
    _append_transcript(rt.meta.owner, rt.meta.name, "USER", text)


def _parse_command(text: str) -> Tuple[str, List[str]]:
    text = text.strip()
    if not text.startswith("/"):
        return "", []
    parts = text.split()
    cmd = parts[0].lower()
    args = parts[1:]
    return cmd, args


def _help_text() -> str:
    return (
        "Claude多会话插件命令：\n"
        "/start <会话名> <工作目录>  启动并切换\n"
        "/switch <会话名>             切换当前会话\n"
        "/resume <会话名>             恢复会话(必要时重启进程)\n"
        "/sessions                    查看会话列表\n"
        "/stop [会话名]               停止会话(默认当前会话)\n"
        "/pwd                         查看当前会话目录\n"
        "/help                        显示帮助"
    )


def handle(msg: Msg):
    owner = msg.receiver
    text = (msg.params or "").strip()

    if msg.is_first_input():
        send_msg(_help_text(), owner)
        recv_next_msg(msg)
        return

    cmd, args = _parse_command(text)

    if cmd in ("/help", "/?"):
        send_msg(_help_text(), owner)
        recv_next_msg(msg)
        return

    if cmd == "/start":
        if len(args) < 2:
            send_msg("用法: /start <会话名> <工作目录>", owner)
            recv_next_msg(msg)
            return

        name = args[0]
        workdir_raw = " ".join(args[1:])
        try:
            workdir = _normalize_workdir(workdir_raw)
        except Exception as e:
            send_msg(str(e), owner)
            recv_next_msg(msg)
            return

        user_metas = _list_user_meta(owner)
        if len(user_metas) >= int(CONFIG.get("max_sessions_per_user", 10)) and not _get_meta(owner, name):
            send_msg("会话数量已达上限，请先停止并删除旧会话。", owner)
            recv_next_msg(msg)
            return

        _stop_runtime(owner, name)
        rt = _start_runtime(owner, name, workdir)
        with SESSIONS_LOCK:
            SESSIONS[(owner, name)] = rt
            ACTIVE_SESSION[owner] = name

        send_msg(f"会话 {name} 已启动并切换，工作目录: {workdir}", owner)
        recv_next_msg(msg)
        return

    if cmd == "/switch":
        if len(args) != 1:
            send_msg("用法: /switch <会话名>", owner)
            recv_next_msg(msg)
            return
        name = args[0]
        if not _get_meta(owner, name):
            send_msg(f"会话 {name} 不存在", owner)
            recv_next_msg(msg)
            return
        with SESSIONS_LOCK:
            ACTIVE_SESSION[owner] = name
        send_msg(f"已切换到会话 {name}", owner)
        recv_next_msg(msg)
        return

    if cmd == "/resume":
        if len(args) != 1:
            send_msg("用法: /resume <会话名>", owner)
            recv_next_msg(msg)
            return
        name = args[0]
        key = (owner, name)

        with SESSIONS_LOCK:
            rt = SESSIONS.get(key)
        if rt and rt.alive and rt.process.poll() is None:
            with SESSIONS_LOCK:
                ACTIVE_SESSION[owner] = name
            send_msg(f"会话 {name} 正在运行，已切换。", owner)
            recv_next_msg(msg)
            return

        meta = _get_meta(owner, name)
        if not meta:
            send_msg(f"找不到会话 {name} 的历史记录", owner)
            recv_next_msg(msg)
            return

        rt = _start_runtime(owner, name, meta.workdir, meta.command)
        with SESSIONS_LOCK:
            SESSIONS[key] = rt
            ACTIVE_SESSION[owner] = name

        recent = _load_recent_transcript(owner, name, int(CONFIG.get("transcript_context_lines", 40)))
        if recent:
            bootstrap = "以下是本会话最近历史，请基于这些上下文继续：\n" + recent
            try:
                _send_to_runtime(rt, bootstrap)
            except Exception:
                pass

        send_msg(f"会话 {name} 已恢复并切换。", owner)
        recv_next_msg(msg)
        return

    if cmd == "/sessions":
        metas = sorted(_list_user_meta(owner), key=lambda x: x.updated_at, reverse=True)
        if not metas:
            send_msg("当前没有会话", owner)
            recv_next_msg(msg)
            return
        current = ACTIVE_SESSION.get(owner)
        lines = ["会话列表："]
        for m in metas:
            mark = "*" if m.name == current else " "
            lines.append(f"{mark} {m.name} [{m.status}] cwd={m.workdir}")
        send_msg("\n".join(lines), owner)
        recv_next_msg(msg)
        return

    if cmd == "/stop":
        name = args[0] if args else ACTIVE_SESSION.get(owner)
        if not name:
            send_msg("没有可停止的当前会话", owner)
            recv_next_msg(msg)
            return
        stopped = _stop_runtime(owner, name)
        if not stopped:
            meta = _get_meta(owner, name)
            if meta:
                meta.status = "stopped"
                meta.pid = None
                meta.updated_at = time.time()
                _upsert_meta(meta)
        if ACTIVE_SESSION.get(owner) == name:
            ACTIVE_SESSION.pop(owner, None)
        send_msg(f"会话 {name} 已停止", owner)
        recv_next_msg(msg)
        return

    if cmd == "/pwd":
        name = ACTIVE_SESSION.get(owner)
        if not name:
            send_msg("当前没有活跃会话", owner)
            recv_next_msg(msg)
            return
        meta = _get_meta(owner, name)
        if not meta:
            send_msg("当前会话不存在", owner)
            recv_next_msg(msg)
            return
        send_msg(f"当前会话: {name}\n工作目录: {meta.workdir}", owner)
        recv_next_msg(msg)
        return

    # 普通消息转发给当前会话
    name = ACTIVE_SESSION.get(owner)
    if not name:
        send_msg("当前无活跃会话，请先 /start 或 /resume", owner)
        recv_next_msg(msg)
        return

    with SESSIONS_LOCK:
        rt = SESSIONS.get((owner, name))

    if not rt or not rt.alive or rt.process.poll() is not None:
        send_msg(f"当前会话 {name} 未运行，请先 /resume {name}", owner)
        recv_next_msg(msg)
        return

    try:
        _send_to_runtime(rt, text)
    except Exception as e:
        send_msg(f"发送失败: {e}", owner)

    recv_next_msg(msg)


if __name__ == '__main__':
    from util.debug.debug import debug_handle

    user_input = '/help'
    debug_handle(handle, user_input)
