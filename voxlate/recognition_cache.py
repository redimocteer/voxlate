"""Validated, project-local checkpoints for Qwen recognition and alignment."""
import math
from pathlib import Path

from .common import digest, file_hash, model_stamp, read_json, write_json


class QwenCheckpoint:
    VERSION = 1

    def __init__(self, audio, config, directory, rate, total, emit):
        self.directory = Path(directory)
        self.transcript_path = self.directory / 'qwen_transcript.json'
        self.alignment_path = self.directory / 'qwen_alignment.json'
        self.state_path = self.directory / 'qwen_resume.json'
        self.key = digest(self.VERSION, file_hash(audio), config, model_stamp(config['model_path']), rate, total)
        self.chunks, self.alignments = [], []
        self.emit = emit
        if self.transcript_path.exists() or self.state_path.exists():
            try:
                state = read_json(self.state_path)
                chunks = read_json(self.transcript_path)
                if state['key'] != self.key or state['transcript'] != digest(chunks):
                    raise ValueError('checkpoint mismatch')
                self.validate_chunks(chunks, rate, total)
                self.chunks = chunks
                try:
                    alignments = read_json(self.alignment_path)
                    if state.get('alignment') != digest(alignments):
                        raise ValueError('alignment mismatch')
                    if not isinstance(alignments, list) or len(alignments) > len(chunks):
                        raise ValueError('invalid alignment')
                    for chunk, aligned in zip(chunks, alignments):
                        if aligned['chunk'] != chunk or not isinstance(aligned['words'], list):
                            raise ValueError('invalid alignment')
                        for word in aligned['words']:
                            if (not isinstance(word['text'], str) or
                                not all(type(word[k]) in (int, float) and math.isfinite(word[k]) for k in ('start_time', 'end_time')) or
                                not 0 <= word['start_time'] <= word['end_time'] <= chunk['end'] - chunk['start'] + .5):
                                raise ValueError('invalid word timestamp')
                    self.alignments = alignments
                except (OSError, ValueError, KeyError, TypeError):
                    emit('Qwen 对齐缓存不完整，将重新对齐；已识别文字保留。')
            except (OSError, ValueError, KeyError, TypeError):
                self.chunks, self.alignments = [], []
                emit('Qwen 缓存不匹配、损坏或缺少校验，本次从头识别。')
        if self.chunks:
            emit(f"Qwen 续识别：复用 {len(self.chunks)} 块，已到 {self.chunks[-1]['end']:.1f} 秒。")
        emit('Qwen 自动保存续跑进度；从头识别请先停止，再删除当前项目的 recognition 文件夹。')

    @staticmethod
    def validate_chunks(chunks, rate, total):
        if not isinstance(chunks, list):
            raise ValueError('invalid transcript')
        previous = 0
        for chunk in chunks:
            first, last = chunk['first_frame'], chunk['last_frame']
            if (type(first) is not int or type(last) is not int or
                first != previous or not first < last <= total or last - first > 30 * rate or
                not isinstance(chunk['text'], str)):
                raise ValueError('invalid chunk')
            if not all(type(chunk[k]) in (int, float) and math.isfinite(chunk[k]) for k in ('start', 'end')):
                raise ValueError('invalid time')
            if abs(chunk['start'] - first / rate) > 1e-6 or abs(chunk['end'] - last / rate) > 1e-6:
                raise ValueError('invalid boundary')
            previous = last

    def save(self):
        # Publish the commit record last. An interrupted write cannot make stale
        # text look compatible with new audio or a different model/configuration.
        write_json(self.transcript_path, self.chunks)
        write_json(self.alignment_path, self.alignments)
        write_json(self.state_path, dict(key=self.key, transcript=digest(self.chunks), alignment=digest(self.alignments)))
