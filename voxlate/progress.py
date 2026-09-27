"""Small, local progress reports shared by the model worker and GUI process."""
import time
import re
from pathlib import Path

from .common import read_json, write_json


def duration(seconds):
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def separation_status(log, model, elapsed):
    """Demucs repeats tqdm for each submodel/shift; never label it total progress."""
    progress = '处理中'
    try:
        with Path(log).open('rb') as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell()-65536))
            text = stream.read().decode('utf-8', errors='replace')
        matches = re.findall(r'(?<!\d)(\d{1,3})%\|', text)
        if matches and 0 <= (percent := int(matches[-1])) <= 100:
            progress = f'本轮 {percent}%' if percent < 100 else '本轮完成，后续处理中'
    except OSError:
        pass
    return f'人声分离（{model}）· {progress} · 已用 {duration(elapsed)}'


def report_tts(path, phase, completed, total, current=None):
    if path:
        try:
            write_json(path, dict(phase=phase, completed=completed, total=total,
                                 current=current, since=time.time()))
        except OSError:
            pass  # A transient progress-file lock must not abort generated speech.


class TTSProgress:
    def __init__(self, path, log, total, cached=0, clock=time.time):
        self.path, self.log, self.clock = Path(path), Path(log), clock
        self.started = self.last_activity = clock()
        self.total, self.cached = total, cached
        self.state = dict(phase="loading", completed=cached, since=self.started)
        self.log_size = 0

    def snapshot(self):
        now = self.clock()
        try:
            state = read_json(self.path)
            if state.get("phase") in ("loading", "preparing", "generating", "completed"):
                self.state = state
        except (OSError, ValueError, AttributeError):
            pass
        try:
            size = self.log.stat().st_size
            if size != self.log_size:
                self.last_activity, self.log_size = now, size
        except OSError:
            pass
        state = self.state
        completed = max(self.cached, min(self.total, int(state.get("completed", self.cached))))
        percent = int(100 * completed / self.total) if self.total else 100
        detail = f"配音 {completed}/{self.total} 句（{percent}%）"
        if state["phase"] == "loading":
            detail += " · 正在加载音色克隆模型（IndexTTS 2.5）"
        elif state['phase'] == 'preparing':
            detail += ' · 正在准备参考音频（IndexTTS 2.5 已加载）'
        elif state["phase"] == "generating":
            detail += f" · 正在生成第 {state.get('current', '?')} 句"
        else:
            detail += " · 音频生成完成"
        detail += " · 已用 " + duration(now - self.started)
        if state["phase"] != "completed" and now - self.last_activity >= 30:
            detail += f" · {int(now - self.last_activity)} 秒无新日志"
        return dict(stage="tts", completed=completed, total=self.total, detail=detail)
