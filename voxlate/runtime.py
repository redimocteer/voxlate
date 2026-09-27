"""Source and frozen applications share external, isolated model workers."""
from pathlib import Path
import os
import sys
import subprocess
import threading

_spawn_lock = threading.Lock()
_external_root = None


def set_external_code_root(path):
    global _external_root
    _external_root = Path(path).resolve()


def external_code_root():
    # A Python 3.11 child must never put the one-file Python 3.12 extraction
    # directory on sys.path: its .pyd modules would shadow the child's stdlib.
    if getattr(sys, "frozen", False):
        if _external_root is None:
            raise VoxlateError("外部运行代码尚未初始化，请重新打开 voxlate")
        return _external_root
    return bundle_root()


def spawn_external(args, **kwargs):
    """Do not let the frozen app's DLL search path contaminate Python/FFmpeg."""
    with _spawn_lock:
        saved = None
        kernel = None
        if os.name == "nt" and getattr(sys, "frozen", False):
            import ctypes
            kernel = ctypes.windll.kernel32
            buffer = ctypes.create_unicode_buffer(32768)
            kernel.GetDllDirectoryW(len(buffer), buffer)
            saved = buffer.value
            kernel.SetDllDirectoryW(None)
        try:
            return subprocess.Popen(args, **hidden_process_options(), **kwargs)
        finally:
            if kernel:
                kernel.SetDllDirectoryW(saved)


def run_external(args, *, input=None, timeout=None, **kwargs):
    with spawn_external(args, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs) as process:
        try:
            stdout, stderr = process.communicate(input, timeout=timeout)
        except BaseException:
            process.kill()
            process.communicate()
            raise
        return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)

from .common import VoxlateError


def bundle_root():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))


def worker_command(config, kind, request):
    if kind == 'speakers':
        kind = 'tts'
    python = config[kind].get("python") or config.get("runtime", {}).get("python")
    if kind == 'asr' and config['asr'].get('combined', False):
        python = config.get('runtime', {}).get('python')
    if not python:
        if getattr(sys, "frozen", False):
            raise VoxlateError("请在资源配置中选择识别与翻译运行环境。EXE 不能充当 Python 解释器。")
        python = sys.executable
    if Path(python).resolve() == Path(sys.executable).resolve() and getattr(sys, "frozen", False):
        raise VoxlateError("运行环境必须是 python.exe，不能选择 voxlate.exe。")
    return [str(python), "-u", str(external_code_root() / "voxlate" / "worker.py"), str(request)]


def hidden_process_options():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def external_env():
    env = dict(os.environ, PYTHONIOENCODING="utf-8", HF_HUB_OFFLINE="1",
               TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1")
    # Do not leak the packager's Python/Qt environment into external runtimes.
    for key in ("PYTHONHOME", "PYTHONPATH", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
        env.pop(key, None)
    return env
