"""Run imports and device checks using the actual selected interpreter."""
import importlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from voxlate.common import enable_offline


def main():
    enable_offline()
    try:
        job = json.load(sys.stdin)
        kind, cfg = job["kind"], job["config"]
        if sys.version_info < (3, 11):
            raise RuntimeError("运行环境需要 Python 3.11 或更新版本")
        if kind == "runtime":
            from voxlate.recognition_models import is_qwen
            from voxlate.asr import configure_cuda
            whisper = not is_qwen(cfg) or cfg['asr'].get('combined', False)
            if whisper:
                configure_cuda(cfg["asr"])
            if whisper and sys.platform == "win32" and cfg["asr"]["device"] == "cuda":
                import ctypes
                try:
                    cuda_libraries = [ctypes.WinDLL(name) for name in ("cublas64_12.dll", "cudnn_ops64_9.dll")]
                except OSError as exc:
                    raise RuntimeError("识别显卡依赖缺失：请准备音色克隆环境以共用 CUDA 库，或将识别设备设为 cpu、精度设为 int8。") from exc
            for module in ("faster_whisper", "ctranslate2", "sentencepiece", "sacremoses"):
                importlib.import_module(module)
            import ctranslate2
            for section in ("asr", "translator"):
                if section == 'asr' and is_qwen(cfg) and not cfg['asr'].get('combined', False):
                    continue
                if section == "translator" and cfg[section].get("model_type", "").startswith("hy_mt2"):
                    continue
                device = cfg[section]["device"]
                types = ctranslate2.get_supported_compute_types(device)
                if cfg[section]["compute_type"] not in types:
                    raise RuntimeError(f"{section} 的 {device} 不支持 {cfg[section]['compute_type']}；可选：{sorted(types)}")
        elif kind == 'qwen':
            import torch
            import qwen_asr
            import nagisa
            nagisa.wakati('日本語の確認です。')
            if cfg['asr']['device'] == 'cuda' and not torch.cuda.is_available():
                raise RuntimeError('Qwen 环境无法使用 CUDA，请重新安装环境或将识别设备设为 cpu。')
        else:
            if kind == "tts":
                repo = Path(cfg["tts"]["repo_path"])
                if not (repo / "indextts/infer_v2_5.py").is_file():
                    raise RuntimeError("IndexTTS-2.5 源码缺失，请选择正确源码目录或安装音色克隆环境")
                sys.path.insert(0, str(repo))
                importlib.import_module("indextts.infer_v2_5")
            else:
                importlib.import_module("demucs.separate")
            import torch
            device = cfg[kind]["device"]
            if device.startswith("cuda"):
                if not torch.cuda.is_available():
                    raise RuntimeError("CUDA 不可用：检查 NVIDIA 驱动以及该环境是否安装 CUDA 版 PyTorch")
                index = int(device.split(":")[1]) if ":" in device else 0
                if index >= torch.cuda.device_count():
                    raise RuntimeError(f"GPU {index} 不存在")
                if kind == "tts" and cfg[kind]["use_bf16"]:
                    with torch.cuda.device(index):
                        if not torch.cuda.is_bf16_supported():
                            raise RuntimeError("此 GPU 不支持 BF16，请在高级设置关闭 BF16")
            elif device != "cpu":
                raise RuntimeError("首版设备请选择 cpu、cuda 或 cuda:0")
        report = {"ready": True, "detail": "依赖导入和所选计算设备检查通过；模型加载将在首次处理时验证。"}
    except Exception as exc:
        report = {"ready": False, "detail": f"{type(exc).__name__}: {exc}"}
    print("VOXLATE_PROBE=" + json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
