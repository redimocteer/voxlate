from __future__ import annotations

import json
import logging
import math
from pathlib import Path
import subprocess
import wave
import threading

from .common import VoxlateError
from .runtime import spawn_external

_local = threading.local()


def set_cancel_event(event):
    _local.cancel_event = event


def check_cancelled():
    if getattr(_local, "cancel_event", None) is not None and _local.cancel_event.is_set():
        raise VoxlateError("已取消。已完成结果保留，可继续处理。")


def run_process(args, label, **kwargs):
    try:
        check_cancelled()
        process = spawn_external([str(x) for x in args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace",
                                   **kwargs)
        try:
            while True:
                try:
                    stdout, stderr = process.communicate(timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    check_cancelled()
        except BaseException:
            process.kill()
            process.communicate()
            raise
    except FileNotFoundError as exc:
        raise VoxlateError(f"{label}：程序不存在，请检查配置和 PATH") from exc
    if process.returncode:
        raise VoxlateError(f"{label}失败：\n{(stderr or stdout)[-3500:]}")
    return stdout


def tempo_filter(ratio):
    if not math.isfinite(ratio) or ratio <= 0:
        raise VoxlateError("无效的音频时长比")
    factors = []
    while ratio > 2:
        factors.append(2.0)
        ratio /= 2
    while ratio < 0.5:
        factors.append(0.5)
        ratio /= 0.5
    factors.append(ratio)
    return ",".join(f"atempo={x:.10f}" for x in factors)


class Media:
    def __init__(self, cfg):
        self.ffmpeg = cfg["ffmpeg"]
        self.ffprobe = cfg["ffprobe"]
        self.sample_rate = cfg["audio"]["sample_rate"]

    def probe(self, path):
        return json.loads(run_process([self.ffprobe, "-v", "error", "-show_streams",
                                     "-show_format", "-of", "json", Path(path).resolve()], "读取媒体信息"))

    def duration(self, path):
        info = self.probe(path)
        value = float(info["format"]["duration"])
        if not math.isfinite(value) or value <= 0:
            raise VoxlateError("媒体时长无效")
        return value

    def render(self, args, output):
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        temp = output.with_name(output.stem + ".partial" + output.suffix)
        run_process([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                     *args, temp], "FFmpeg")
        temp.replace(output)

    def extract(self, video, output, duration, audio_track=0):
        # Resample with timestamps to preserve initial delays/gaps relative to video.
        from .audio_tracks import selected_track
        audio_track = selected_track({'audio_track': audio_track})
        self.render(["-i", video, "-map", f"0:a:{audio_track}", "-vn", "-af",
                     "aresample=44100:async=1:first_pts=0,apad", "-t", str(duration),
                     "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le"], output)

    def trim(self, source, output, start, duration):
        self.render(["-i", source, "-ss", str(start), "-t", str(duration),
                     "-ac", "1", "-ar", str(self.sample_rate), "-c:a", "pcm_s16le"], output)

    def align(self, source, output, duration):
        generated = self.duration(source)
        ratio = generated / duration
        # Very short utterances keep their pace and are padded with silence.
        pace = max(0.85, ratio)
        filters = f"{tempo_filter(pace)},apad,atrim=duration={duration:.9f},asetpts=N/SR/TB"
        self.render(["-i", source, "-af", filters, "-ac", "1", "-ar", str(self.sample_rate),
                     "-c:a", "pcm_s16le"], output)
        return generated, ratio

    def mix(self, background, dubbing, output, duration, background_gain, dubbing_gain,
            *, original=None, original_intervals=()):
        filters = (f"[0:a]volume={background_gain}[bg];[1:a]volume={dubbing_gain}[dub];"
                   "[bg][dub]amix=inputs=2:duration=longest:normalize=0,"
                   "alimiter=limit=0.95:level=0:latency=1,apad[mix]")
        self.render(["-i", background, "-i", dubbing, "-filter_complex", filters,
                     "-map", "[mix]", "-t", str(duration), "-ac", "2", "-ar", "44100",
                     "-c:a", "pcm_s16le"], output)
        if original is not None and original_intervals:
            restore_original_intervals(output, original, original_intervals)

    def mux(self, video, audio, output, duration, original_audio=None):
        inputs = ["-i", video, "-i", audio]
        if original_audio:
            inputs += ["-i", original_audio]
        inputs += ["-map", "0:V:0", "-map", "1:a:0"]
        if original_audio:
            inputs += ["-map", "2:a:0", "-metadata:s:a:0", "title=中文配音",
                       "-metadata:s:a:0", "handler_name=Voxlate Dub",
                       "-metadata:s:a:0", "language=zho", "-disposition:a:0", "default",
                       "-metadata:s:a:1", "title=原声", "-metadata:s:a:1", "handler_name=Voxlate Original",
                       "-disposition:a:1", "0"]
        audio_options = ["-c:a", "aac", "-b:a", "192k", "-t", str(duration), "-movflags", "+faststart"]
        try:
            self.render([*inputs, "-c:v", "copy", *audio_options], output)
        except VoxlateError as exc:
            # Only retry container/codec incompatibility, never cancellation or disk errors.
            if not any(message in str(exc) for message in (
                    "Could not find tag for codec", "codec not currently supported in container")):
                raise
            logging.getLogger("voxlate").info("原视频编码不支持 MP4，正在转为 H.264；配音无需重新生成…")
            self.render([*inputs, "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                         "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-pix_fmt", "yuv420p",
                         *audio_options], output)


def original_audio_intervals(segments, duration):
    """Keep original audio everywhere outside selected dubbing sentence windows."""
    if not math.isfinite(duration) or duration <= 0:
        raise VoxlateError('媒体时长无效')
    selected = []
    for segment in segments:
        if not segment.get('enabled', True):
            continue
        start, end = segment['start'], segment['end']
        if not all(math.isfinite(t) for t in (start, end)) or start < 0 or end <= start or end > duration:
            raise VoxlateError('配音时间范围无效')
        selected.append((start, end))
    ranges, cursor = [], 0.
    for start, end in sorted(selected):
        if start > cursor:
            ranges.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        ranges.append((cursor, duration))
    return ranges


def restore_original_intervals(output, original, intervals):
    """Replace the entire mixed signal with original PCM at absolute positions.

    Copy both stereo channels without gain, resampling or separation; bounded
    chunks keep memory use independent of video length.
    """
    output = Path(output)
    temp = output.with_name(output.stem + '.partial.wav')
    with wave.open(str(output), 'rb') as mixed, wave.open(str(original), 'rb') as source:
        fmt = lambda audio: (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype())
        if fmt(mixed) != fmt(source) or mixed.getcomptype() != 'NONE':
            raise VoxlateError('原音轨与混音格式不匹配，请重新提取音轨')
        rate, total = mixed.getframerate(), mixed.getnframes()
        ranges = [(max(0, round(start*rate)), min(total, round(end*rate))) for start, end in intervals]
        ranges = [(start, end) for start, end in ranges if end > start]
        ranges.sort()
        if any(end <= start or end > source.getnframes() for start, end in ranges):
            raise VoxlateError('保留原声的片段超出原音轨范围')
        merged = []
        for start, end in ranges:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        with wave.open(str(temp), 'wb') as dest:
            dest.setparams(mixed.getparams())
            def copy_frames(audio, start, end):
                audio.setpos(start)
                remaining = end-start
                frame_bytes = audio.getnchannels()*audio.getsampwidth()
                while remaining:
                    check_cancelled()
                    frames = audio.readframes(min(remaining, rate))
                    if not frames:
                        raise VoxlateError('音轨数据不完整，请重新提取音轨')
                    dest.writeframesraw(frames)
                    remaining -= len(frames)//frame_bytes
            cursor = 0
            for start, end in merged:
                copy_frames(mixed, cursor, start)
                copy_frames(source, start, end)
                cursor = end
            copy_frames(mixed, cursor, total)
    temp.replace(output)


def build_timeline(segments, output, duration, sample_rate):
    """Stream PCM + silence; memory usage is independent of video length."""
    total = round(duration * sample_rate)
    cursor = 0
    output = Path(output)
    temp = output.with_suffix(".partial.wav")
    with wave.open(str(temp), "wb") as dst:
        dst.setparams((1, 2, sample_rate, 0, "NONE", "not compressed"))

        def silence(count):
            while count > 0:
                size = min(count, sample_rate)
                dst.writeframesraw(b"\0\0" * size)
                count -= size

        for seg in segments:
            start = round(seg["start"] * sample_rate)
            end = min(total, round(seg["end"] * sample_rate))
            if start < cursor or end <= start:
                raise VoxlateError("时间轴存在重叠或无效片段")
            silence(start - cursor)
            remaining = end - start
            with wave.open(str(seg["aligned_audio"]), "rb") as src:
                if (src.getnchannels(), src.getsampwidth(), src.getframerate()) != (1, 2, sample_rate):
                    raise VoxlateError("对齐音频必须为单声道 PCM16")
                while remaining:
                    frames = src.readframes(min(remaining, sample_rate))
                    if not frames:
                        break
                    dst.writeframesraw(frames)
                    remaining -= len(frames) // 2
            silence(remaining)
            cursor = end
        silence(total - cursor)
    temp.replace(output)
