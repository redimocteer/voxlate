"""Shared resource names, storage locations and reviewed cleanup plans."""
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import sys

from .common import VoxlateError, read_json, write_json
from .translation_models import MODELS, selected_key
from .recognition_models import MODELS as ASR_MODELS, selected_key as selected_asr_key


@dataclass(frozen=True)
class ResourceInfo:
    title: str
    category: str
    estimate: str
    relative: str


CATALOG = {
    "ffmpeg": ResourceInfo("视频处理程序（FFmpeg / FFprobe）", "程序", "约 220 MB", "tools/ffmpeg"),
    "runtime": ResourceInfo("识别与翻译环境", "环境", "约 500 MB", ".venv"),
    "qwen": ResourceInfo("Qwen 识别环境", "环境", "约 6 GB", ".venv-qwen"),
    "separator": ResourceInfo("人声分离环境", "环境", "约 5 GB", ".venv-separator"),
    "tts": ResourceInfo("音色克隆环境", "环境", "约 8 GB", "third_party/index-tts"),
    "separator_model": ResourceInfo("人声分离模型 · Demucs htdemucs", "模型", "约 85 MB", "models/demucs"),
    "tts_model": ResourceInfo("音色克隆模型 · IndexTTS 2.5", "模型", "约 7.1 GB", "models/IndexTTS-2.5"),
}
EXTRAS = {
    "download_cache": ResourceInfo("下载与安装缓存", "其它", "按实际统计", "setup-cache"),
    "prepare_env": ResourceInfo("模型准备临时环境", "其它", "按实际统计", "prepare-env"),
    "python": ResourceInfo("共享 Python", "其它", "约 100 MB", "tools/python"),
    "python_bin": ResourceInfo("Python 启动文件", "其它", "小于 1 MB", "tools/python-bin"),
    "uv": ResourceInfo("自动安装工具", "其它", "约 60 MB", "tools/uv"),
}
CATALOG["llm_engine"] = ResourceInfo("翻译运行程序（llama.cpp）", "程序", "约 100 MB", "tools/llama")
for _key, _model in MODELS.items():
    CATALOG[_key] = ResourceInfo(_model["title"], "模型", f"约 {_model['size'] / 1_000_000_000:.2f} GB", _model["relative"])
for _key, _model in ASR_MODELS.items():
    CATALOG[_key] = ResourceInfo(_model["title"], "模型", f"约 {_model['size'] / 1_000_000_000:.2f} GB", _model["relative"])
CATALOG = {key: CATALOG[key] for key in ("ffmpeg", "llm_engine", "runtime", "qwen", "separator", "tts",
                                      "asr_large_model", "asr_turbo_model", "asr_qwen_model", "hy7_model", "separator_model", "tts_model")}

PURPOSES = {
    'ffmpeg': '用于提取音轨、处理音视频和导出视频。',
    'llm_engine': '用于运行 Hy-MT2 翻译模型。',
    'runtime': '运行语音识别及翻译辅助程序所需的依赖。',
    'qwen': '运行 Qwen 语音识别及文字时间对齐，不影响 Whisper 环境。',
    'asr_qwen_model': '将英文、日文识别成文字；包含词级时间对齐模型。',
    'separator': '运行 Demucs 人声分离所需的依赖。',
    'tts': '运行 IndexTTS 音色克隆所需的程序和依赖。',
    'asr_large_model': '将英文、日文人声识别成文字，质量优先。',
    'asr_turbo_model': '将英文、日文人声识别成文字，速度优先。',
    'hy7_model': '中文、英文、日文之间互译。',
    'separator_model': '用于分离原人声与背景声。',
    'tts_model': '参考原人声音色，合成目标语言配音。',
    'download_cache': '保存下载包和安装缓存，便于续传或重复安装。',
    'prepare_env': '用于下载和转换模型，处理视频时不用。',
    'python': '供识别、人声分离等环境使用的 Python 程序。',
    'python_bin': '用于启动共享 Python。',
    'uv': '用于自动安装 Python 和运行依赖。',
}


def current_root(cfg):
    return Path(cfg.get("resource_root") or Path(cfg["runtime"]["python"]).parent.parent.parent).resolve()


def configured_paths(cfg, key):
    if key == 'qwen':
        return [current_root(cfg)/'.venv-qwen']
    if key in ASR_MODELS:
        return [Path(cfg["asr"]["model_path"]).resolve()] if key == selected_asr_key(cfg) else [current_root(cfg) / ASR_MODELS[key]["relative"]]
    if key in MODELS:
        if key == selected_key(cfg):
            return [Path(cfg["translator"]["model_path"]).resolve()]
        return [current_root(cfg) / MODELS[key]["relative"]]
    if key == "llm_engine":
        return [current_root(cfg) / "tools/llama"]
    if key == "ffmpeg":
        return [Path(p).resolve() for name in ("ffmpeg", "ffprobe") if (p := shutil.which(cfg[name]))]
    if key.endswith("_model"):
        return [Path(cfg[key.removesuffix("_model")]["model_path"]).resolve()]
    if key == "tts":
        repo = Path(cfg[key]["repo_path"]).resolve()
        env = Path(cfg[key]["python"]).resolve().parent.parent
        return [repo] if env.is_relative_to(repo) else [repo, env]
    return [Path(cfg[key]["python"]).resolve().parent.parent]


