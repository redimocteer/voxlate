"""Pinned offline recognition models and their runtime selection."""
from pathlib import Path
from .qwen_manifest import PARTS

MODELS = {
    "asr_large_model": dict(size_name="large-v3", title="Whisper large-v3 · 质量优先识别", relative="models/faster-whisper-large-v3",
        size=3090835702, repo="Systran/faster-whisper-large-v3", revision="edaa852ec7e145841d8ffdb056a99866b5f0a478",
        files={"model.bin": (3087284237, "69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1"),
               "config.json": (2394, None), "preprocessor_config.json": (340, None),
               "tokenizer.json": (2480617, None), "vocabulary.json": (1068114, None)}),
    "asr_turbo_model": dict(size_name="large-v3-turbo", title="Whisper turbo · 快速识别", relative="models/faster-whisper-large-v3-turbo",
        size=1621665983, repo="dropbox-dash/faster-whisper-large-v3-turbo", revision="0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf",
        files={"model.bin": (1617884929, "e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da"),
               "config.json": (2263, None), "preprocessor_config.json": (340, None),
               "tokenizer.json": (2710337, None), "vocabulary.json": (1068114, None)}),
}
MODELS['asr_qwen_model'] = dict(size_name='qwen3-asr-1.7b', title='Qwen3-ASR 1.7B · 含时间对齐',
    relative='models/qwen3-asr-1.7b', size=sum(size for p in PARTS for size, _ in p['files'].values()),
    files={p['prefix']+name: spec for p in PARTS for name, spec in p['files'].items()})


def is_qwen(cfg):
    return selected_key(cfg) == 'asr_qwen_model'


def required_model(cfg, key):
    return key in ('asr_large_model', 'asr_turbo_model') if cfg['asr'].get('combined', False) else key == selected_key(cfg)


def selected_key(cfg):
    name = cfg["asr"].get("model_size")
    if name:
        return next((k for k, item in MODELS.items() if item["size_name"] == name), "asr_turbo_model")
    folder = Path(cfg["asr"]["model_path"]).name
    return next((k for k, item in MODELS.items() if Path(item["relative"]).name == folder), "asr_turbo_model")


def select_model(cfg, key, root):
    item = MODELS[key]
    previous = selected_key(cfg)
    legacy = Path(cfg["asr"]["model_path"]).name == "faster-whisper-base" or cfg["asr"].get("model_size") == "base"
    cfg.get("asr_profiles", {}).pop("asr_model", None)
    if key != previous or legacy:
        options = ("device", "compute_type", "cpu_threads", "beam_size")
        profiles = cfg.setdefault("asr_profiles", {})
        if not legacy:
            profiles[previous] = {k: cfg["asr"][k] for k in options if k in cfg["asr"]}
        saved = profiles.get(key, {})
        cfg["asr"]["beam_size"] = 5
        cfg["asr"].update({k: saved[k] for k in options if k in saved})
    cfg["asr"]["model_path"] = str(Path(root) / item["relative"])
    cfg["asr"].pop("model_size", None)
    if key == 'asr_qwen_model':
        cfg['asr']['combined'] = False
        cfg['asr']['python'] = str(Path(root)/'.venv-qwen/Scripts/python.exe')
        cfg['asr']['compute_type'] = 'float16' if cfg['asr']['device'] == 'cuda' else 'float32'
    else:
        cfg['asr'].pop('python', None)
    cfg.setdefault('qwen', {})['python'] = str(Path(root)/'.venv-qwen/Scripts/python.exe')
    return cfg


def missing_files(key, directory):
    directory = Path(directory)
    return [name for name, (size, _) in MODELS[key]["files"].items()
            if not (directory/name).is_file() or (directory/name).stat().st_size != size]


def download_model(key, directory, emit, mirrors=True, progress=None):
    from .resource_terms import preserve_model_terms
    from .installer import download, HF_MIRROR, HF_OFFICIAL
    from .media import check_cancelled
    from .common import VoxlateError
    item, directory = MODELS[key], Path(directory)
    preserve_model_terms(key, directory, emit)
    total = sum(size for size, _ in item["files"].values())
    completed = 0
    for name, (size, checksum) in item["files"].items():
        check_cancelled()
        if progress:
            progress(completed / total, (completed + size) / total)
        target = directory / name
        # Metadata comes from an immutable revision; weights are SHA-256 checked.
        if not checksum and target.is_file() and target.stat().st_size == size:
            completed += size
            continue
        part = next((p for p in PARTS if name.startswith(p['prefix']) and name.removeprefix(p['prefix']) in p['files']), None) if key == 'asr_qwen_model' else None
        repo, revision, remote_name = (part['repo'], part['revision'], name.removeprefix(part['prefix'])) if part else (item['repo'], item['revision'], name)
        endpoints = [HF_MIRROR, HF_OFFICIAL] if mirrors else [HF_OFFICIAL]
        for index, endpoint in enumerate(endpoints):
            try:
                emit("识别模型下载来源：" + endpoint)
                download(f"{endpoint}/{repo}/resolve/{revision}/{remote_name}", target, emit, checksum)
                if target.stat().st_size != size:
                    raise VoxlateError(f"{name} 大小不正确，请重试下载。")
                break
            except Exception:
                check_cancelled()
                if index == len(endpoints) - 1:
                    raise
                emit("镜像暂不可用，尝试原站；保留已有下载。")
        completed += size
