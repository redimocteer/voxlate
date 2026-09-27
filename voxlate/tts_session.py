"""One local TTS worker retained by a GUI window, scoped to one project."""
from pathlib import Path
import subprocess
import time

from .common import digest, model_stamp, read_json, VoxlateError
from .runtime import worker_command, spawn_external, external_env
from .project_storage import worker_environment


class TTSJob:
    def __init__(self, session, done):
        self.session, self.done = session, done
        self.process = session.process

    def wait(self, timeout=None):
        deadline = time.monotonic() + (timeout or 0)
        while True:
            if self.done.is_file():
                return 0 if read_json(self.done).get('ok') else 1
            code = self.process.poll()
            if code is not None:
                return code or 1
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired('tts', timeout)
            time.sleep(min(.05, max(0, deadline-time.monotonic())))

    def terminate(self):
        self.session.close('停止配音')

    kill = terminate


class TTSSession:
    def __init__(self, on_event=None):
        self.process = self.stream = self.key = None
        self.on_event, self.log = on_event, None

    @property
    def is_alive(self):
        process = self.process
        return process is not None and process.poll() is None

    def close(self, reason='关闭会话'):
        process, self.process = self.process, None
        self.key = None
        if process:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            if process.stdin:
                process.stdin.close()
        if self.stream:
            self.stream.close()
            self.stream = None
        if process:
            message = f'已释放音色克隆模型（IndexTTS 2.5）：{reason}'
            if self.log:
                try:
                    with self.log.with_name('run.log').open('a', encoding='utf-8') as stream:
                        stream.write(time.strftime('%Y-%m-%d %H:%M:%S ') + message + '\n')
                except OSError:
                    pass
            if self.on_event:
                self.on_event(message)

    def submit(self, cfg, request, log):
        request, log = Path(request).resolve(), Path(log).resolve()
        key = digest(str(request.parent), cfg['tts'], model_stamp(cfg['tts']['model_path']))
        reused = self.process is not None and self.process.poll() is None and self.key == key
        if not reused:
            self.close('项目或音色克隆设置已变化' if self.process and self.process.poll() is None else '进程已退出')
        self.log = log
        done = request.with_suffix('.done.json')
        done.unlink(missing_ok=True)
        try:
            if reused:
                self.process.stdin.write(str(request) + '\n')
                self.process.stdin.flush()
            else:
                self.stream = log.open('a', encoding='utf-8')
                self.process = spawn_external(worker_command(cfg, 'tts', request) + ['--serve'],
                    stdout=self.stream, stderr=subprocess.STDOUT, stdin=subprocess.PIPE,
                    text=True, encoding='utf-8', cwd=request.parent,
                    env=worker_environment(request.parent, external_env()))
                self.key = key
        except (OSError, ValueError) as exc:
            self.close('音色克隆进程异常')
            raise VoxlateError('配音环境无法启动或已退出，请重试配音。') from exc
        return TTSJob(self, done), reused