def remember_root(config_path, root):
    path = Path(config_path).parent / "resource-locations.json"
    roots = read_json(path) if path.exists() else []
    root = str(Path(root).resolve())
    if root not in roots:
        write_json(path, [*roots, root])


def extra_resource_paths(cfg, config_path):
    return {key: paths for key, paths in tracked_resource_paths(cfg, config_path).items() if key in EXTRAS}


def tracked_resource_paths(cfg, config_path):
    """Presence checks across current and previously selected resource locations."""
    registry = Path(config_path).parent / "resource-locations.json"
    roots = list(dict.fromkeys([str(current_root(cfg)), *(read_json(registry) if registry.exists() else [])]))
    return {key: [path for root in roots if (path := Path(root) / info.relative).exists()]
            for key, info in {**CATALOG, **EXTRAS}.items()}


def format_bytes(size):
    return f"{size / 1_000_000_000:.2f} GB" if size >= 1_000_000_000 else f"{size / 1_000_000:.1f} MB"


def is_link(path):
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def folder_size(path):
    total = 0
    for directory, folders, files in os.walk(path, followlinks=False):
        base = Path(directory)
        folders[:] = [name for name in folders if not is_link(base / name)]
        for name in files:
            target = base / name
            if not is_link(target):
                total += target.stat().st_size
    return total


class ResourceInspection(list):
    """Keep the readiness list API while carrying background size measurements."""

    def __init__(self, results, sizes, estimates, summary):
        super().__init__(results)
        self.sizes, self.estimates, self.summary = sizes, estimates, summary


def measure_resources(cfg, config_path, results, *, keys=None, cached=None):
    from .media import check_cancelled

    def fail_scan(error):
        raise error

    record = Path(config_path).parent / "resource-size-estimates.json"
    try:
        learned = read_json(record) if record.exists() else {}
        if not isinstance(learned, dict):
            learned = {}
    except (OSError, ValueError):
        learned = {}
    previous = dict(learned)
    ready = {item.key: item.ready for item in results}
    ready["ffmpeg"] = ready.get("ffmpeg", False) and ready.get("ffprobe", False)
    tracked = tracked_resource_paths(cfg, config_path)
    sizes, estimates, errors = {}, {}, []
    for key, info in {**CATALOG, **EXTRAS}.items():
        check_cancelled()
        if keys is not None and key not in keys and isinstance(cached, ResourceInspection):
            if key in getattr(cached, "sizes", {}):
                sizes[key] = cached.sizes[key]
            estimates[key] = getattr(cached, "estimates", {}).get(key, info.estimate)
            continue
        # Core entries describe the selected location; extras include retained old locations.
        paths = tracked[key] if key in EXTRAS else [current_root(cfg) / info.relative]
        options = cfg.get(key, {})
        variant = info.relative + ":" + str(options.get("device", "") if isinstance(options, dict) else "")
        total, present, partial = 0, False, False
        try:
            for path in paths:
                if not path.exists() or is_link(path):
                    continue
                present = True
                for directory, folders, files in os.walk(path, followlinks=False, onerror=fail_scan):
                    check_cancelled()
                    base = Path(directory)
                    folders[:] = [name for name in folders if not is_link(base / name)]
                    for name in files:
                        target = base / name
                        if not is_link(target):
                            total += target.stat().st_size
                            partial |= name.endswith((".incomplete", ".part"))
            if present:
                sizes[key] = total
                if key in CATALOG and ready.get(key) and not partial and total:
                    learned[variant] = total
        except OSError:
            errors.append(info.title)
        value = learned.get(variant)
        estimates[key] = "约 " + format_bytes(value) if isinstance(value, int) and value > 0 else info.estimate
    if learned != previous:
        try:
            write_json(record, learned)
        except OSError:
            errors.append("预估大小保存失败")
    summary = f"空间统计完成：{len(sizes)} 项，共 {format_bytes(sum(sizes.values()))}（文件大小合计，含缓存及保留的旧资源）。"
    if keys is not None and isinstance(cached, ResourceInspection):
        summary = f"空间更新完成：文件大小合计 {format_bytes(sum(sizes.values()))}（未变动项沿用上次统计）。" if keys else ""
    if errors:
        summary += " 未能统计或保存：" + "、".join(errors)
    return ResourceInspection(results, sizes, estimates, summary)


