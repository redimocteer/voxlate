from pathlib import Path
import sys
import wave

from .common import VoxlateError


def release_idle_cuda_memory(torch, device):
    """Bound unused allocator cache between utterances without unloading models."""
    if not str(device).startswith("cuda"):
        return
    allocated = torch.cuda.memory_allocated(device)
    reserved = torch.cuda.memory_reserved(device)
    free, _ = torch.cuda.mem_get_info(device)
    if reserved - allocated >= 512 * 1024**2 or free < 1024**3:
        with torch.cuda.device(device):
            torch.cuda.empty_cache()
        print(f"显存：模型与张量 {allocated / 1024**3:.2f} GB；"
              f"释放闲置缓存后保留 {torch.cuda.memory_reserved(device) / 1024**3:.2f} GB", flush=True)


def valid_wav(path):
    try:
        with wave.open(str(path), "rb") as audio:
            frames = audio.getnframes()
            if frames <= 0:
                return False
            # Read in blocks, checking the data chunk is not truncated.
            remaining = frames
            stride = audio.getnchannels() * audio.getsampwidth()
            while remaining:
                count = min(remaining, 65536)
                if len(audio.readframes(count)) != count * stride:
                    return False
                remaining -= count
            return True
    except (OSError, EOFError, wave.Error):
        return False


class TTSEngine:
    def __init__(self, config):
        sys.path.insert(0, config["repo_path"])
        import torch
        from indextts.infer_v2_5 import IndexTTS2

        self.torch = torch
        self.device = config["device"]
        self.model = IndexTTS2(cfg_path=str(Path(config["model_path"]) / "config.yaml"),
                               model_dir=config["model_path"], device=config["device"],
                               use_bf16=config["use_bf16"], use_cuda_kernel=False,
                               use_deepspeed=False, use_qwen_emo=False)
        release_idle_cuda_memory(self.torch, self.device)

    def reset_reference_cache(self):
        # IndexTTS caches by path, while speaker_A.wav can be replaced in place.
        for name in ('cache_spk_cond', 'cache_s2mel_style', 'cache_s2mel_prompt',
                     'cache_spk_audio_prompt', 'cache_emo_cond', 'cache_emo_audio_prompt', 'cache_mel'):
            setattr(self.model, name, None)
        release_idle_cuda_memory(self.torch, self.device)

    def generate(self, text, speaker_reference, output, emotion_reference=None, duration_factor=1.0):
        if not isinstance(duration_factor, (int, float)) or not .5 <= duration_factor <= 2:
            raise VoxlateError('合成时长比例须在 0.5～2 之间')
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(".partial.wav")
        self.model.infer(text=text, spk_audio_prompt=str(speaker_reference),
                         emo_audio_prompt=str(emotion_reference) if emotion_reference else None,
                         output_path=str(temporary), lang="ZH", verbose=False,
                         use_random=False, interval_silence=100, duration_factor=duration_factor)
        if not valid_wav(temporary):
            raise VoxlateError("TTS 未生成有效音频")
        temporary.replace(output)
        release_idle_cuda_memory(self.torch, self.device)
        return str(output)
