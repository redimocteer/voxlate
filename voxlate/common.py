from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
from contextlib import contextmanager
from contextvars import ContextVar

_inference_port = ContextVar('voxlate_inference_port', default=None)


@contextmanager
def local_inference_connection(port):
    """Permit only the owned native translator's numeric loopback endpoint."""
    if type(port) is not int or not 1 <= port <= 65535:
        raise VoxlateError('本机翻译端口无效。')
    token = _inference_port.set(port)
    try:
        yield
    finally:
        _inference_port.reset(token)


class VoxlateError(RuntimeError):
    pass


def format_timestamp(seconds):
    """Display a media position with millisecond precision, including long videos."""
    milliseconds = max(0, round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}'


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def digest(*items):
    return hashlib.sha256(json.dumps(items, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def model_stamp(path):
    """Invalidate caches on model replacement without hashing gigabytes each run."""
    path = Path(path)
    if not path.is_dir():
        raise VoxlateError(f"模型目录不存在：{path}。请先按 README 准备本地模型。")
    return digest([(str(p.relative_to(path)), p.stat().st_size, p.stat().st_mtime_ns)
                   for p in sorted(path.rglob("*")) if p.is_file() and ".cache" not in p.parts])


def enable_offline():
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                      HF_HUB_DISABLE_TELEMETRY="1", DO_NOT_TRACK="1")

    def deny_network(event, args):
        if event == 'socket.connect' and args[1] == ('127.0.0.1', _inference_port.get()) and _inference_port.get() is not None:
            return
        if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
            raise VoxlateError("离线模式禁止联网；请在准备阶段补齐模型和辅助资源。")

    sys.addaudithook(deny_network)


def load_config(path):
    path = Path(path).resolve()
    cfg = read_json(path)
    required = {
        "asr": ("model_path", "device", "compute_type", "beam_size", "cpu_threads"),
        "translator": ("model_path", "device", "compute_type", "beam_size", "cpu_threads"),
        "separator": ("python", "model_path", "model", "device", "shifts", "overlap"),
        "tts": ("python", "repo_path", "model_path", "device", "use_bf16", "emotion_reference"),
        "audio": ("sample_rate", "background_gain", "dubbing_gain"),
    }
    if not isinstance(cfg, dict):
        raise VoxlateError("配置必须是 JSON 对象")
    for section, keys in required.items():
        if not isinstance(cfg.get(section), dict):
            raise VoxlateError(f"配置缺少 {section} 分组")
        for key in keys:
            if key not in cfg[section]:
                raise VoxlateError(f"配置缺少 {section}.{key}")
    if "runtime" in cfg and (not isinstance(cfg["runtime"], dict) or "python" not in cfg["runtime"]):
        raise VoxlateError("配置缺少 runtime.python")
    for key in ("ffmpeg", "ffprobe"):
        if not isinstance(cfg.get(key), str) or not cfg[key].strip():
            raise VoxlateError(f"配置缺少有效的 {key} 路径")
    for section in ("asr", "translator"):
        for key in ("beam_size", "cpu_threads"):
            if type(cfg[section][key]) is not int or cfg[section][key] < 1:
                raise VoxlateError(f"{section}.{key} 必须是正整数")
    for section in ("asr", "translator", "separator", "tts"):
        if not isinstance(cfg[section]["device"], str) or not cfg[section]["device"].strip():
            raise VoxlateError(f"{section}.device 必须是 cpu 或 cuda")
    for section in ("asr", "translator"):
        if not isinstance(cfg[section]["compute_type"], str):
            raise VoxlateError(f"{section}.compute_type 必须是有效的精度名称")
    cfg['asr'].setdefault('combined', True)
    if type(cfg['asr']['combined']) is not bool:
        raise VoxlateError('综合识别选项必须为开或关')
    for key in ("use_bf16", "emotion_reference"):
        if type(cfg["tts"][key]) is not bool:
            raise VoxlateError(f"tts.{key} 必须是 true 或 false")
    import re
    model = cfg["separator"]["model"]
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", model):
        raise VoxlateError("separator.model 必须是本地模型名称，例如 htdemucs")
    if type(cfg["separator"]["shifts"]) is not int or cfg["separator"]["shifts"] < 0:
        raise VoxlateError("separator.shifts 必须为非负整数")
    overlap = cfg["separator"]["overlap"]
    if not isinstance(overlap, (int, float)) or not 0 <= overlap < 1:
        raise VoxlateError("separator.overlap 必须在 0（含）到 1（不含）之间")
    for name in ("runtime", "qwen", "asr", "translator", "separator", "tts"):
        if name not in cfg:
            continue
        for key in ("model_path", "repo_path", "python", "engine_path"):
            if key in cfg[name]:
                if not isinstance(cfg[name][key], str) or not cfg[name][key].strip():
                    raise VoxlateError(f"{name}.{key} 路径不能为空")
                cfg[name][key] = str((path.parent / cfg[name][key]).resolve())
    for key in ("ffmpeg", "ffprobe"):
        if "/" in cfg[key] or "\\" in cfg[key]:
            cfg[key] = str((path.parent / cfg[key]).resolve())
    if cfg["audio"]["sample_rate"] not in (16000, 22050, 24000, 44100, 48000):
        raise VoxlateError("不支持的 audio.sample_rate")
    for key in ("background_gain", "dubbing_gain"):
        value = cfg["audio"][key]
        if not isinstance(value, (int, float)) or not 0 <= value <= 4:
            raise VoxlateError(f"{key} 必须在 0 到 4 之间")
    cfg.setdefault("source_lang", "en")
    cfg.setdefault("target_lang", "zh")
    from .languages import direction
    direction(cfg)
    if cfg['translator'].get('model_type') == 'hy_mt2_1b':
        from .translation_models import MODELS
        item = MODELS['hy7_model']
        root = (path.parent / cfg['resource_root']).resolve() if cfg.get('resource_root') else Path(cfg['translator']['model_path']).parent.parent
        cfg['translator'].update(model_type=item['kind'], model_path=str(root / item['relative']))
    return cfg
