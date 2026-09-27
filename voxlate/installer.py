"""Explicit online setup, invoked by GUI buttons; never by the dubbing pipeline."""
from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
import queue
import re
import shutil
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zipfile

from .common import VoxlateError, write_json
from .media import check_cancelled
from .runtime import bundle_root, external_code_root, external_env, spawn_external, run_external
from .resources import CATALOG, remember_root, format_bytes, folder_size, is_link
from .translation_models import MODELS, selected_key, select_model, ENGINE_URL, ENGINE_HASH
from .recognition_models import MODELS as ASR_MODELS, selected_key as selected_asr_key, select_model as select_asr_model, download_model as download_asr
from .resource_terms import preserve_model_terms

UV_VERSION = "0.12.18"
UV_SHA256 = "cae6a3bc25239f83dffb467a4b180508d9da23986c04639ebfa44e43e6a84bff"
TTS_REVISION = "ee40fa7d6c6b8a2c7f06105f9f1e65775b74868c"
STAGES = ("ffmpeg", "runtime", "qwen", "separator", "tts", "llm_engine", "asr_large_model", "asr_turbo_model", "asr_qwen_model", "hy7_model", "separator_model", "tts_model")
TITLES = {key: CATALOG[key].title for key in STAGES}
PYPI_OFFICIAL = "https://pypi.org/simple"
PYPI_MIRROR = "https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple"
HF_OFFICIAL = "https://huggingface.co"
HF_MIRROR = "https://hf-mirror.com"
STAGE_WEIGHTS = dict(ffmpeg=220, runtime=500, separator=5000, tts=8000,
                     qwen=6000, asr_qwen_model=6544,
                     translator_model=500, separator_model=85, tts_model=7100,
                     llm_engine=100, hy7_model=4625, asr_large_model=3091, asr_turbo_model=1622)


def resource_root(cfg):
    return Path(cfg.get("resource_root") or Path(cfg["runtime"]["python"]).parent.parent.parent).resolve()


def relocate(cfg, root):
    """Use the GUI's shared resource location while preserving runtime options."""
    cfg = copy.deepcopy(cfg)
    asr_key = selected_asr_key(cfg)
    previous_root = resource_root(cfg)
    root = Path(root).resolve()
    cfg["resource_root"] = str(root)
    paths = {"runtime": {"python": ".venv/Scripts/python.exe"},
             "translator": {"model_path": "models/m2m100-418M-int8"},
             "separator": {"python": ".venv-separator/Scripts/python.exe", "model_path": "models/demucs"},
             "tts": {"python": "third_party/index-tts/.venv/Scripts/python.exe",
                     "repo_path": "third_party/index-tts", "model_path": "models/IndexTTS-2.5"}}
    for section, values in paths.items():
        cfg[section].update({key: str(root / value) for key, value in values.items()})
    select_model(cfg, selected_key(cfg), root)
    select_asr_model(cfg, asr_key, root)
    cfg["translator"].pop("ja_model_path", None)
    # Keep working system FFmpeg; new copies use the selected resource location.
    for name in ("ffmpeg", "ffprobe"):
        if not shutil.which(cfg[name]) or Path(cfg[name]).parent == previous_root / "tools/ffmpeg":
            cfg[name] = str(root / "tools/ffmpeg" / (name + ".exe"))
    return cfg


