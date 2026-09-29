"""Actionable readiness checks shared by the GUI and CLI."""
from dataclasses import dataclass, replace
import json
from pathlib import Path
import shutil
import subprocess
import sys

from .runtime import bundle_root, external_code_root, external_env, run_external
from .resources import CATALOG, ResourceInspection, measure_resources
from .translation_models import MODELS, selected_key
from .recognition_models import MODELS as ASR_MODELS, selected_key as selected_asr_key, missing_files, required_model, is_qwen


@dataclass
class ResourceStatus:
    key: str
    title: str
    ready: bool
    detail: str
    instructions: str
    url: str = ""
    required: bool = True


def resource_required(cfg, key):
    if key in ASR_MODELS:
        return required_model(cfg, key)
    if key in MODELS:
        return key == selected_key(cfg)
    if key == 'qwen':
        return not cfg['asr'].get('combined', False) and is_qwen(cfg)
    return True


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def install_command(config_path, step):
    persistent = Path(config_path).resolve().parent / "support/scripts/setup_windows.py"
    script = persistent if persistent.exists() else bundle_root() / "scripts" / "setup_windows.py"
    return f"py -3.11 {ps_quote(script)} {step} --config {ps_quote(Path(config_path).resolve())}"


MODEL_FILES = {
    "asr": ["model.bin", "config.json", "tokenizer.json"],
    "translator": ["model.bin", "config.json", "source.spm", "target.spm", "shared_vocabulary.json"],
    "tts": ["config.yaml", "gpt.pth", "s2mel.pth", "codec.pth", "feat1.pt", "feat2.pt",
            "wav2vec2bert_stats.pt", "multilingual_zh_ja_yue_char_del.tiktoken",
            "hf_cache/w2v-bert-2.0/config.json", "hf_cache/w2v-bert-2.0/preprocessor_config.json",
            "hf_cache/w2v-bert-2.0/model.safetensors", "hf_cache/campplus_cn_common.bin",
            "hf_cache/bigvgan/config.json", "hf_cache/bigvgan/bigvgan_generator.pt"]}


def probe_runtime(python, kind, cfg):
    if not python or not Path(python).is_file():
        return False, "未找到运行环境中的 python.exe"
    if getattr(sys, "frozen", False) and Path(python).resolve() == Path(sys.executable).resolve():
        return False, "选择了 voxlate.exe；请改选运行环境中的 python.exe"
    try:
        result = run_external([python, "-u", str(external_code_root() / "voxlate/resource_probe.py")],
                                input=json.dumps({"kind": kind, "config": cfg}),
                                text=True, encoding="utf-8", errors="replace",
                                timeout=45, env=external_env())
        lines = [s[len("VOXLATE_PROBE="):] for s in result.stdout.splitlines() if s.startswith("VOXLATE_PROBE=")]
        if not lines:
            return False, "运行环境无法启动或检查异常：" + (result.stderr.strip()[-700:] or f"退出码 {result.returncode}")
        report = json.loads(lines[-1])
        return report["ready"], report["detail"]
    except subprocess.TimeoutExpired:
        return False, "运行环境检查超时（45 秒）；请检查依赖或关闭占用 GPU 的程序后重试。"
    except (OSError, ValueError, KeyError) as exc:
        return False, f"无法检查运行环境：{exc}"


