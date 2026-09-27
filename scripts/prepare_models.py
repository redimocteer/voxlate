"""Explicit online preparation. Never called by the offline dubbing pipeline."""
import argparse
import hashlib
from pathlib import Path
import sys
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from voxlate.common import load_config
from voxlate.installer import download
from voxlate.resource_terms import preserve_model_terms, require_cli_consent, TTS_MODEL_REVISION, TTS_AUXILIARY


def prepare_asr(cfg):
    from voxlate.recognition_models import selected_key, download_model
    download_model(selected_key(cfg), cfg["asr"]["model_path"], lambda s: print(s, flush=True),
                   mirrors=cfg.get("try_mirrors", True))


def prepare_translator(cfg):
    if cfg["translator"].get("model_type", "").startswith("hy_mt2"):
        from voxlate.translation_models import MODELS, selected_key
        import os
        item = MODELS[selected_key(cfg)]
        preserve_model_terms(selected_key(cfg), cfg['translator']['model_path'], print)
        endpoint = os.environ.get("HF_ENDPOINT", "https://huggingface.co")
        download(f"{endpoint}/{item['repo']}/resolve/{item['revision']}/{item['filename']}",
                 Path(cfg["translator"]["model_path"]) / item["filename"],
                 lambda message: print(message, flush=True), item["sha256"])
        return
    from huggingface_hub import snapshot_download
    from voxlate.model_downloads import TRANSLATION_REPO, TRANSLATION_REVISION, TRANSLATION_FILES, TRANSLATION_HASHES
    snapshot_download(TRANSLATION_REPO, revision=TRANSLATION_REVISION,
                      local_dir=cfg["translator"]["model_path"], allow_patterns=TRANSLATION_FILES)
    for name, expected in TRANSLATION_HASHES.items():
        with (Path(cfg["translator"]["model_path"]) / name).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise RuntimeError(f"{name} 校验失败，请删除翻译模型后重新下载。")
    print("直译模型已下载，无需转换。")


def prepare_separator(cfg):
    target = Path(cfg["separator"]["model_path"])
    if cfg["separator"]["model"] != "htdemucs":
        raise RuntimeError("准备脚本仅支持 htdemucs；自选模型请手动放入本地仓库。")
    preserve_model_terms('separator_model', target, print)
    target.mkdir(parents=True, exist_ok=True)
    filename = "955717e8-8726e21a.th"
    output = target / filename
    checksum = ""
    if output.is_file():
        with output.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    if not checksum.startswith("8726e21a"):
        download("https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/" + filename,
                 output, lambda message: print(message, flush=True), "8726e21a")
    (target / "htdemucs.yaml").write_text("models: ['955717e8']\n", encoding="utf-8")


def prepare_tts(cfg):
    from huggingface_hub import hf_hub_download, snapshot_download
    target = Path(cfg["tts"]["model_path"])
    preserve_model_terms('tts_model', target, print)
    snapshot_download("IndexTeam/IndexTTS-2.5", revision=TTS_MODEL_REVISION, local_dir=str(target),
                      ignore_patterns=["qwen0.6bemo4-merge/*", ".gitattributes"])
    auxiliary = target / "hf_cache"
    snapshot_download(TTS_AUXILIARY['w2v'][0], revision=TTS_AUXILIARY['w2v'][1], local_dir=str(auxiliary / "w2v-bert-2.0"),
                      allow_patterns=["config.json", "preprocessor_config.json", "model.safetensors"])
    hf_hub_download(TTS_AUXILIARY['campplus'][0], 'campplus_cn_common.bin', revision=TTS_AUXILIARY['campplus'][1], local_dir=str(auxiliary))
    snapshot_download(TTS_AUXILIARY['bigvgan'][0], revision=TTS_AUXILIARY['bigvgan'][1], local_dir=str(auxiliary / "bigvgan"),
                      allow_patterns=["config.json", "bigvgan_generator.pt"])


def main():
    parser = argparse.ArgumentParser(description="只下载公共模型，不读取或上传用户视频")
    parser.add_argument("stage", choices=["asr", "translator", "separator", "tts", "all"])
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "config.json")
    parser.add_argument('--accept-resource-terms', action='store_true', help='阅读上游条款后，自行确认下载第三方资源')
    args = parser.parse_args()
    cfg = load_config(args.config)
    stages = ["asr", "translator", "separator", "tts"] if args.stage == "all" else [args.stage]
    if 'translator' in stages and not cfg['translator'].get('model_type', '').startswith('hy_mt2'):
        parser.error('当前下载入口只提供 Hy-MT2；旧版或自选翻译模型请自行核对许可并准备。')
    from voxlate.recognition_models import selected_key as asr_key
    from voxlate.translation_models import selected_key as translation_key
    names = dict(asr=asr_key(cfg), translator=translation_key(cfg), separator='separator_model', tts='tts_model')
    require_cli_consent(parser, args.accept_resource_terms, [names[s] for s in stages])
    functions = {"asr": prepare_asr, "translator": prepare_translator,
                 "separator": prepare_separator, "tts": prepare_tts}
    for stage in stages:
        print(f"准备 {stage} 模型（需要联网）", flush=True)
        functions[stage](cfg)


if __name__ == "__main__":
    main()
