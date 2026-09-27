"""Offline CAMPPlus grouping. Scores indicate similarity, not audio quality."""
import math
import sys
import uuid
import wave
from pathlib import Path
from .common import VoxlateError, write_json
from .roles import MAX_ROLES, automatic_role_name
from .model_lifecycle import model_event


def unit(vector):
    norm = math.sqrt(sum(x*x for x in vector))
    return [x/max(norm, 1e-12) for x in vector]


def similarity(a, b):
    return sum(x*y for x, y in zip(a, b))


def group_records(records, threshold=.68):
    """Bounded centroid clustering; short utterances do not create new speakers."""
    if not records:
        raise VoxlateError('没有可用于分组的对白。')
    usable = [r for r in records if r.get('embedding')]
    if not usable:
        raise VoxlateError('人声音频过短或接近静音，请手动分配角色。')
    seeds = [r for r in usable if r['end']-r['start'] >= 1 and not r.get('unstable')]
    seeds = seeds or [max(usable, key=lambda r: r['end']-r['start'])]
    clusters = []
    membership = {}
    for record in sorted(seeds, key=lambda r: r.get('quality', 0), reverse=True):
        emb = unit(record['embedding'])
        scores = [similarity(emb, c['center']) for c in clusters]
        best = max(range(len(scores)), key=scores.__getitem__) if scores else None
        if best is None or (scores[best] < threshold and len(clusters) < MAX_ROLES):
            best = len(clusters)
            clusters.append(dict(center=emb, records=[]))
        cluster = clusters[best]
        cluster['records'].append(record)
        cluster['center'] = unit([sum(r['embedding'][i] for r in cluster['records']) for i in range(len(emb))])
        membership[record['id']] = best
    for record in records:
        if record['id'] not in membership:
            emb = record.get('embedding')
            membership[record['id']] = max(range(len(clusters)), key=lambda i: similarity(emb, clusters[i]['center'])) if emb else 0
    order = sorted(range(len(clusters)), key=lambda i: min(r['start'] for r in records if membership[r['id']] == i))
    roles, assignments = [], {}
    for number, index in enumerate(order):
        members = [r for r in records if membership[r['id']] == index]
        center = clusters[index]['center']
        candidates = [r for r in members if r.get('embedding') and not r.get('unstable') and r['end']-r['start'] >= 1]
        candidates = candidates or [r for r in members if r.get('embedding')]
        reference = max(candidates, key=lambda r: .75*similarity(r['embedding'], center)+.25*r.get('quality', 0))
        ident = 'role_'+uuid.uuid4().hex[:12]
        roles.append(dict(id=ident, name=automatic_role_name(number), reference_sentence_id=reference['id']))
        for record in members:
            emb = record.get('embedding')
            scores = sorted([similarity(emb, c['center']) for c in clusters], reverse=True) if emb else [0]
            uncertain = (not emb or record.get('unstable', False) or record['end']-record['start'] < 1
                         or scores[0] < threshold or len(scores) > 1 and scores[0]-scores[1] < .08)
            assignments[str(record['id'])] = dict(role_id=ident, role_uncertain=bool(uncertain))
    # Select the speaker with most estimated speaking time, then its representative clip.
    speaking_time = {role['id']: 0.0 for role in roles}
    for record in usable:
        role_id = assignments[str(record['id'])]['role_id']
        speaking_time[role_id] += (record['end']-record['start'])*record.get('voiced_fraction', 1.0)
    dominant = max(roles, key=lambda role: speaking_time[role['id']])
    return dict(roles=roles, assignments=assignments,
                recommended_sentence_id=dominant['reference_sentence_id'])


def analyze_speakers(audio, segments, cfg, directory):
    import numpy as np
    import torch
    import torchaudio
    repo = Path(cfg['repo_path']).resolve()
    sys.path.insert(0, str(repo))
    from indextts.s2mel.modules.campplus.DTDNN import CAMPPlus
    weights = Path(cfg['model_path'])/'hf_cache'/'campplus_cn_common.bin'
    if not weights.is_file():
        raise VoxlateError('缺少 CAMPPlus 文件，请在资源配置中补全音色克隆模型。')
    torch.set_num_threads(4)
    model_event(directory, '正在加载角色分组模型（CAMPPlus，CPU）')
    model = CAMPPlus(feat_dim=80, embedding_size=192)
    model.load_state_dict(torch.load(weights, map_location='cpu', weights_only=True))
    model.eval()
    model_event(directory, '角色分组模型已加载（CAMPPlus）')
    records = []
    with wave.open(str(audio), 'rb') as source, torch.inference_mode():
        if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, 16000):
            raise VoxlateError('角色分组需要 16kHz 单声道人声。')
        for index, segment in enumerate(segments):
            start, duration = segment['start'], segment['end']-segment['start']
            record = dict(segment, embedding=None, quality=0, unstable=True)
            source.setpos(min(round(start*16000), source.getnframes()))
            samples = np.frombuffer(source.readframes(round(min(duration, 12)*16000)), dtype='<i2').astype(np.float32)/32768
            if len(samples) >= 1600 and float(np.sqrt(np.mean(samples**2))) > .0005:
                # Compare up to three windows to flag unstable or mixed segments.
                chunk_size = min(len(samples), 64000)
                offsets = sorted(set(int(x) for x in np.linspace(0, max(0, len(samples)-chunk_size), min(3, max(1, len(samples)//32000)))))
                embeddings = []
                for offset in offsets:
                    chunk = samples[offset:offset+chunk_size]
                    if len(chunk) < 8000:
                        chunk = np.pad(chunk, (0, 8000-len(chunk)), mode='wrap')
                    feature = torchaudio.compliance.kaldi.fbank(torch.from_numpy(chunk).unsqueeze(0),
                        num_mel_bins=80, dither=0, sample_frequency=16000)
                    feature -= feature.mean(dim=0, keepdim=True)
                    embeddings.append(unit(model(feature.unsqueeze(0)).squeeze(0).tolist()))
                embedding = unit([sum(v[i] for v in embeddings) for i in range(192)])
                unstable = any(similarity(a, b) < .55 for a in embeddings for b in embeddings)
                frame = samples[:len(samples)//320*320].reshape(-1, 320)
                rms = np.sqrt(np.mean(frame**2, axis=1))
                voiced = float(np.mean(rms > max(.003, float(rms.max())*.08)))
                clipping = float(np.mean(np.abs(samples) > .98))
                # Prefer enough usable speech; very long clips are truncated by TTS.
                quality = .6*min(duration*voiced/4, 1)+.4*voiced-3*clipping-.2*min(max(duration-12, 0)/12, 1)
                record.update(embedding=embedding, quality=quality, unstable=unstable,
                              voiced_fraction=voiced, clipping_fraction=clipping)
            records.append(record)
            if index == 0 or (index+1) % 10 == 0 or index+1 == len(segments):
                model_event(directory, f'角色分组：已分析 {index+1}/{len(segments)} 句')
    result = group_records(records)
    target = Path(directory)/'recognition'
    target.mkdir(exist_ok=True)
    write_json(target/'speakers.json', dict(version=1, threshold=.68, records=records, result=result))
    return result