@dataclass(frozen=True)
class CleanupEntry:
    key: str
    title: str
    path: Path
    root: Path
    size: int
    deletable: bool
    reason: str = ""


def validate_target(path, root, protected=()):
    """Resolve and bound every recursive operation before touching files."""
    root, path = Path(root).absolute(), Path(path).absolute()
    allowed = {root / info.relative for info in (*CATALOG.values(), *EXTRAS.values())}
    if path not in allowed:
        raise VoxlateError("此目录不是 voxlate 的专用资源目录，仅支持打开位置")
    resolved_root, resolved = root.resolve(), path.resolve()
    if resolved == resolved_root or not resolved.is_relative_to(resolved_root):
        raise VoxlateError("资源目录越界，已拒绝清理")
    for ancestor in (path, *path.parents):
        if is_link(ancestor):
            raise VoxlateError("目录包含链接或联接，仅支持打开位置")
        if ancestor == root:
            break
    for item in (*protected, Path(sys.executable), Path(__file__).resolve()):
        if item and Path(item).resolve().is_relative_to(resolved):
            raise VoxlateError("目录包含正在使用的程序、配置、项目或视频，不能清理")
    for directory, folders, files in os.walk(path, followlinks=False):
        for name in (*folders, *files):
            link = Path(directory) / name
            if is_link(link) and not link.resolve().is_relative_to(resolved):
                raise VoxlateError("目录内部的链接或联接指向其他位置，仅支持打开位置")
        folders[:] = [name for name in folders if not is_link(Path(directory) / name)]
    return resolved


def cleanup_inventory(cfg, config_path, protected=(), *, keys=None):
    registry = Path(config_path).parent / "resource-locations.json"
    roots = list(dict.fromkeys([*(read_json(registry) if registry.exists() else []), str(current_root(cfg))]))
    entries, seen = [], set()
    protected = [*protected, Path(config_path), Path(config_path).parent / "projects"]
    for root_text in roots:
        root = Path(root_text).absolute()
        for key, info in {**CATALOG, **EXTRAS}.items():
            if keys is not None and key not in keys:
                continue
            target = root / info.relative
            if not target.exists() or target in seen:
                continue
            seen.add(target)
            try:
                validate_target(target, root, protected)
                size = folder_size(target)
                reason = ""
            except (OSError, VoxlateError) as exc:
                size, reason = 0, str(exc)
            entries.append(CleanupEntry(key, info.title, target, root, size, not reason, reason))
    # External paths remain visible and locatable, but are not recursively removed.
    for key, info in CATALOG.items():
        if keys is not None and key not in keys:
            continue
        if key == "ffmpeg":
            continue
        for path in configured_paths(cfg, key):
            if path.exists() and path not in seen:
                entries.append(CleanupEntry(key, info.title, path, current_root(cfg), 0, False,
                                            "自选或共享目录，仅支持打开位置"))
                seen.add(path)
    return entries


def clean_resources(entries, cfg, config_path, emit, protected=()):
    protected = [*protected, Path(config_path), Path(config_path).parent / "projects"]
    targets = []
    for entry in entries:
        if not entry.deletable:
            raise VoxlateError("所选目录不可自动清理：" + str(entry.path))
        targets.append(validate_target(entry.path, entry.root, protected))
    # Managed Python is shared. Never leave a retained environment pointing at a removed interpreter.
    for entry in entries:
        if entry.key != "python":
            continue
        environments = [Path(cfg[k]["python"]).parent.parent for k in ("runtime", "separator", "tts")]
        environments += [entry.root / p for p in (".venv", ".venv-qwen", ".venv-separator", "third_party/index-tts/.venv", "prepare-env")]
        for env in environments:
            marker = env / "pyvenv.cfg"
            if marker.exists() and not any(env.resolve().is_relative_to(p) for p in targets):
                home = next((line.split("=", 1)[1].strip() for line in marker.read_text(encoding="utf-8").splitlines()
                             if line.lower().startswith("home") and "=" in line), "")
                if home and Path(home).resolve().is_relative_to(entry.path.resolve()):
                    raise VoxlateError(f"共享 Python 仍被此环境使用，请先清理该环境或保留共享 Python：\n{env}")
    for entry, target in zip(entries, targets):
        # Revalidate at the mutation boundary, including after any previous deletion.
        validate_target(entry.path, entry.root, protected)
        if target.exists():
            emit(f"清理 {entry.title}：{target}")
            try:
                shutil.rmtree(target)
            except OSError as exc:
                raise VoxlateError(f"无法完整清理 {target}：{exc}\n请关闭使用该资源的程序后重试；已删除部分不会恢复。") from exc
    return len(entries)