def check_resources(cfg, config_path, report=None, probe=probe_runtime, *, keys=None, cached=None, size_keys=None, quick=False):
    results = []
    prior = {item.key: item for item in cached or []}
    selected = set(keys) if keys is not None else None
    if selected is not None:
        if "ffmpeg" in selected or "ffprobe" in selected:
            selected.update(("ffmpeg", "ffprobe"))
        if "python" in selected:
            selected.update(("runtime", "qwen", "separator", "tts"))

    def reuse(key):
        if selected is not None and key not in selected:
            item = prior.get(key) or ResourceStatus(key, CATALOG[key].title if key in CATALOG else key,
                False, '尚未检查；使用时检查，或点击「检查」。', '')
            results.append(replace(item, required=resource_required(cfg, key)))
            return True
        return False

    def add(item):
        if item.key in CATALOG:
            item.title = CATALOG[item.key].title
        results.append(item)
        if report:
            report(item)

    for name in ("ffmpeg", "ffprobe"):
        if reuse(name):
            continue
        program = shutil.which(cfg[name])
        good = False
        detail = f"未找到 {name}.exe；当前设置：{cfg[name]}"
        if program and quick:
            good, detail = True, "程序文件已找到；运行时验证。"
        elif program:
            try:
                run = run_external([program, "-version"], timeout=10,
                                     text=True, encoding="utf-8", errors="replace")
                good = run.returncode == 0
                detail = run.stdout.splitlines()[0] if good and run.stdout else "程序无法正常执行"
            except (OSError, subprocess.TimeoutExpired) as exc:
                detail = str(exc)
        add(ResourceStatus(name, "视频处理程序" if name == "ffmpeg" else "媒体信息程序", good, detail,
                           "用于读取和导出视频，安装后自动重新检查。",
                           "https://ffmpeg.org/download.html#build-windows"))
    titles = {"runtime": "识别与翻译运行环境", "qwen": "Qwen 识别环境", "separator": "人声分离运行环境", "tts": "音色克隆运行环境"}
    for kind, title in titles.items():
        if reuse(kind):
            continue
        python = cfg.get(kind, {}).get("python") or (sys.executable if kind == "runtime" and not getattr(sys, "frozen", False) else "")
        if kind == 'qwen':
            from .resources import current_root
            python = str(current_root(cfg)/'.venv-qwen/Scripts/python.exe')
        if quick:
            ready = bool(python and Path(python).is_file())
            detail = "环境文件已找到；运行时验证。" if ready else "未找到运行环境中的 python.exe"
        else:
            ready, detail = probe(python, kind, cfg)
        instructions = "" if ready else "安装或调整设置后，点击「检查」重新检查。"
        add(ResourceStatus(kind, title, ready, detail, instructions, "https://www.nvidia.com/Download/index.aspx",
                           required=not cfg['asr'].get('combined', False) and is_qwen(cfg) if kind == 'qwen' else True))
    for kind, title in (("separator", "人声分离模型"), ("tts", "音色克隆及辅助模型")):
        if reuse(kind + "_model"):
            continue
        directory = Path(cfg[kind]["model_path"])
        file_kind = kind
        files = MODEL_FILES[file_kind] if file_kind in MODEL_FILES else [cfg[kind]["model"] + ".yaml"]
        files = list(files)
        missing = [p for p in files if not (directory / p).is_file() or (directory / p).stat().st_size == 0]
        if kind == "separator":
            pattern = "955717e8-*.th" if cfg[kind]["model"] == "htdemucs" else "*.th"
            if not any(p.stat().st_size for p in directory.glob(pattern)):
                missing.append("模型权重 " + pattern)
        detail = "关键文件已找到（首次推理仍会验证模型兼容性）" if not missing else "缺少：\n" + "\n".join(missing)
        add(ResourceStatus(kind + "_model", title, not missing, detail,
                           f"目录：{directory}\n"
                           "下载中断可重试，已完成文件会复用。手动复制模型后，点击「检查」重新检查。"))
    from .resources import current_root
    root = current_root(cfg)
    for key, item in ASR_MODELS.items():
        if reuse(key):
            continue
        directory = Path(cfg["asr"]["model_path"]) if key == selected_asr_key(cfg) else root / item["relative"]
        missing = missing_files(key, directory)
        add(ResourceStatus(key, item["title"], not missing,
            "模型文件已就绪。" if not missing else "缺少或不完整：\n" + "\n".join(missing),
            "支持英文和日文；综合识别使用 Whisper v3 和 turbo。", required=required_model(cfg, key)))
    if not reuse("llm_engine"):
        engine = root / "tools/llama/llama-server.exe"
        ready, detail = False, "请安装翻译运行程序。"
        if engine.is_file() and quick:
            ready, detail = True, "程序文件已找到；运行时验证。"
        elif engine.is_file():
            try:
                result = run_external([str(engine), "--version"], timeout=15, text=True, encoding="utf-8", errors="replace")
                ready = result.returncode == 0
                detail = "翻译运行程序可启动。" if ready else result.stderr[-500:]
            except (OSError, subprocess.TimeoutExpired) as exc:
                detail = str(exc)
        add(ResourceStatus("llm_engine", "", ready, detail, "Hy-MT2 共用；使用显卡加速需要支持 Vulkan 的显卡驱动。"))
    for key, item in MODELS.items():
        if reuse(key):
            continue
        model = root / item["relative"] / item["filename"]
        ready = model.is_file() and model.stat().st_size == item["size"]
        add(ResourceStatus(key, item["title"], ready, "模型文件已就绪。" if ready else "模型尚未下载完整。",
            "中、英、日互译，自动参考前后对白。首次运行会验证兼容性。", required=selected_key(cfg) == key))
    if quick:
        return measure_resources(cfg, config_path, results, keys=set(),
                                 cached=cached if isinstance(cached, ResourceInspection) else ResourceInspection([], {}, {}, ""))
    return measure_resources(cfg, config_path, results, keys=selected if size_keys is None else size_keys, cached=cached)
