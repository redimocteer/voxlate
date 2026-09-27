"""Explicit, user-launched installation into the paths selected in the GUI."""
import argparse
from pathlib import Path
import subprocess
import sys
import shutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from voxlate.common import load_config

ROOT = Path(__file__).resolve().parents[1]


def run(*args, **kwargs):
    subprocess.run([str(a) for a in args], check=True, **kwargs)


def ensure_venv(python):
    python = Path(python)
    if python.exists():
        return python
    if python.name.lower() != "python.exe" or python.parent.name.lower() != "scripts":
        raise RuntimeError("新环境的路径需为 <环境文件夹>/Scripts/python.exe；请先在界面中调整路径")
    run(sys.executable, "-m", "venv", python.parent.parent)
    return python


def main():
    parser = argparse.ArgumentParser(description="voxlate 环境准备：此操作联网下载公共依赖及模型")
    parser.add_argument("step", choices=["runtime", "qwen", "separator", "tts", "models-asr", "models-translator", "models-separator", "models-tts"])
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    if sys.version_info[:2] not in ((3, 11), (3, 12)):
        raise RuntimeError("请使用 Python 3.11 或 3.12 运行安装工具")
    cfg = load_config(args.config)
    if args.step == 'qwen':
        from voxlate.installer import Installer
        Installer(cfg, args.config, print).install(['qwen'])
    elif args.step == "runtime":
        python = ensure_venv(cfg["runtime"]["python"])
        run(python, "-m", "pip", "install", "-r", ROOT / "requirements.txt")
    elif args.step == "separator":
        python = ensure_venv(cfg["separator"]["python"])
        run(python, "-m", "pip", "install", "torch==2.5.1", "torchaudio==2.5.1", "--index-url", "https://download.pytorch.org/whl/cu121")
        run(python, "-m", "pip", "install", "demucs==4.0.1", "soundfile")
    elif args.step == "tts":
        if not shutil.which("git"):
            raise RuntimeError("缺少 Git，请从 https://git-scm.com/download/win 安装后重新打开 PowerShell")
        repo = Path(cfg["tts"]["repo_path"])
        expected = repo / ".venv/Scripts/python.exe"
        if Path(cfg["tts"]["python"]).resolve() != expected.resolve():
            raise RuntimeError(f"自动安装使用 {expected}，请在 GUI 中选择该路径；已有外部环境请自行安装官方依赖")
        bootstrap = ensure_venv(Path(cfg["runtime"]["python"]))
        run(bootstrap, "-m", "pip", "install", "uv")
        if not repo.exists():
            repo.parent.mkdir(parents=True, exist_ok=True)
            run("git", "clone", "https://github.com/index-tts/index-tts.git", repo)
        if not (repo / "pyproject.toml").is_file():
            raise RuntimeError("源码目录不完整；请重新选择目录，不会覆盖现有文件")
        run(bootstrap, "-m", "uv", "sync", "--project", repo)
    else:
        kind = args.step.removeprefix("models-")
        base = Path(cfg["runtime"]["python"]).parent.parent.parent
        python = ensure_venv(base / "prepare-env/Scripts/python.exe")
        run(python, "-m", "pip", "install", "huggingface-hub>=0.28,<1")
        run(python, ROOT / "scripts/prepare_models.py", kind, "--config", args.config.resolve())
    print("准备完成。请返回 voxlate 点击「检查」。")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"准备失败：{exc}\n已下载文件保留。修复后重试本步骤。", file=sys.stderr)
        raise SystemExit(1)