def planned_stages(results, selected=None):
    keys = {selected} if selected else {item.key for item in results if not item.ready and item.required}
    if "ffprobe" in keys:
        keys.add("ffmpeg")
    return [stage for stage in STAGES if stage in keys]


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(url, target, emit, checksum=None):
    """Resumable downloads to a dedicated partial file, verified before use."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and checksum and sha256(target).startswith(checksum):
        emit(f"复用已下载文件：{target.name}")
        return target
    partial = target.with_name(target.name + ".part")
    size = partial.stat().st_size if partial.exists() else 0
    if checksum and size and sha256(partial).startswith(checksum):
        partial.replace(target)
        return target
    headers = {"User-Agent": "voxlate-resource-installer/1", "Accept-Encoding": "identity"}
    if size:
        headers["Range"] = f"bytes={size}-"
    check_cancelled()
    try:
        response = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and size:
            partial.unlink()
            return download(url, target, emit, checksum)
        raise
    with response:
        resumed = response.status == 206 and response.headers.get("Content-Range", "").startswith(f"bytes {size}-")
        if response.status == 206 and not resumed:
            raise VoxlateError("下载服务器返回了错误的续传位置，请重试")
        if not resumed:
            size = 0
        total = size + int(response.headers.get("Content-Length", 0))
        last = 0
        with partial.open("ab" if resumed else "wb") as stream:
            while True:
                check_cancelled()
                block = response.read(256 * 1024)
                if not block:
                    break
                stream.write(block)
                size += len(block)
                if time.monotonic() - last > 0.5:
                    amount = f"{size / 1048576:.1f} MB"
                    if total:
                        amount += f" / {total / 1048576:.1f} MB（{size * 100 / total:.0f}%）"
                    emit(f"下载 {target.name}：{amount}")
                    last = time.monotonic()
        if total and size != total:
            raise VoxlateError(f"{target.name} 下载未完成，已保留进度，点击按钮重试")
    if checksum and not sha256(partial).startswith(checksum):
        partial.unlink()
        raise VoxlateError(f"{target.name} 校验失败，点击按钮重新下载")
    partial.replace(target)
    return target


def safe_extract(archive, target):
    target = Path(target).resolve()
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as source:
        for entry in source.infolist():
            name = entry.filename.replace("\\", "/")
            path = (target / name).resolve()
            if not path.is_relative_to(target) or ":" in name or stat.S_ISLNK(entry.external_attr >> 16):
                raise VoxlateError("资源压缩包包含不安全路径，已停止解压")
        for entry in source.infolist():
            check_cancelled()
            source.extract(entry, target)


class DownloadActivity:
    """Observe existing HF partial files without changing their resume format."""

    def __init__(self, directory, clock=time.monotonic):
        self.directory, self.clock = directory, clock
        self.previous = self.bytes_saved()
        self.last_sample = self.last_growth = clock()

    def bytes_saved(self):
        total = 0
        for directory, folders, files in os.walk(self.directory, followlinks=False):
            check_cancelled()
            base = Path(directory)
            folders[:] = [name for name in folders if not is_link(base / name)]
            for name in files:
                if name.endswith((".lock", ".metadata")):
                    continue
                path = base / name
                try:
                    if not is_link(path):
                        total += path.stat().st_size
                except OSError:
                    pass  # The downloader may be moving a completed partial file.
        return total

    def sample(self):
        now, total = self.clock(), self.bytes_saved()
        delta = total - self.previous
        if delta > 0:
            detail = f"本项已保存 {format_bytes(total)} · 约 {format_bytes(delta / max(1, now - self.last_sample))}/秒"
            self.last_growth = now
        else:
            idle = int(now - self.last_growth)
            detail = f"本项已保存 {format_bytes(total)} · {idle} 秒未新增数据"
            if idle >= 60:
                detail += "，可能在等待网络或校验；可停止后重试"
        self.previous, self.last_sample = total, now
        return detail


class EnvironmentActivity:
    """Estimate preparation from disk growth, not compressed download bytes or elapsed time."""

    def __init__(self, cache, environment, expected_bytes, clock=time.monotonic):
        self.cache = DownloadActivity(cache, clock)
        self.environment = DownloadActivity(environment, clock)
        self.initial_cache = self.cache.previous
        self.initial_environment = self.environment.previous
        self.expected_bytes, self.clock = max(1, expected_bytes), clock
        self.last_growth = clock()
        self.last_size = self.initial_environment
        self.fraction = 0.2
        self.ceiling = 0.97
        self.operation = "解析依赖"

    def observe(self, line):
        downloading = re.match(r"Downloading (\S+)(?: \(([^)]+)\))?", line.strip())
        if downloading:
            self.operation = "下载 " + downloading[1]
            if downloading[2]:
                self.operation += "（安装包 " + downloading[2] + "）"
        elif line.strip().startswith("Prepared "):
            self.fraction = max(self.fraction, 0.88)
            self.operation = "安装依赖"
        elif line.strip().startswith(("Installed ", "Audited ")):
            self.fraction = max(self.fraction, 0.97)
            self.operation = "依赖处理完成，等待检查"
        elif line.strip().startswith("Building "):
            self.operation = "构建 " + line.strip().split(maxsplit=1)[1][:80]

    def sample(self):
        now = self.clock()
        cached = max(0, self.cache.bytes_saved() - self.initial_cache)
        installed = self.environment.bytes_saved()
        # UV moves extracted files within its cache and links/copies them into the env.
        # Using max avoids counting those two copies as twice the progress.
        size = max(installed, self.initial_environment + cached)
        self.fraction = min(self.ceiling, max(self.fraction, 0.2 + 0.6 * min(1, size / self.expected_bytes)))
        detail = f"环境准备约 {self.fraction * 100:.0f}% · {self.operation}"
        delta = size - self.last_size
        if delta > 0:
            self.last_growth = now
            detail += f" · 文件新增 {format_bytes(max(cached, installed - self.initial_environment))}"
        else:
            detail += f" · {int(now - self.last_growth)} 秒无新增文件"
            if now - self.last_growth >= 60:
                detail += "（可能等待网络或处理依赖）"
        self.last_size = size
        return self.fraction, detail


class Installer:
    def __init__(self, cfg, config_path, emit, progress=None):
        self.cfg = copy.deepcopy(cfg)
        self.config_path = Path(config_path)
        self._emit = emit
        self.progress = progress or (lambda update: None)
        self._base, self._span, self._fraction, self._percent = 0, 95, 0, 0
        self._download_range = None
        self._model_activity = None
        self._environment_activity = None
        self.root = resource_root(cfg)
        self.cache = self.root / "setup-cache"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.log_path = self.cache / "install.log"
        self.env = external_env()
        # A GUI source choice must not be overridden by shell/package-manager settings.
        for key in list(self.env):
            if key.startswith(("PIP_", "UV_INDEX", "UV_EXTRA_INDEX", "UV_DEFAULT_INDEX")) or key in (
                    "UV_FIND_LINKS", "UV_NO_INDEX", "HF_ENDPOINT", "UV_PYTHON_INSTALL_MIRROR",
                    "UV_PYPY_INSTALL_MIRROR", "UV_ASTRAL_MIRROR_URL", "UV_PYTHON_DOWNLOADS_JSON_URL"):
                self.env.pop(key, None)
        for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "UV_OFFLINE", "VIRTUAL_ENV", "CONDA_PREFIX"):
            self.env.pop(key, None)
        self.env.update(UV_CACHE_DIR=str(self.cache / "uv"), UV_PYTHON_INSTALL_DIR=str(self.root / "tools/python"),
                        UV_PYTHON_BIN_DIR=str(self.root / "tools/python-bin"), UV_NO_CONFIG="true",
                        UV_PYTHON_INSTALL_REGISTRY="false", UV_PYTHON_PREFERENCE="only-managed",
                        HF_HOME=str(self.cache / "huggingface"), HF_ENDPOINT=HF_OFFICIAL, HF_HUB_DISABLE_XET="1",
                        HF_HUB_DISABLE_IMPLICIT_TOKEN="1", HF_HUB_ENABLE_HF_TRANSFER="0",
                        PYTHONUNBUFFERED="1", NO_COLOR="1")
        self.uv = self.root / "tools/uv/uv.exe"
        self._uv_ready = False

    def phase(self, fraction, detail):
        self._fraction = max(self._fraction, fraction)
        self._percent = max(self._percent, min(95, self._base + self._span * self._fraction))
        self.progress({"percent": self._percent, "detail": detail})

    def emit(self, message):
        if self._environment_activity:
            self._environment_activity.observe(message)
        files = re.search(r"Fetching\s+\d+\s+files:.*?\|\s*(\d+)/(\d+)", message)
        if files:
            detail = f"本批文件 {files[1]}/{files[2]}（按文件数，非下载大小）"
            self.progress({"percent": self._percent, "detail": detail})
            self._emit(detail)
            return
        # A file reaching 100% does not mean the whole installation is finished.
        match = re.search(r"(\d{1,3}(?:\.\d+)?)%", message)
        if match and (message.startswith("下载 ") or "|" in message):
            if self._download_range and message.startswith("下载 "):
                low, high = self._download_range
                self.phase(low + (high - low) * min(100, float(match[1])) / 100, "下载资源")
            self.progress({"percent": self._percent, "detail": f"当前下载 {min(100, float(match[1])):.0f}%"})
        self._emit(message)

    def run(self, args, label, env=None):
        check_cancelled()
        self.emit(label)
        process = spawn_external([str(a) for a in args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, env=env or self.env, encoding="utf-8", errors="replace")
        lines = queue.Queue()

        def read():
            try:
                for line in process.stdout:
                    lines.put(line.rstrip())
            finally:
                process.stdout.close()
                lines.put(None)

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        tail = []
        last_update = time.monotonic()
        last_activity = last_update
        try:
            with self.log_path.open("a", encoding="utf-8") as log:
                log.write(f"\n--- {label} ---\n")
                done = False
                while not done:
                    check_cancelled()
                    if self._environment_activity and time.monotonic() - last_activity >= 5:
                        fraction, detail = self._environment_activity.sample()
                        self.phase(fraction, detail)
                        if time.monotonic() - last_update >= 15:
                            self._emit(detail)
                            log.write(detail + "\n")
                            log.flush()
                            last_update = time.monotonic()
                        last_activity = time.monotonic()
                    if self._model_activity and time.monotonic() - last_activity >= 5:
                        detail = self._model_activity.sample()
                        self.progress({"percent": self._percent, "detail": detail})
                        if time.monotonic() - last_update >= 15:
                            self._emit(detail)
                            log.write(detail + "\n")
                            log.flush()
                            last_update = time.monotonic()
                        last_activity = time.monotonic()
                    try:
                        line = lines.get(timeout=0.2)
                    except queue.Empty:
                        if not (self._model_activity or self._environment_activity) and time.monotonic() - last_update > 15:
                            self.emit(f"{label}…仍在进行中，可点击「停止」")
                            last_update = time.monotonic()
                        continue
                    if line is None:
                        done = True
                    elif line.strip():
                        log.write(line + "\n")
                        log.flush()
                        tail = (tail + [line])[-15:]
                        self.emit(line[:700])
                        last_update = time.monotonic()
                process.wait()
        except BaseException:
            if process.poll() is None:
                # Kill only this installer's process tree, including package build children.
                if os.name == "nt":
                    try:
                        run_external(["taskkill", "/PID", str(process.pid), "/T", "/F"], timeout=5)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
            raise
        finally:
            reader.join(timeout=3)
        if process.returncode:
            raise VoxlateError(f"{label}失败：\n" + "\n".join(tail)[-2500:] + f"\n完整日志：{self.log_path}")

    def bootstrap(self):
        if self._uv_ready:
            return
        if os.name != "nt":
            raise VoxlateError("一键安装目前支持 Windows x64")
        archive = download(f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/uv-x86_64-pc-windows-msvc.zip",
                           self.cache / f"uv-{UV_VERSION}.zip", self.emit, UV_SHA256)
        safe_extract(archive, self.uv.parent)
        self.run([self.uv, "--version"], "检查自动安装工具")
        self._uv_ready = True

    def venv(self, python):
        self.bootstrap()
        python = Path(python)
        if not python.is_file():
            if python.name.lower() != "python.exe" or python.parent.name.lower() != "scripts":
                raise VoxlateError("环境路径应为 <文件夹>/Scripts/python.exe，请选择资源存放位置自动配置")
            self.run([self.uv, "venv", "--python", "3.11", python.parent.parent], "自动准备独立 Python 3.11 环境")
        return python

    def run_with_mirror(self, args, label, *, mirror_args=None, mirror_env=None, source=""):
        if self.cfg.get("try_mirrors", True) and (mirror_args is not None or mirror_env is not None):
            self.emit(f"尝试镜像站：{source}")
            try:
                return self.run(mirror_args if mirror_args is not None else args, label, env=mirror_env)
            except VoxlateError as exc:
                check_cancelled()
                if "已取消" in str(exc):
                    raise
                self.emit("镜像未完成，改用原站重试；已下载文件保留。")
        self.emit("下载来源：原站")
        return self.run(args, label)

    def pip(self, python, *requirements, index=PYPI_OFFICIAL):
        args = [self.uv, "pip", "install", "--python", python, "--index-url", index, *requirements]
        mirror_args = [self.uv, "pip", "install", "--python", python, "--index-url", PYPI_MIRROR, *requirements] if index == PYPI_OFFICIAL else None
        self.run_with_mirror(args, "下载并安装运行依赖", mirror_args=mirror_args, source=PYPI_MIRROR)

    def ffmpeg(self):
        self.phase(0.05, "下载视频处理程序")
        self._download_range = (0.05, 0.8)
        base = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
        with urllib.request.urlopen(base + ".sha256", timeout=20) as response:
            checksum = response.read(1024).decode().split()[0].lower()
        if len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum):
            raise VoxlateError("FFmpeg 校验信息无效，请稍后重试")
        archive = download(base, self.cache / f"ffmpeg-{checksum[:12]}.zip", self.emit, checksum)
        self.phase(0.8, "解压视频处理程序")
        folder = self.cache / f"ffmpeg-{checksum[:12]}"
        safe_extract(archive, folder)
        for name in ("ffmpeg", "ffprobe"):
            source = next(folder.rglob(name + ".exe"), None)
            if source is None:
                raise VoxlateError(f"下载的资源缺少 {name}.exe")
            target = self.root / "tools/ffmpeg" / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            self.cfg[name] = str(target)
        # Keep the upstream notices/docs with the binaries, not only in a
        # deletable download cache. Do not redistribute this external tool.
        source_root = source.parent.parent if source.parent.name.lower() == 'bin' else source.parent
        if not source_root.resolve().is_relative_to(folder.resolve()):
            raise VoxlateError('FFmpeg 归档结构不正确，未复制额外文件。')
        notices = self.root / 'tools/ffmpeg/upstream'
        notices.mkdir(parents=True, exist_ok=True)
        for path in source_root.rglob('*'):
            if path.is_file() and (path.relative_to(source_root).parts[0].lower() == 'doc'
                    or re.match(r'(license|licence|copying|copyright|notice|readme)', path.name, re.I)):
                destination = notices / path.relative_to(source_root)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
        write_json(notices / 'DOWNLOAD.json', {'url': base, 'sha256': checksum,
                   'source_and_build_information': 'https://www.gyan.dev/ffmpeg/builds/'})

    def environment(self, kind):
        try:
            return self._prepare_environment(kind)
        finally:
            self._environment_activity = None

    def _prepare_environment(self, kind):
        self.phase(0.05, "准备安装工具")
        if kind == 'qwen':
            self.cfg.setdefault('qwen', {})['python'] = str(self.root/'.venv-qwen/Scripts/python.exe')
            python = self.venv(self.cfg['qwen']['python'])
            self._environment_activity = EnvironmentActivity(self.cache/'uv', Path(python).parent.parent,
                                                              STAGE_WEIGHTS[kind]*1_000_000)
            self.pip(python, 'torch==2.8.0', 'torchaudio==2.8.0',
                     index='https://download.pytorch.org/whl/'+('cpu' if self.cfg['asr']['device'] == 'cpu' else 'cu128'))
            self.pip(python, 'qwen-asr==0.0.6', 'numpy<2')
            return
        if kind == "tts":
            self.bootstrap()
            repo = Path(self.cfg["tts"]["repo_path"])
            if not (repo / "pyproject.toml").is_file():
                if repo.exists() and any(repo.iterdir()):
                    raise VoxlateError("IndexTTS 目录已有不完整文件，请在设置中选择新的空目录；原文件未修改")
                archive = download(f"https://codeload.github.com/index-tts/index-tts/zip/{TTS_REVISION}",
                                   self.cache / f"index-tts-{TTS_REVISION}.zip", self.emit)
                folder = self.cache / "index-tts-source"
                safe_extract(archive, folder)
                source = folder / f"index-tts-{TTS_REVISION}"
                repo.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source, repo, dirs_exist_ok=True)
            python = Path(self.cfg["tts"]["python"])
            if python.name.lower() != "python.exe" or python.parent.name.lower() != "scripts":
                raise VoxlateError("音色环境路径应为 <文件夹>/Scripts/python.exe")
            env = dict(self.env, UV_PROJECT_ENVIRONMENT=str(python.parent.parent))
            self.phase(0.2, "安装音色克隆环境")
            self._environment_activity = EnvironmentActivity(self.cache / "uv", python.parent.parent,
                                                              STAGE_WEIGHTS[kind] * 1_000_000)
            self.emit("音色克隆环境使用上游锁定的原站地址。")
            self.run([self.uv, "sync", "--frozen", "--no-dev", "--project", repo, "--python", "3.11"],
                     "安装 IndexTTS 音色克隆环境（体积较大）", env)
        else:
            python = self.venv(self.cfg[kind]["python"])
            self._environment_activity = EnvironmentActivity(self.cache / "uv", Path(python).parent.parent,
                                                              STAGE_WEIGHTS[kind] * 1_000_000)
            self.phase(0.2, "下载并安装运行依赖")
            if kind == "runtime":
                self.pip(python, "-r", bundle_root() / "requirements.txt")
            else:
                self._environment_activity.ceiling = 0.82
                index = "https://download.pytorch.org/whl/" + ("cpu" if self.cfg[kind]["device"] == "cpu" else "cu121")
                self.pip(python, "torch==2.5.1", "torchaudio==2.5.1", index=index)
                self._environment_activity.ceiling = 0.97
                self._environment_activity.fraction = 0.85
                self._environment_activity.operation = "准备人声分离依赖"
                self.phase(0.85, "安装人声分离依赖")
                self.pip(python, "demucs==4.0.1", "soundfile", "setuptools<81")

    def models(self, kind):
        self.phase(0.02, "准备模型下载环境")
        python = self.venv(self.root / "prepare-env/Scripts/python.exe")
        self.pip(python, "huggingface-hub>=0.28,<1")
        args = [python, "-u", external_code_root() / "scripts/prepare_models.py", kind, "--config", self.config_path,
                '--accept-resource-terms']
        self.phase(0.15, "下载" + TITLES[kind + "_model"])
        self._download_range = (0.15, 0.9)
        mirror_env = dict(self.env, HF_ENDPOINT=HF_MIRROR) if kind != "separator" else None
        self._model_activity = DownloadActivity(Path(self.cfg[kind]["model_path"]))
        try:
            self.run_with_mirror(args, f"下载{TITLES[kind + '_model']}", mirror_env=mirror_env, source=HF_MIRROR)
        finally:
            self._model_activity = None

    def native_translation(self, key):
        self._download_range = (0.02, 0.95)
        if key == "llm_engine":
            archive = download(ENGINE_URL, self.cache / "llama-b11157-vulkan.zip", self.emit, ENGINE_HASH)
            safe_extract(archive, self.root / "tools/llama")
            return
        item = MODELS[key]
        target = self.root / item["relative"] / item["filename"]
        preserve_model_terms(key, target.parent, self.emit)
        endpoints = [HF_MIRROR, HF_OFFICIAL] if self.cfg.get("try_mirrors", True) else [HF_OFFICIAL]
        for index, endpoint in enumerate(endpoints):
            check_cancelled()
            self.emit("模型下载来源：" + endpoint)
            try:
                download(f"{endpoint}/{item['repo']}/resolve/{item['revision']}/{item['filename']}", target, self.emit, item["sha256"])
                return
            except Exception:
                check_cancelled()
                if index == len(endpoints) - 1:
                    raise
                self.emit("镜像暂不可用，尝试原站；保留已有下载。")

    def recognition_model(self, key):
        def progress(start, end):
            self.phase(start * .95, "下载" + ASR_MODELS[key]["title"])
            self._download_range = (start * .95, end * .95)
        download_asr(key, self.root / ASR_MODELS[key]["relative"], self.emit,
                     mirrors=self.cfg.get("try_mirrors", True), progress=progress)

    def install(self, stages, *, accepted_terms=False):
        if not accepted_terms:
            raise VoxlateError('请先查看并确认第三方资源条款，再选择下载。')
        remember_root(self.config_path, self.root)
        self.cfg["resource_root"] = str(self.root)
        write_json(self.config_path, self.cfg)
        total = sum(STAGE_WEIGHTS[stage] for stage in stages) or 1
        completed = 0
        for number, stage in enumerate(stages, 1):
            check_cancelled()
            self._base = 95 * completed / total
            self._span = 95 * STAGE_WEIGHTS[stage] / total
            self._fraction = 0
            self._download_range = None
            self.phase(0, TITLES[stage])
            self.emit(f"[{number}/{len(stages)}] 正在准备：{TITLES[stage]}")
            try:
                if stage == "ffmpeg":
                    self.ffmpeg()
                elif stage == "llm_engine" or stage in MODELS:
                    self.native_translation(stage)
                elif stage in ASR_MODELS:
                    self.recognition_model(stage)
                elif stage.endswith("_model"):
                    self.models(stage.removesuffix("_model"))
                else:
                    self.environment(stage)
                write_json(self.config_path, self.cfg)
                self.phase(1, TITLES[stage] + "已准备")
                try:
                    size = folder_size(self.root / CATALOG[stage].relative)
                    self.progress({"percent": self._percent, "detail": TITLES[stage] + "已准备",
                                   "resource_key": stage, "resource_bytes": size})
                    self._emit(f"{TITLES[stage]}：实测 {format_bytes(size)}，正在继续后续步骤。")
                except OSError:
                    self._emit("暂时无法统计大小，全部准备结束后会重新统计。")
                completed += STAGE_WEIGHTS[stage]
            except Exception as exc:
                if "已取消" in str(exc):
                    raise
                raise VoxlateError(f"{TITLES[stage]}准备未完成：{exc}\n\n已完成的步骤和下载缓存保留。"
                                   "网络问题请检查 GitHub、PyPI、Hugging Face 的连接后点击按钮重试。"
                                   f"\n安装日志：{self.log_path}") from exc
        return self.cfg
