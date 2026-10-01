"""Isolated offline workers; GUI models can stay loaded within one project."""
import os
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from voxlate.common import enable_offline, read_json, write_json
from voxlate.model_lifecycle import model_event, model_name


def main():
    enable_offline()
    request = Path(sys.argv[1]).resolve()
    from voxlate.project_storage import worker_environment
    import tempfile
    previous_directory, previous_env, previous_temp = Path.cwd(), dict(os.environ), tempfile.tempdir
    try:
        os.environ.update(worker_environment(request.parent, os.environ))
        tempfile.tempdir = os.environ['TEMP']
        os.chdir(request.parent)
        if '--serve' in sys.argv[2:]:
            serve_models(request)
        else:
            run_job(request)
    finally:
        os.chdir(previous_directory)
        os.environ.clear()
        os.environ.update(previous_env)
        tempfile.tempdir = previous_temp


def serve_models(request):
    import queue
    import threading
    import traceback
    from voxlate.common import digest
    first = read_json(request)
    kind = first['kind']
    root = Path(first.get('session_root', request.parent)).resolve()
    if kind not in ('tts', 'translator') or not request.is_relative_to(root):
        raise ValueError('Invalid resident worker scope')
    config_key = digest(first['config'])
    jobs = queue.Queue()
    def receive():
        # A blocking CRT stdin read can deadlock NumPy initialization on Windows.
        # Poll the pipe before reading; no open socket or network service is used.
        import time
        fd = sys.stdin.fileno()
        if os.name == 'nt':
            import ctypes
            import msvcrt
            from ctypes import wintypes
            peek = ctypes.WinDLL('kernel32', use_last_error=True).PeekNamedPipe
            peek.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                             ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
            peek.restype = wintypes.BOOL
            handle = msvcrt.get_osfhandle(fd)
        pending = b''
        while True:
            if os.name == 'nt':
                available = wintypes.DWORD()
                if not peek(handle, None, 0, None, ctypes.byref(available), None):
                    break
                if not available.value:
                    time.sleep(.05)
                    continue
                count = min(available.value, 65536)
            else:
                import select
                if not select.select([fd], [], [], .1)[0]:
                    continue
                count = 65536
            chunk = os.read(fd, count)
            if not chunk:
                break
            pending += chunk
            while b'\n' in pending:
                line, pending = pending.split(b'\n', 1)
                if line.strip():
                    jobs.put(Path(line.decode('utf-8').strip()).resolve())
        # Also release CUDA if the parent exits while a job is running.
        os._exit(0)
    threading.Thread(target=receive, daemon=True).start()
    cache = {}
    while True:
        try:
            job = read_json(request)
            if (not request.is_relative_to(root) or job['kind'] != kind or digest(job['config']) != config_key
                    or (kind == 'tts' and request.parent != root)):
                raise ValueError('Resident worker project or settings changed')
            if kind == 'translator':
                # Each editor request has its own project-local diagnostics, while
                # the native model process remains attached to the same worker.
                from contextlib import redirect_stdout, redirect_stderr
                with request.with_name('translator.log').open('a', encoding='utf-8') as stream:
                    with redirect_stdout(stream), redirect_stderr(stream):
                        try:
                            run_job(request, cache)
                        except Exception:
                            traceback.print_exc()
                            raise
            else:
                run_job(request, cache)
            write_json(request.with_suffix('.done.json'), {'ok': True})
        except Exception:
            traceback.print_exc()
            write_json(request.with_suffix('.done.json'), {'ok': False})
            return
        request = jobs.get()


def run_job(request, engine_cache=None):
    job = read_json(request)
    kind, cfg = job["kind"], job["config"]
    if kind == 'speakers':
        from voxlate.speaker_groups import analyze_speakers
        result = analyze_speakers(job['audio'], job['segments'], cfg, request.parent)
    elif kind == "separator":
        from voxlate.separator import separate
        model_event(request.parent, f'正在加载人声分离模型（{model_name(kind, cfg)}）')
        result = separate(job["audio"], job["directory"], cfg)
    elif kind == "asr":
        if 'manual_blocks' in job:
            from voxlate.manual_asr import transcribe_blocks
            result = transcribe_blocks(job['manual_blocks'], cfg, request.parent)
        else:
            from voxlate.asr import transcribe
            result = transcribe(job["audio"], cfg, work_dir=request.parent)
    elif kind == "translator":
        from voxlate.translator import Translator
        result = Translator(cfg, work_dir=request.parent, session_cache=engine_cache).translate_many(
            job["texts"], job.get("context"), job.get("indices"))
    elif kind == "tts":
        from voxlate.tts import TTSEngine, valid_wav
        from voxlate.progress import report_tts
        pending = [s for s in job["segments"] if not valid_wav(s["tts_audio"])]
        total = job.get("total", len(job["segments"]))
        completed = total - len(pending)
        progress = job.get("progress")
        import time
        engine = engine_cache.get('engine') if engine_cache is not None else None
        reused = engine is not None
        loading_seconds = 0.
        if pending and engine is None:
            report_tts(progress, "loading", completed, total)
            model_event(request.parent, '正在加载音色克隆模型（IndexTTS 2.5）')
            loading = time.perf_counter()
            engine = TTSEngine(cfg)
            loading_seconds = time.perf_counter()-loading
            model_event(request.parent, f'音色克隆模型已加载（IndexTTS 2.5），耗时 {loading_seconds:.2f} 秒')
            if engine_cache is not None:
                engine_cache['engine'] = engine
        elif pending:
            report_tts(progress, 'preparing', completed, total)
            engine.reset_reference_cache()
            model_event(request.parent, '复用音色克隆模型（IndexTTS 2.5）')
        generation_started = time.perf_counter()
        for i, segment in enumerate(pending, 1):
            report_tts(progress, "generating", completed, total, segment["id"])
            print(f"中文配音 {i}/{len(pending)}", flush=True)
            engine.generate(segment["target_text"], segment.get('speaker_reference_audio') or job["reference"], segment["tts_audio"],
                            segment["source_audio"] if cfg["emotion_reference"] else None)
            completed += 1
            report_tts(progress, "completed" if completed == total else "generating", completed, total,
                       pending[i]["id"] if i < len(pending) else None)
        report_tts(progress, "completed", completed, total)
        generation_seconds = time.perf_counter()-generation_started
        write_json(request.with_name('tts_metrics.json'), dict(reused=reused, sentences=len(pending),
            loading_seconds=loading_seconds, generation_seconds=generation_seconds))
        print(f'本次合成 {len(pending)} 句，耗时 {generation_seconds:.2f} 秒', flush=True)
        result = True
    else:
        raise ValueError(f"Unknown stage: {kind}")
    write_json(job["result"], result)


if __name__ == "__main__":
    main()
