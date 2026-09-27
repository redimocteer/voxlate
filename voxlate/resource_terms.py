"""Public upstream terms and pinned provenance; no user content is transmitted."""
from pathlib import Path
import hashlib
import json
import urllib.request

from .common import VoxlateError, write_json
from .media import check_cancelled

TTS_MODEL_REVISION = 'c39ce5ba981572cb187443877ff559dfb246ce63'
TTS_AUXILIARY = {
    'w2v': ('facebook/w2v-bert-2.0', 'da985ba0987f70aaeb84a80f2851cfac8c697a7b'),
    'bigvgan': ('nvidia/bigvgan_v2_22khz_80band_256x', '633ff708ed5b74903e86ff1298cf4a98e921c513'),
    'campplus': ('funasr/campplus', 'e4b6ede7ce16997aff4ae69fbca1f0175e2afede'),
}

def hf(repo, revision, name):
    return f'https://huggingface.co/{repo}/resolve/{revision}/{name}'

def github(repo, revision, name='LICENSE'):
    return f'https://raw.githubusercontent.com/{repo}/{revision}/{name}'

WHISPER_LICENSE = github('openai/whisper', '86098128c0b4f24f0e2aa2994de830614b474227')
QWEN_LICENSE = github('QwenLM/Qwen3-ASR', '7c6daf77a2421100f5fb066495372c00129d39ff')

def model_documents(key):
    """Keep model cards as attribution when upstream has no separate license."""
    if key in ('asr_large_model', 'asr_turbo_model'):
        from .recognition_models import MODELS
        model = MODELS[key]
        return {'MODEL_CARD.md': hf(model['repo'], model['revision'], 'README.md'),
                'Whisper-LICENSE.txt': WHISPER_LICENSE}
    if key == 'asr_qwen_model':
        from .qwen_manifest import PARTS
        return {'Qwen-LICENSE.txt': QWEN_LICENSE, **{
            f'{i+1}-MODEL_CARD.md': hf(p['repo'], p['revision'], 'README.md') for i, p in enumerate(PARTS)}}
    if key == 'hy7_model':
        from .translation_models import MODELS
        m = MODELS[key]
        return {n: hf(m['repo'], m['revision'], n) for n in ('LICENSE.txt', 'README.md')}
    if key == 'separator_model':
        rev = 'e976d93ecc3865e5757426930257e200846a520a'
        return {n: github('facebookresearch/demucs', rev, n) for n in ('LICENSE', 'README.md')}
    if key == 'tts_model':
        docs = {f'IndexTTS-{n}': hf('IndexTeam/IndexTTS-2.5', TTS_MODEL_REVISION, n)
                for n in ('LICENSE', 'README.md')}
        for name, (repo, rev) in TTS_AUXILIARY.items():
            docs[f'{name}-MODEL_CARD.md'] = hf(repo, rev, 'README.md')
        docs['BigVGAN-LICENSE.txt'] = hf(*TTS_AUXILIARY['bigvgan'], 'LICENSE')
        docs['w2v-BERT-MIT_LICENSE.txt'] = github('facebookresearch/seamless_communication',
            '9a081e935c29c6b1e0e05bcbf3fcb623ad306524', 'MIT_LICENSE')
        for name in ('LICENSE', 'MODEL_LICENSE'):
            docs[f'FunASR-{name}.txt'] = github('modelscope/FunASR',
                '2d4566d4a4c84d73e1f828efa2dedfa88f33677f', name)
        return docs
    return {}


