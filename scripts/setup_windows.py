"""Explicit, user-launched installation into the paths selected in the GUI."""
import argparse
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from voxlate.common import load_config
from voxlate.installer import Installer
from voxlate.resource_terms import require_cli_consent
from voxlate.recognition_models import selected_key as asr_key
from voxlate.translation_models import selected_key as translation_key

def main():
    parser = argparse.ArgumentParser(description="voxlate 环境准备：此操作联网下载公共依赖及模型")
    parser.add_argument("step", choices=["runtime", "qwen", "separator", "tts", "models-asr", "models-translator", "models-separator", "models-tts"])
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument('--accept-resource-terms', action='store_true', help='阅读所选资源条款后确认下载')
    args = parser.parse_args()
    if sys.version_info[:2] not in ((3, 11), (3, 12)):
        raise RuntimeError("请使用 Python 3.11 或 3.12 运行安装工具")
    cfg = load_config(args.config)
    stage = {'models-asr': asr_key(cfg), 'models-translator': translation_key(cfg),
             'models-separator': 'separator_model', 'models-tts': 'tts_model'}.get(args.step, args.step)
    require_cli_consent(parser, args.accept_resource_terms, [stage])
    Installer(cfg, args.config, print).install([stage], accepted_terms=True)
    print("准备完成。请返回 voxlate 点击「检查」。")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"准备失败：{exc}\n已下载文件保留。修复后重试本步骤。", file=sys.stderr)
        raise SystemExit(1)
