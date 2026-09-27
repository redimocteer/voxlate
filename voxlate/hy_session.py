"""One offline native model per translation task; independent batch contexts."""
import http.client
import json
import re
import secrets
import socket
import subprocess
import time
from pathlib import Path

from .common import VoxlateError, local_inference_connection
from .model_lifecycle import model_event, model_name
from .runtime import external_env, spawn_external


class HySession:
    def __init__(self, cfg, model, directory, work_dir):
        self.cfg, self.model = cfg, model
        self.directory, self.work_dir = Path(directory), work_dir
        self.name = model_name('translator', cfg)
        self.process = self.log = None
        self.cleanup = lambda: None
        self.port = None
        self.key = secrets.token_urlsafe(32)

    def __enter__(self):
        # Reuse the complete pinned llama.cpp bundle already installed by voxlate.
        engine = Path(self.cfg['engine_path']).with_name('llama-server.exe')
        if not engine.is_file():
            raise VoxlateError('翻译运行程序不完整，请在资源配置中重新安装「翻译运行程序」。')
        key_path = self.directory/'session.key'
        key_path.write_text(self.key, encoding='utf-8')
        self.log = (self.directory/'stderr.log').open('w+', encoding='utf-8')
        args = [str(engine), '-m', str(self.model), '--offline', '--jinja', '--no-warmup',
            # This native option uses a narrow Windows file API. The ASCII name
            # resolves in our project-local Unicode cwd, including mixed scripts.
            '--host', '127.0.0.1', '--port', '0', '--api-key-file', key_path.name,
            '--no-webui', '--no-slots', '--no-cache-prompt', '-np', '1',
            '-c', '8192', '-n', '4096', '-b', '256', '-ub', '128',
            '-t', str(self.cfg.get('cpu_threads', 4)), '-ngl', '0' if self.cfg.get('device') == 'cpu' else '99']
        env = {k:v for k,v in external_env().items() if not k.startswith('LLAMA_')}
        env.update(TEMP=str(self.directory), TMP=str(self.directory), TMPDIR=str(self.directory))
        model_event(self.work_dir, f'正在加载翻译模型（{self.name}）')
        try:
            self.process = spawn_external(args, stdin=subprocess.DEVNULL, stdout=self.log,
                stderr=subprocess.STDOUT, env=env, cwd=self.directory)
            from .hy_translator import protect_child
            self.cleanup = protect_child(self.process)
            deadline = time.monotonic()+300
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise VoxlateError('本地翻译运行失败：'+self.details())
                # Native bind(port=0) chooses a free port atomically. Read only our own log.
                match = re.search(r'listening on http://127\.0\.0\.1:(\d+)', self.details(16384))
                if match:
                    self.port = int(match[1])
                    try:
                        status, body = self.request('GET', '/health', timeout=2)
                        if status == 200 and body.get('status') == 'ok':
                            return self
                    except (OSError, http.client.HTTPException):
                        pass
                time.sleep(.1)
            raise VoxlateError('翻译模型加载超时，请查看运行环境或重试。')
        except BaseException:
            self.close()
            raise

    def details(self, limit=2000):
        # Open a separate reader: seeking the inherited stdout handle would race the child.
        with (self.directory/'stderr.log').open('rb') as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell()-limit))
            return stream.read().decode('utf-8', errors='replace')

    def request(self, method, path, payload=None, timeout=1800):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=timeout)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        connection.sock = sock
        try:
            sock.settimeout(timeout)
            # Direct numeric connection: no DNS, proxy handling, redirects or external URLs.
            with local_inference_connection(self.port):
                sock.connect(('127.0.0.1', self.port))
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
            connection.request(method, path, body=body, headers={'Content-Type':'application/json',
                'Authorization':'Bearer '+self.key, 'Connection':'close'})
            response = connection.getresponse()
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise VoxlateError('翻译结果过长，已停止本次翻译。')
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeError) as exc:
                raise VoxlateError('本地翻译返回了无效结果。') from exc
            if not isinstance(value, dict):
                raise VoxlateError('本地翻译返回了无效结果。')
            return response.status, value
        finally:
            connection.close()

    def translate(self, prompt, schema):
        if self.process.poll() is not None:
            raise VoxlateError('本地翻译进程已退出：'+self.details())
        payload = dict(messages=[dict(role='user', content=prompt)], stream=False,
            response_format=dict(type='json_object',schema=schema), max_tokens=4096,
            temperature=.2, top_p=.6, top_k=20, repeat_penalty=1.05, cache_prompt=False)
        try:
            status, result = self.request('POST', '/v1/chat/completions', payload)
        except (OSError, http.client.HTTPException) as exc:
            raise VoxlateError('本地翻译连接中断或超时，已停止本次翻译。') from exc
        if status != 200:
            raise VoxlateError('本地翻译运行失败：'+str(result.get('error',status))[:1500])
        try:
            choice = result['choices'][0]
            text = choice['message']['content']
            if choice.get('finish_reason') != 'stop' or not isinstance(text,str):
                raise ValueError('incomplete')
            return text
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise VoxlateError('翻译结果不完整，未覆盖已有译文。') from exc

    def close(self):
        process, self.process = self.process, None
        try:
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                else:
                    process.wait()
        finally:
            try:
                self.cleanup()
            finally:
                self.cleanup = lambda: None
                if self.log is not None:
                    self.log.close()
                    self.log = None
                if process is not None:
                    model_event(self.work_dir, f'已释放翻译模型（{self.name}）')

    def __exit__(self, *_):
        self.close()