def preserve_model_terms(key, directory, emit):
    """Fetch small official documents before weights; fail visibly if unavailable."""
    docs = model_documents(key)
    if not docs:
        return
    target = Path(directory) / 'licenses' / key
    target.mkdir(parents=True, exist_ok=True)
    manifest = target / 'SOURCES.json'
    try:
        previous = json.loads(manifest.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        previous = {}
    if not isinstance(previous, dict):
        previous = {}
    result = {}
    for name, url in docs.items():
        check_cancelled()
        path = target / name
        old = previous.get(name, {})
        if not isinstance(old, dict):
            old = {}
        if path.is_file() and old.get('url') == url and hashlib.sha256(path.read_bytes()).hexdigest() == old.get('sha256'):
            result[name] = old
            continue
        request = urllib.request.Request(url, headers={'User-Agent': 'Voxlate-resource-terms/1'})
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(2_000_001)
        if not data or len(data) > 2_000_000 or data.lstrip().lower().startswith((b'<!doctype html', b'<html')):
            raise VoxlateError('上游许可文件无效，已停止本项下载；请从官方来源核对。')
        data.decode('utf-8')
        partial = path.with_name(path.name + '.part')
        partial.write_bytes(data)
        partial.replace(path)
        result[name] = {'url': url, 'sha256': hashlib.sha256(data).hexdigest()}
    write_json(manifest, result)
    emit('已保存上游许可、模型说明与固定版本来源。')


RESOURCE_LINKS = {
    'ffmpeg': ('FFmpeg · 按实际构建适用 GPL/LGPL', 'https://www.gyan.dev/ffmpeg/builds/', 'https://ffmpeg.org/legal.html'),
    'runtime': ('识别环境 · 各依赖分别授权', 'https://github.com/SYSTRAN/faster-whisper', 'https://github.com/SYSTRAN/faster-whisper/blob/master/LICENSE'),
    'qwen': ('Qwen 识别环境 · Apache 2.0 及依赖许可', 'https://github.com/QwenLM/Qwen3-ASR', QWEN_LICENSE),
    'separator': ('Demucs 环境 · MIT 及依赖许可', 'https://github.com/facebookresearch/demucs', 'https://github.com/facebookresearch/demucs/blob/main/LICENSE'),
    'tts': ('IndexTTS 环境 · 专用协议及依赖许可', 'https://github.com/index-tts/index-tts', 'https://github.com/index-tts/index-tts/blob/ee40fa7d6c6b8a2c7f06105f9f1e65775b74868c/LICENSE'),
    'llm_engine': ('llama.cpp · MIT 及随附运行库许可', 'https://github.com/ggml-org/llama.cpp/releases/tag/b11157', 'https://github.com/ggml-org/llama.cpp/blob/b11157/LICENSE'),
    'asr_large_model': ('Whisper large-v3 · MIT', 'https://huggingface.co/Systran/faster-whisper-large-v3', WHISPER_LICENSE),
    'asr_turbo_model': ('Whisper turbo · MIT', 'https://huggingface.co/dropbox-dash/faster-whisper-large-v3-turbo', WHISPER_LICENSE),
    'asr_qwen_model': ('Qwen3-ASR + 对齐模型 · Apache 2.0', 'https://huggingface.co/Qwen/Qwen3-ASR-1.7B', QWEN_LICENSE),
    'hy7_model': ('Hy-MT2 7B · Apache 2.0', 'https://huggingface.co/tencent/Hy-MT2-7B-GGUF', hf('tencent/Hy-MT2-7B-GGUF', 'ab8472660ac61fac25f1af43fac2599d52a8a775', 'LICENSE.txt')),
    'separator_model': ('Demucs htdemucs · MIT', 'https://github.com/facebookresearch/demucs', 'https://github.com/facebookresearch/demucs/blob/main/LICENSE'),
    'tts_model': ('IndexTTS 2.5 + 辅助模型 · 分别授权', 'https://huggingface.co/IndexTeam/IndexTTS-2.5', hf('IndexTeam/IndexTTS-2.5', TTS_MODEL_REVISION, 'LICENSE')),
}

def require_cli_consent(parser, accepted, stages):
    if accepted:
        return
    for stage in stages:
        if stage in RESOURCE_LINKS:
            name, source, terms = RESOURCE_LINKS[stage]
            print(f'{name}\n来源：{source}\n许可：{terms}')
    parser.error('请先阅读 docs/RESOURCE_DOWNLOADS.md 及所选资源的上游条款，再加 --accept-resource-terms 自行确认下载。')
