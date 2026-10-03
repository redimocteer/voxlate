from __future__ import annotations

from contextlib import contextmanager
import logging
import json
import copy
import math
import os
import shutil
from pathlib import Path
import subprocess
import sys
import time
import uuid

from .common import VoxlateError, digest, file_hash, model_stamp, read_json, write_json
from .media import Media, build_timeline, check_cancelled, original_audio_intervals
from .runtime import worker_command, spawn_external, external_env
from .tts import valid_wav
from .progress import TTSProgress
from .project_storage import worker_environment, relocated_project
from .dubbing_state import voice_key, sentence_ready, voice_selection, select_voice_version, remember_voice, automatic_reference, reference_input_key, recommended_reference
from .model_lifecycle import model_name
from .roles import ensure_roles, validate_roles, reference_segment, apply_automatic_voice_mode
from .audio_tracks import selected_track

LOG = logging.getLogger("voxlate")
CACHE_VERSION = 1


def elapsed_text(seconds):
    hours, remainder = divmod(max(0, round(seconds)), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


@contextmanager
def project_lock(directory):
    def check_parent_lock():
        for parent in directory.parents:
            if parent.name.endswith('.voxlate') and (parent / '.lock').exists():
                raise VoxlateError('项目正在清空，请稍后再试。')
    check_parent_lock()
    path = directory / ".lock"
    try:
        with path.open("x") as stream:
            stream.write(str(os.getpid()))
    except FileExistsError as exc:
        raise VoxlateError("项目正在使用；若上次被强制终止，请确认进程已退出后移除项目中的 .lock。") from exc
    try:
        check_parent_lock()
        yield
    finally:
        path.unlink(missing_ok=True)


def validate_segments(segments, duration):
    ids = set()
    end = 0.0
    for segment in segments:
        ident = segment["id"]
        if type(ident) is not int or ident < 1 or ident in ids:
            raise VoxlateError("每句 id 必须为唯一的正整数")
        ids.add(ident)
        start, stop = segment["start"], segment["end"]
        if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in (start, stop)):
            raise VoxlateError(f"第 {ident} 句时间必须为有限数值")
        if start < end - 1e-7 or stop <= start or stop > duration + 0.001:
            raise VoxlateError(f"第 {ident} 句时间越界、重叠或未按时间排序")
        if segment.get("speaker", "A") != "A":
            raise VoxlateError("首版只支持单人 Speaker A")
        if not isinstance(segment["source_text"], str) or (not segment["source_text"].strip() and segment.get('pending_recognition') is not True):
            raise VoxlateError(f"第 {ident} 句原文为空")
        if not isinstance(segment.get("target_text", ""), str):
            raise VoxlateError(f"第 {ident} 句译文必须是字符串")
        if type(segment.get("enabled", True)) is not bool:
            raise VoxlateError(f"第 {ident} 句配音选择必须为 true 或 false")
        end = stop


class VideoDubPipeline:
    def __init__(self, config, runner=None, tts_session=None, translation_session=None):
        self.cfg = copy.deepcopy(config)
        self.source_lang = config.get("source_lang", "en")
        if self.source_lang not in ("en", "ja"):
            raise VoxlateError("仅支持英文或日文翻译成中文")
        self.cfg["asr"]["language"] = self.source_lang
        self.cfg["translator"]["source_lang"] = self.source_lang
        self.media = Media(config)
        self.runner = runner or self.run_worker
        self.tts_session = tts_session
        self.translation_session = translation_session

    def run_worker(self, kind, job):
        started = time.perf_counter()
        config = self.cfg['tts' if kind == 'speakers' else kind]
        name = model_name(kind, config)
        label = {'asr': '识别', 'translator': '翻译', 'separator': '人声分离', 'tts': '音色克隆', 'speakers': '角色分组'}[kind]
        events = self.work/'model_events.jsonl'
        events.write_text('', encoding='utf-8')
        event_offset = 0
        release_reported = False
        def drain_events():
            nonlocal event_offset, release_reported
            with events.open(encoding='utf-8') as stream:
                stream.seek(event_offset)
                while line := stream.readline():
                    if not line.endswith('\n'):
                        break
                    message = json.loads(line)
                    release_reported |= message.startswith(f'已释放{label}模型（')
                    self.record_elapsed(message)
                    event_offset = stream.tell()
        request = self.work / f"{kind}_job.json"
        result = self.work / f"{kind}_result.json"
        result.unlink(missing_ok=True)
        progress_path = self.work / f"{kind}_progress.json"
        progress_path.unlink(missing_ok=True)
        write_json(request, dict(job, kind=kind, config=config, result=str(result), progress=str(progress_path),
                                session_root=str(getattr(self, 'session_root', self.work))))
        log = self.work / f"{kind}.log"
        total = job.get("total", len(job.get("segments", [])))
        tracker = TTSProgress(progress_path, log, total, total - len(job["segments"])) if kind == "tts" else None
        session = self.tts_session if kind == 'tts' else self.translation_session if kind == 'translator' else None
        resident = session is not None
        if self.tts_session is not None and kind not in ('tts', 'speakers'):
            self.tts_session.close(f'开始{label}，腾出显存')
        if self.translation_session is not None and kind not in ('translator', 'speakers'):
            self.translation_session.close(f'开始{label}，腾出显存')
        with log.open("a" if resident else "w", encoding="utf-8") as stream:
            env = worker_environment(self.work, external_env())
            try:
                check_cancelled()
                if resident:
                    process, reused = session.submit(self.cfg, request, log)
                    if reused and tracker:
                        tracker.state['phase'] = 'preparing'
                else:
                    process = spawn_external(worker_command(self.cfg, kind, request), stdout=stream,
                                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env, cwd=self.work)
            except OSError as exc:
                raise VoxlateError(f"{kind} 环境无法启动，请检查配置中的 python 路径") from exc
            try:
                notice = time.monotonic()
                checked, last_state = 0, None
                while True:
                    try:
                        code = process.wait(timeout=0.25)
                        break
                    except subprocess.TimeoutExpired:
                        check_cancelled()
                        drain_events()
                        if kind == 'asr' and time.monotonic() - checked >= 1:
                            try:
                                update = read_json(progress_path)
                                if update.get('detail') and update['detail'] != last_state:
                                    LOG.info(update['detail'], extra={'voxlate_progress': update})
                                    last_state = update['detail']
                                    notice = time.monotonic()
                            except (OSError, ValueError):
                                pass
                            checked = time.monotonic()
                        if tracker and time.monotonic() - checked >= 1:
                            update = tracker.snapshot()
                            state = (tracker.state.get("phase"), update["completed"], tracker.state.get("current"))
                            if state != last_state or time.monotonic() - notice >= 15:
                                LOG.info(update["detail"], extra={"voxlate_progress": update})
                                notice, last_state = time.monotonic(), state
                            checked = time.monotonic()
                        elif not tracker and time.monotonic() - notice >= 30:
                            from .progress import separation_status, duration as progress_duration
                            elapsed = time.perf_counter()-started
                            detail = separation_status(log, name, elapsed) if kind == 'separator' else (
                                f'{label}（{name}）处理中 · 已用 {progress_duration(elapsed)}')
                            LOG.info(detail)
                            notice = time.monotonic()
            except BaseException:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                drain_events()
                if not resident and not release_reported:
                    self.record_elapsed(f'已释放{label}模型（{name}）：任务停止或失败')
                raise
        drain_events()
        if not resident and not release_reported:
            self.record_elapsed(f'已释放{label}模型（{name}）')
        if code or not result.is_file():
            if resident:
                session.close(f'{label}失败')
            raise VoxlateError(f"{kind} 失败（退出码 {code}），请查看项目中的 {log.name}。已完成结果保留。")
        if tracker:
            update = tracker.snapshot()
            LOG.info(update["detail"], extra={"voxlate_progress": update})
        value = read_json(result)
        if resident:
            self.record_elapsed(f'{label}模型已保留（{name}），后续继续复用')
        label = {"asr": "识别", "translator": "翻译", "separator": "人声分离", "tts": "配音", 'speakers': '角色分组'}[kind]
        self.record_elapsed(f"{label}完成 · 耗时 {elapsed_text(time.perf_counter() - started)}")
        return value

    def record_elapsed(self, message):
        LOG.info(message)
        try:
            with (getattr(self, 'log_directory', self.work) / "run.log").open("a", encoding="utf-8") as stream:
                stream.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")
        except OSError:
            LOG.warning("耗时记录未能写入 run.log")

    def save(self):
        if getattr(self, 'defer_project_save', False):
            return
        write_json(self.work / "project.json", self.project)

    def stage(self, name, key, outputs, action):
        check_cancelled()
        entry = self.project["stages"].get(name, {})
        if entry.get("key") == key and all(Path(p).is_file() and Path(p).stat().st_size for p in outputs):
            LOG.info("复用缓存：%s", name)
            return
        LOG.info("开始：%s", name)
        started = time.perf_counter()
        action()
        if not all(Path(p).is_file() and Path(p).stat().st_size for p in outputs):
            raise VoxlateError(f"{name} 未生成预期文件")
        self.project["stages"][name] = {"key": key, "seconds": round(time.perf_counter() - started, 3)}
        self.save()

    def process(self, video_path, output_path, work_dir, speaker_ref=None, stop_after=None, reference_range=None,
                *, require_translated=False, auto_reference=None, export_only=False, sentence_ids=None, force_tts=False,
                voice_mode=None, reference_sentence_id=None, force_translation=False, force_recognition=False,
                translate_only=False, auto_export=False, recognition_only=False, translate_missing_only=False,
                force_separation=False, external_voice=None, allow_missing_dubbing=False):
        video, output = Path(video_path).resolve(), Path(output_path).resolve()
        self.work = Path(work_dir).resolve()
        if not video.is_file():
            raise VoxlateError("输入视频不存在")
        if output == video or output.suffix.lower() != ".mp4":
            raise VoxlateError("输出必须为独立的 .mp4 文件，不能覆盖输入")
        if video.is_relative_to(self.work) or output.is_relative_to(self.work):
            raise VoxlateError("输入和输出视频应位于项目缓存目录之外")
        if speaker_ref and (Path(speaker_ref).resolve() == output or Path(speaker_ref).resolve().is_relative_to(self.work)):
            raise VoxlateError("手动参考音频应位于项目缓存目录之外，并与输出路径不同")
        self.work.mkdir(parents=True, exist_ok=True)
        self.reference_range = reference_range
        self.require_translated = require_translated
        self.auto_reference = auto_reference
        self.export_only = export_only
        self.allow_missing_dubbing = allow_missing_dubbing
        if allow_missing_dubbing and not export_only:
            raise VoxlateError('保留未配音原声仅适用于导出步骤')
        self.sentence_ids = set(sentence_ids) if sentence_ids is not None else None
        self.force_tts = force_tts
        self.force_translation = force_translation
        self.translate_missing_only = translate_missing_only
        self.force_recognition = force_recognition
        self.force_separation = force_separation
        if force_separation and (stop_after != 'separate' or recognition_only or translate_only or auto_export or export_only):
            raise VoxlateError('重新分离仅能用于分离步骤')
        self.translate_only, self.auto_export = translate_only, auto_export
        self.recognition_only = recognition_only
        if recognition_only and (stop_after != 'recognize' or auto_export or export_only):
            raise VoxlateError('单独识别仅能用于识别步骤')
        self.defer_project_save = False
        if force_recognition and stop_after not in ('recognize', 'translate'):
            raise VoxlateError('重新识别仅能用于识别步骤')
        if translate_only and (stop_after != 'translate' or force_recognition):
            raise VoxlateError('单独翻译不能重新识别')
        if translate_missing_only and (not translate_only or force_translation or auto_export or export_only):
            raise VoxlateError('补全译文仅能用于单独翻译，不能同时覆盖已有译文')
        if auto_export and (stop_after is not None or export_only or force_recognition or force_translation or force_tts):
            raise VoxlateError('一键导出仅接续未完成步骤')
        if force_translation and stop_after != 'translate':
            raise VoxlateError('重新翻译仅能用于翻译步骤')
        self.voice_mode, self.reference_sentence_id = voice_mode, reference_sentence_id
        self.external_voice = copy.deepcopy(external_voice)
        if voice_mode is not None:
            if voice_mode not in ('uniform', 'individual', 'roles', 'external'):
                raise VoxlateError('音色模式无效')
            self.cfg['tts']['emotion_reference'] = True
        if force_tts and stop_after != 'dub':
            raise VoxlateError('重新配音仅能用于生成配音步骤')
        if self.sentence_ids is not None and stop_after != 'dub' and not translate_only:
            raise VoxlateError('所选句子仅能用于翻译或配音步骤')
        with project_lock(self.work):
            started = time.perf_counter()
            label = '一键导出' if auto_export else ('导出视频' if export_only else
                {'separate':'分离', 'recognize':'识别', 'translate':'翻译' if translate_only else '识别并翻译', 'dub':'生成配音'}.get(stop_after, '配音并导出'))
            outcome = "未完成"
            try:
                result = self._process(video, output, speaker_ref, stop_after)
                outcome = "完成"
                return result
            except Exception as exc:
                if "已取消" in str(exc):
                    outcome = "已停止"
                raise
            finally:
                self.record_elapsed(f"{label}{outcome} · 总耗时 {elapsed_text(time.perf_counter() - started)}")

    def _process(self, video, output, speaker_ref, stop_after):
        LOG.info('正在核对视频…')
        input_key = file_hash(video)
        project_path = self.work / "project.json"
        self.project = read_json(project_path) if project_path.exists() else {
            "schema_version": 1, "name": "voxlate", "source_lang": self.source_lang, "target_lang": "zh",
            "input_hash": input_key, "input": str(video), "audio_track": selected_track(self.cfg), "stages": {}, "segments": []}
        self.project = relocated_project(self.project, project_path)
        previous_project = copy.deepcopy(self.project) if self.force_recognition and project_path.exists() else None
        self.defer_project_save = previous_project is not None or self.force_separation
        if self.project.get("input_hash") != input_key or self.project.get("schema_version") != 1:
            raise VoxlateError("项目与视频或版本不匹配，请指定新的 --work-dir")
        if self.project.get("source_lang", "en") != self.source_lang:
            raise VoxlateError("源语言与项目不同，请新建项目，原有译文会保留。")
        track = selected_track(self.cfg)
        if selected_track(self.project) != track:
            raise VoxlateError('音轨与项目不同，请切换到对应音轨的项目。')
        if self.voice_mode is not None:
            self.project.update(voice_mode=self.voice_mode, reference_sentence_id=self.reference_sentence_id)
            self.project['reference_auto_recommend'] = self.reference_sentence_id is None
            self.project.pop('reference_auto_longest', None)
        if self.external_voice is not None:
            self.project['external_voice'] = self.external_voice
        if self.project.get('voice_mode') == 'roles':
            ensure_roles(self.project)
            validate_roles(self.project, allow_legacy_names=True)
        select_voice_version(self.project, self.cfg)
        if self.export_only:
            return self.export_existing(video, output, input_key)
        if self.translate_only:
            return self.translate_existing()
        if self.auto_export:
            return self.finish_all(video, output, speaker_ref, input_key)
        if stop_after == 'dub' and self.require_translated:
            return self.generate_existing(video, output, speaker_ref)
        if self.require_translated and not self.project["segments"]:
            raise VoxlateError("请先完成识别和翻译，再生成配音。")
        info = self.media.probe(video)
        videos = [s for s in info["streams"] if s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic")]
        if not videos or not any(s["codec_type"] == "audio" for s in info["streams"]):
            raise VoxlateError("输入必须同时包含视频和音频轨道")
        audio_streams = [s for s in info['streams'] if s['codec_type'] == 'audio']
        if track >= len(audio_streams):
            raise VoxlateError(f'视频没有音轨 {track+1}，请重新选择音轨。')
        self.project['audio_track'] = track
        self.project['audio_track_count'] = len(audio_streams)
        duration = float(videos[0].get("duration", info["format"]["duration"]))
        if not math.isfinite(duration) or duration <= 0:
            raise VoxlateError("视频时长无效")
        selected = self.reference_range
        if selected is not None:
            if (not isinstance(selected, (list, tuple)) or len(selected) != 2 or
                    not all(isinstance(v, (int, float)) and math.isfinite(v) for v in selected) or
                    not 0 <= selected[0] < selected[1] <= duration or not 3 <= selected[1] - selected[0] <= 15):
                raise VoxlateError("音色参考须为视频内连续的 3–15 秒，请重新选择起止时间。")
        self.project["duration"] = duration
        self.project["speaker_reference"] = str(Path(speaker_ref).resolve()) if speaker_ref else ""
        self.project["reference_range"] = selected
        self.project["auto_reference"] = self.auto_reference if self.auto_reference is not None else not (speaker_ref or selected)
        cfg = self.cfg
        # Recognition can reuse prepared audio even if the separation runtime or
        # weights were removed. Its saved signature still must match the settings.
        prepared_key = digest(CACHE_VERSION, input_key, track, cfg['separator'], cfg['audio']['sample_rate'])
        prepared = self.project.get('prepared_audio_key') == prepared_key
        audio = self.work / 'original.wav'
        stems_root = self.work / 'separated' / cfg['separator']['model'] / audio.stem
        vocals, background = stems_root / 'vocals.wav', stems_root / 'no_vocals.wav'
        if self.recognition_only and (not prepared or not all(p.is_file() and p.stat().st_size for p in (audio, vocals, background))):
            raise VoxlateError('请先点击「① 分离」准备当前音轨，再识别。已有分离文件会尽量复用。')
        stamps = {}
        if not self.recognition_only:
            stamps['separator'] = digest(CACHE_VERSION, cfg['separator'], model_stamp(cfg['separator']['model_path']))
        if stop_after != 'separate':
            stamps['asr'] = digest(CACHE_VERSION, cfg['asr'], model_stamp(cfg['asr']['model_path']))
        if stop_after != 'separate' and cfg['asr'].get('combined', False):
            from .combined_asr import VERSION
            models = Path(cfg['asr']['model_path']).parent
            stamps['asr'] = digest(stamps['asr'], VERSION,
                [model_stamp(models / name) for name in ('faster-whisper-large-v3', 'faster-whisper-large-v3-turbo')])
        sep_key = self.project.get('stages', {}).get('separate', {}).get('key') if self.recognition_only else digest(input_key, stamps['separator'])
        if track and not self.recognition_only:
            sep_key = digest(sep_key, 'audio-track', track)
        asr_key = digest(sep_key, stamps['asr']) if stop_after != 'separate' else self.project.get('asr_key')
        # Reject incompatible settings before replacing separated audio or saving
        # stage metadata that still belongs to the existing transcript.
        if self.project["segments"] and self.project.get("asr_key") != asr_key and not self.force_recognition:
            raise VoxlateError("识别模型或分离配置已变化；请恢复原设置后继续，或备份后清空项目再识别。命令行可指定新的 --work-dir。")
        if stop_after == 'separate' and self.project['segments'] and self.project.get('stages', {}).get('separate', {}).get('key') != sep_key:
            raise VoxlateError('分离设置已变化；请先备份并清空当前项目，避免旧分句与新音频混用。')
        self.save()
        extract_key = digest(CACHE_VERSION, input_key, duration)
        if track:
            extract_key = digest(extract_key, 'audio-track', track)
        if not self.recognition_only:
            self.stage("extract", extract_key, [audio],
                       lambda: self.media.extract(video, audio, duration, track))
        if self.force_separation:
            self.reseparate(audio, stems_root, sep_key, prepared_key)
        elif not self.recognition_only:
            self.stage("separate", sep_key, [vocals, background],
                       lambda: self.runner("separator", {"audio": str(audio), "directory": str(self.work / "separated")}))
        self.project['prepared_audio_key'] = prepared_key
        self.project['separator_model'] = cfg['separator']['model']
        self.save()
        if stop_after == 'separate':
            from .recognition_models import selected_key as selected_asr_key
            self.project.setdefault('recognition_model', 'asr_combined' if cfg['asr'].get('combined', False) else selected_asr_key(cfg))
            self.save()
            return project_path
        if self.force_recognition or self.project.get("asr_key") != asr_key:
            LOG.info("开始%s识别", "日文" if self.source_lang == "ja" else "英文")
            raw = self.runner("asr", {"audio": str(vocals)})
            segments = []
            previous = 0.0
            for s in raw:
                s["start"] = max(previous, 0, s["start"])
                s["end"] = min(duration, s["end"])
                if s["end"] > s["start"]:
                    s.update(source_lang=self.source_lang, target_lang="zh")
                    segments.append(s)
                    previous = s["end"]
            from .long_sentences import refine_long_sentences
            segments, refined = refine_long_sentences(segments)
            if refined:
                LOG.info('已细分 %d 句超过 8 秒的长句；无法可靠切分的句子保留。', refined)
            if self.force_recognition:
                if not segments:
                    raise VoxlateError('没有识别到对白，原项目保留。')
                validate_segments(segments, duration)
                # Old sentence IDs no longer identify the same references or roles.
                self.project.update(reference_sentence_id=None, reference_auto_recommend=True, roles_initialized=False)
                self.project.pop('reference_recommendation', None)
                self.project.pop('roles', None)
                for stage in ('timeline', 'mix', 'mux'):
                    self.project['stages'].pop(stage, None)
            from .recognition_models import selected_key as selected_asr_key
            self.project.update(segments=segments, asr_key=asr_key,
                recognition_model='asr_combined' if cfg['asr'].get('combined', False) else selected_asr_key(cfg))
            self.save()
        segments = self.project["segments"]
        if not segments:
            raise VoxlateError("没有识别到对白，请检查源语言和人声音轨；不会输出静音配音。")
        validate_segments(segments, duration)
        if self.project.get('voice_mode') == 'roles':
            ensure_roles(self.project)
            validate_roles(self.project, allow_legacy_names=True)
        if self.voice_mode is not None:
            self.project['reference_sentence_id'] = None if automatic_reference(self.project) else voice_selection(self.project)[1]
        if stop_after == 'recognize':
            self.project.pop('translation_config_key', None)
            self.commit_recognition(previous_project)
            self.save()
            LOG.info('识别完成：%d 句', len(segments), extra={'voxlate_progress': {
                'stage':'project_ready', 'path':str(project_path), 'project':copy.deepcopy(self.project)}})
            return project_path
        stamps['translator'] = digest(CACHE_VERSION, cfg['translator'], model_stamp(cfg['translator']['model_path']))
        if cfg["translator"].get("model_type", "").startswith("hy_mt2"):
            stamps["translator"] = digest(stamps["translator"], "context-v1", [s["source_text"] for s in segments])
        active = [s for s in segments if s.get("enabled", True)]
        chosen = [s for s in active if self.sentence_ids is None or s['id'] in self.sentence_ids]
        if self.sentence_ids is not None and (not self.sentence_ids or {s['id'] for s in chosen} != self.sentence_ids):
            raise VoxlateError('所选句子不存在或未启用配音')
        pending = [s for s in segments if self.force_translation or not s.get("target_text", "").strip()
                   or s.get("translation_key") != digest(s["source_text"], stamps["translator"])]
        if pending and self.require_translated:
            raise VoxlateError("译文需要更新，请先点击「翻译」，查看译文后再生成配音。")
        if pending:
            LOG.info("本地%s翻译：%d 句", "日中" if self.source_lang == "ja" else "英中", len(pending))
            translations = self.runner("translator", {"texts": [s["source_text"] for s in pending],
                "context": [s["source_text"] for s in segments], "indices": [segments.index(s) for s in pending]})
            if len(translations) != len(pending) or any(not t.strip() for t in translations):
                raise VoxlateError("翻译结果数量不匹配或存在空译文")
            for segment, translated in zip(pending, translations):
                if "generated_duration" in segment and "timing_text" not in segment:
                    segment["timing_text"] = segment.get("target_text", "")
                segment.update(target_text=translated,
                               translation_key=digest(segment["source_text"], stamps["translator"]))
        self.project["translation_config_key"] = digest(cfg["translator"])
        self.commit_recognition(previous_project)
        self.save()
        LOG.info("译文已就绪：%d 句", len(segments), extra={"voxlate_progress": {
            "stage": "project_ready", "path": str(project_path), "project": copy.deepcopy(self.project)}})
        if stop_after == "translate":
            return project_path

        return self.generate_dubbing(video, output, speaker_ref, stop_after, sep_key, audio, vocals, background)

    def commit_recognition(self, previous_project):
        if previous_project is not None:
            project_path = self.work/'project.json'
            check_cancelled()
            if digest(read_json(project_path)) != digest(previous_project):
                raise VoxlateError('项目已变化，重新识别结果未覆盖当前项目。')
            backup = self.work/'history'/'automatic-recognition'/uuid.uuid4().hex/'project.json'
            if not backup.resolve().is_relative_to(self.work):
                raise VoxlateError('项目备份目录指向项目外。')
            write_json(backup, previous_project)
            self.project.setdefault('recognition_history', []).append(dict(backup=str(backup)))
            self.defer_project_save = False
            LOG.info('已重新自动分句，原项目已备份。')

    def translate_existing(self):
        """Translate saved rows without probing media, separation or recognition."""
        project_path = self.work/'project.json'
        segments = self.project.get('segments', [])
        if not segments:
            raise VoxlateError('请先完成识别')
        validate_segments(segments, self.project['duration'])
        previous = read_json(project_path)
        config_key = digest(self.cfg['translator'])
        changed = self.project.get('translation_config_key') not in (None, config_key)
        chosen = [s for s in segments if self.sentence_ids is None or s['id'] in self.sentence_ids]
        if self.sentence_ids is not None and (not self.sentence_ids or {s['id'] for s in chosen} != self.sentence_ids):
            raise VoxlateError('所选句子不存在，请重新选择。')
        missing_sources = [s for s in chosen if s.get('pending_recognition') and not s['source_text'].strip()]
        if missing_sources:
            folder = self.work/'.temp'/'manual-recognition'/uuid.uuid4().hex
            if not folder.resolve().is_relative_to(self.work.resolve()):
                raise VoxlateError('识别缓存目录指向项目外。')
            original, vocals, _ = self.cached_media()
            inputs = []
            for index, row in enumerate(missing_sources):
                check_cancelled()
                clip = folder/f'{index}.wav'
                self.media.trim(original if row.get('recognition_use_original', True) else vocals,
                    clip, row['start'], row['end']-row['start'])
                inputs.append(dict(index=index, audio=str(clip)))
            LOG.info('补识别手动分句：%d 句', len(inputs))
            texts = self.runner('asr', dict(manual_blocks=inputs))
            if not isinstance(texts, list) or len(texts) != len(inputs) or any(not isinstance(text, str) for text in texts):
                raise VoxlateError('分块识别结果不完整，原项目保留。')
            for row, text in zip(missing_sources, texts):
                row['source_text'] = text.strip()
                if text.strip():
                    row.pop('pending_recognition', None)
                    row.pop('recognition_use_original', None)
                else:
                    row.update(enabled=False, target_text='')
                    LOG.info('第 %s 句未识别到文字，保留原声。', row['id'])
        pending = [s for s in chosen if s['source_text'].strip() and (
            not s.get('target_text', '').strip() or
            (not self.translate_missing_only and (self.force_translation or changed)))]
        if pending:
            LOG.info('翻译：%d 句', len(pending))
            stamp = digest(CACHE_VERSION, self.cfg['translator'], model_stamp(self.cfg['translator']['model_path']))
            context = [s['source_text'] for s in segments]
            translations = self.runner('translator', dict(texts=[s['source_text'] for s in pending],
                context=context, indices=[segments.index(s) for s in pending]))
            if not isinstance(translations, list) or len(translations) != len(pending) or any(
                    not isinstance(t, str) or not t.strip() for t in translations):
                raise VoxlateError('翻译结果缺句或为空，原结果保留。')
            for row, text in zip(pending, translations):
                if 'generated_duration' in row and 'timing_text' not in row:
                    row['timing_text'] = row.get('target_text', '')
                row.update(target_text=text, translation_key=digest(row['source_text'], stamp))
            for stage in ('timeline', 'mix', 'mux'):
                self.project.get('stages', {}).pop(stage, None)
        check_cancelled()
        if digest(read_json(project_path)) != digest(previous):
            raise VoxlateError('项目已变化，译文未覆盖当前项目。')
        if pending and any(s.get('target_text', '').strip() for s in previous['segments']):
            backup = self.work/'history'/'translation'/uuid.uuid4().hex/'project.json'
            if not backup.resolve().is_relative_to(self.work):
                raise VoxlateError('项目备份目录指向项目外。')
            write_json(backup, previous)
            self.project.setdefault('translation_history', []).append(dict(backup=str(backup)))
        self.project['translation_config_key'] = config_key
        self.save()
        if pending:
            LOG.info('翻译完成：更新 %d 句', len(pending))
        else:
            LOG.info('沿用已有译文。')
        LOG.info('译文已就绪', extra={'progress_only':True, 'voxlate_progress': {
            "stage": "project_ready", "path": str(project_path), "project": copy.deepcopy(self.project)}})
        return project_path

    def finish_all(self, video, output, speaker_ref, input_key):
        """Resume checkpoints under the same project lock, preserving user edits."""
        if not self.project.get('segments'):
            if self.project.get('manual_edits'):
                raise VoxlateError('项目已无句子，请先识别或恢复分句。')
            self.auto_export = False
            try:
                self._process(video, output, speaker_ref, 'recognize')
            finally:
                self.auto_export = True
        self.translate_existing()
        check_cancelled()
        active = any(s.get('enabled', True) for s in self.project['segments'])
        if active and self.project.get('voice_mode') == 'roles' and not self.project.get('roles_initialized'):
            _, vocals, _ = self.cached_media()
            grouped = self.analyze_reference_voices(vocals)
            self.project['roles'] = grouped['roles']
            for row in self.project['segments']:
                row.update(grouped['assignments'][str(row['id'])])
            validate_roles(self.project, allow_legacy_names=True)
            self.project['roles_initialized'] = True
            self.save()
        from .dubbing_state import dubbing_ready
        if not dubbing_ready(self.project, self.cfg) or any(
                not valid_wav(s.get('tts_audio', '')) for s in self.project['segments'] if s.get('enabled', True)):
            self.generate_existing(video, output, speaker_ref)
        else:
            LOG.info('沿用已有配音。')
        check_cancelled()
        return self.export_existing(video, output, input_key)

    def cached_media(self):
        audio = self.work/'original.wav'
        model = self.project.get('separator_model')
        if not model:
            job = self.work/'separator_job.json'
            model = read_json(job)['config']['model'] if job.exists() else self.cfg['separator']['model']
        root = self.work/'separated'/model/audio.stem
        vocals, background = root/'vocals.wav', root/'no_vocals.wav'
        if not all(valid_wav(p) for p in (audio, vocals, background)):
            raise VoxlateError('项目音轨缺失，请重新识别')
        return audio, vocals, background

    def generate_existing(self, video, output, speaker_ref):
        if self.translation_session is not None:
            self.translation_session.close('开始配音，腾出显存')
        segments = self.project.get('segments', [])
        if not segments:
            raise VoxlateError('请先完成识别翻译')
        duration = self.project['duration']
        validate_segments(segments, duration)
        selected = self.reference_range
        if selected is not None and (not isinstance(selected, (list, tuple)) or len(selected) != 2 or
                not all(isinstance(v, (int, float)) and math.isfinite(v) for v in selected) or
                not 0 <= selected[0] < selected[1] <= duration or not 3 <= selected[1]-selected[0] <= 15):
            raise VoxlateError('音色参考须为视频内连续的 3–15 秒')
        self.project.update(speaker_reference=str(Path(speaker_ref).resolve()) if speaker_ref else '',
            reference_range=selected, auto_reference=self.auto_reference if self.auto_reference is not None else not (speaker_ref or selected))
        audio, vocals, background = self.cached_media()
        return self.generate_dubbing(video, output, speaker_ref, 'dub',
            self.project['stages']['separate']['key'], audio, vocals, background)

    def reseparate(self, audio, stems_root, sep_key, prepared_key):
        # Produce both stems before touching the current pair. All scratch files
        # stay in this video's project; rollback also covers commit/save failures.
        revision = uuid.uuid4().hex
        scratch = self.work/'.temp'/('separation-' + revision)
        incoming = scratch/'new'/self.cfg['separator']['model']/audio.stem
        backup = scratch/'previous'
        moved_old = installed = False
        started = time.perf_counter()
        previous = copy.deepcopy(self.project)
        try:
            scratch.mkdir(parents=True)
            self.runner('separator', dict(audio=str(audio), directory=str(scratch/'new')))
            if not all(valid_wav(incoming/name) for name in ('vocals.wav', 'no_vocals.wav')):
                raise VoxlateError('分离未生成完整音频，原结果保留。')
            check_cancelled()
            if stems_root.exists():
                stems_root.replace(backup)
                moved_old = True
            stems_root.parent.mkdir(parents=True, exist_ok=True)
            incoming.replace(stems_root)
            installed = True
            self.project['stages']['separate'] = dict(key=sep_key, seconds=round(time.perf_counter()-started, 3))
            for stage in ('reference', 'mix', 'mux'):
                self.project['stages'].pop(stage, None)
            self.project.update(separation_revision=revision, prepared_audio_key=prepared_key,
                                separator_model=self.cfg['separator']['model'])
            for segment in self.project['segments']:
                segment.pop('source_audio', None)
                segment.pop('clip_key', None)
            self.defer_project_save = False
            self.save()
        except Exception:
            self.project = previous
            self.defer_project_save = True
            if installed:
                stems_root.replace(incoming)
            if moved_old:
                backup.replace(stems_root)
            raise
        finally:
            # A failed rollback must never discard the only copy of old stems.
            if scratch.exists() and not backup.exists():
                shutil.rmtree(scratch, ignore_errors=True)
        if backup.exists():
            shutil.rmtree(scratch, ignore_errors=True)

    def analyze_reference_voices(self, vocals, *, refresh=False):
        weights = Path(self.cfg['tts']['model_path'])/'hf_cache'/'campplus_cn_common.bin'
        stamp = (weights.stat().st_size, weights.stat().st_mtime_ns) if weights.is_file() else None
        key = digest(reference_input_key(self.project), str(weights.resolve()), stamp)
        cache = self.work/'recognition'/'reference_analysis.json'
        if not refresh and cache.is_file():
            try:
                saved = read_json(cache)
                if saved.get('key') == key and saved.get('result', {}).get('recommended_sentence_id') in {
                        s['id'] for s in self.project['segments']}:
                    return saved['result']
            except (OSError, ValueError):
                pass
        LOG.info('正在分析参考音色（CAMPPlus，本地 CPU）…')
        prepared = self.work/'.temp'/'speakers'/'vocals-16k.wav'
        self.media.render(['-i', vocals, '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le'], prepared)
        result = self.runner('speakers', dict(audio=str(prepared), segments=[
            dict(id=s['id'], start=s['start'], end=s['end']) for s in self.project['segments']]))
        if result.get('recommended_sentence_id') not in {s['id'] for s in self.project['segments']}:
            raise VoxlateError('未找到合适的参考音色，请手动填写参考句号。')
        write_json(cache, dict(key=key, result=result))
        return result

    def generate_dubbing(self, video, output, speaker_ref, stop_after, sep_key, audio, vocals, background):
        if self.project.get('separation_revision'):
            sep_key = digest(sep_key, self.project['separation_revision'])
        cfg, segments = self.cfg, self.project['segments']
        duration, selected = self.project['duration'], self.reference_range
        project_path, stamps = self.work/'project.json', {}
        active = [s for s in segments if s.get('enabled', True)]
        chosen = [s for s in active if self.sentence_ids is None or s['id'] in self.sentence_ids]
        if self.sentence_ids is not None and (not self.sentence_ids or {s['id'] for s in chosen} != self.sentence_ids):
            raise VoxlateError('所选句子不存在或未启用配音')
        if any(not s.get('target_text', '').strip() for s in chosen):
            raise VoxlateError('请先完成所选句子的译文')

        if active:
            stamps["tts"] = digest(CACHE_VERSION, cfg["tts"], model_stamp(cfg["tts"]["model_path"]),
                                   file_hash(Path(cfg["tts"]["repo_path"]) / "indextts" / "infer_v2_5.py"))
        mode_chosen = [s for s in chosen if s.get('voice_reference_sentence_id') is None]
        if mode_chosen and self.project.get('voice_mode') == 'uniform' and automatic_reference(self.project):
            result = self.analyze_reference_voices(vocals)
            self.project['reference_recommendation'] = recommended_reference(self.project, result['recommended_sentence_id'])
            if apply_automatic_voice_mode(self.project, len(result['roles'])):
                self.record_elapsed(f"识别到 {len(result['roles'])} 个角色，已切换逐句音色；仍可手动改回。")
            else:
                self.record_elapsed(f"自动推荐音色：第 {result['recommended_sentence_id']} 句（对白最多角色的代表句）")
        mode, ident = voice_selection(self.project)
        if mode == 'roles':
            ensure_roles(self.project)
            validate_roles(self.project, allow_legacy_names=True)
        select_voice_version(self.project, cfg)
        sentence_voice = 'voice_mode' in self.project
        if sentence_voice:
            if self.reference_sentence_id is not None and self.reference_sentence_id not in {s['id'] for s in segments}:
                raise VoxlateError('音色参考句号不存在，请重新选择')
            self.project['reference_sentence_id'] = None if automatic_reference(self.project) else ident
        reference = None
        role_references = {}
        if mode_chosen and sentence_voice and mode == 'external':
            from .external_voice import checked_reference
            reference = checked_reference(self.project, self.work)
        elif mode_chosen and sentence_voice and mode == 'roles':
            for role_id in {s['role_id'] for s in mode_chosen}:
                best = reference_segment(self.project, role_id)
                key = digest(sep_key, best['start'], min(15, best['end']-best['start']), self.media.sample_rate)
                path = self.work/'segments'/f'role_reference_{key}.wav'
                if not valid_wav(path):
                    self.media.trim(vocals, path, best['start'], min(15, best['end']-best['start']))
                role_references[role_id] = path
        elif mode_chosen and sentence_voice and mode == 'individual':
            pass  # Each segment supplies its own original voice below.
        elif mode_chosen and sentence_voice:
            best = next(s for s in segments if s['id'] == ident)
            reference = self.work / 'speaker_A.wav'
            ref_start, ref_duration = best['start'], min(15, best['end']-best['start'])
            self.stage('reference', digest(sep_key, ref_start, ref_duration, self.media.sample_rate), [reference],
                       lambda: self.media.trim(vocals, reference, ref_start, ref_duration))
        elif mode_chosen and speaker_ref:
            reference = Path(speaker_ref).resolve()
            if not reference.is_file():
                raise VoxlateError("参考音频不存在")
            ref_duration = self.media.duration(reference)
            if ref_duration < 3 or ref_duration > 15:
                LOG.warning("建议使用 5–15 秒干净参考音频，当前 %.1f 秒", ref_duration)
        elif mode_chosen:
            # Longest recognized single-speaker utterance; capped at 10 s.
            best = max(segments, key=lambda s: s["end"] - s["start"])
            ref_start, ref_duration = (selected[0], selected[1] - selected[0]) if selected else (
                best["start"], min(10, best["end"] - best["start"]))
            reference = self.work / "speaker_A.wav"
            self.stage("reference", digest(sep_key, ref_start, ref_duration, self.media.sample_rate), [reference],
                       lambda: self.media.trim(vocals, reference, ref_start, ref_duration))
            if ref_duration < 5:
                LOG.warning("自动参考仅 %.1f 秒；可用 --speaker-ref 指定更清晰的长片段", ref_duration)
        cache = self.work / "segments"
        cache.mkdir(exist_ok=True)
        preparing = segments if self.sentence_ids is None else chosen
        LOG.info('正在准备配音片段：%d 句…', len(preparing))
        for index, segment in enumerate(preparing, 1):
            check_cancelled()
            clip_key = digest(sep_key, segment["start"], segment["end"], self.media.sample_rate)
            clip = cache / f"source_{segment['id']}_{clip_key[:16]}.wav"
            if not valid_wav(clip):
                self.media.trim(vocals, clip, segment["start"], segment["end"] - segment["start"])
            if index == 1 or index % 10 == 0 or index == len(preparing):
                LOG.info('准备配音片段：%d/%d 句', index, len(preparing))
            segment.update(source_audio=str(clip), target_duration=segment["end"] - segment["start"])
            if not segment.get("enabled", True):
                # Keep the original voice in unchecked intervals, along with the background.
                # Existing TTS cache fields remain available if the sentence is enabled again.
                segment.update(aligned_audio=str(clip))
                continue
            if self.sentence_ids is not None and segment['id'] not in self.sentence_ids:
                continue
            remember_voice(segment)
            if self.force_tts:
                segment['tts_take'] = uuid.uuid4().hex
            reference_id = segment.get('voice_reference_sentence_id')
            if reference_id is not None:
                best = next(s for s in segments if s['id'] == reference_id)
                key = digest(sep_key, best['start'], min(15, best['end']-best['start']), self.media.sample_rate)
                local_reference = cache / f'sentence_reference_{key}.wav'
                if not valid_wav(local_reference):
                    self.media.trim(vocals, local_reference, best['start'], min(15, best['end']-best['start']))
            else:
                local_reference = role_references[segment['role_id']] if mode == 'roles' else (
                    clip if sentence_voice and mode == 'individual' else reference)
            segment['speaker_reference_audio'] = str(local_reference)
            local_ref_key = file_hash(local_reference)
            tts_key = digest(segment["target_text"], local_ref_key, stamps["tts"],
                             clip_key if cfg["tts"]["emotion_reference"] else None)
            if segment.get('tts_take'):
                tts_key = digest(tts_key, segment['tts_take'])
            # Never attach old completion metadata to an unfinished new take.
            if segment.get('tts_key') != tts_key:
                for field in ('voice_key', 'tts_text', 'timing_text', 'generated_duration',
                              'translation_too_long', 'aligned_audio'):
                    segment.pop(field, None)
            segment.update(source_audio=str(clip), tts_key=tts_key,
                           tts_audio=str(cache / f"tts_{tts_key}.wav"),
                           target_duration=segment["end"] - segment["start"])
        self.save()
        missing = [s for s in chosen if not valid_wav(s["tts_audio"])]
        if len(active) != len(segments):
            LOG.info("已选配音 %d/%d 句；未勾选句子保留原声", len(active), len(segments))
        if missing:
            LOG.info("克隆中文音色：%d 句；其他句子使用缓存", len(missing))
            self.runner("tts", {"reference": str(reference) if reference else '', "segments": missing, "total": len(chosen)})
        LOG.info("正在对齐配音…", extra={"voxlate_progress": {"stage": "processing"}})
        for segment in chosen:
            context = voice_key(self.project, cfg, segment)
            check_cancelled()
            if not valid_wav(segment["tts_audio"]):
                raise VoxlateError(f"第 {segment['id']} 句配音缺失")
            # A regenerated waveform may differ despite identical text/settings.
            # Key alignment by actual samples, not just the generation request.
            alignment_key = digest(CACHE_VERSION, segment["tts_key"], file_hash(segment["tts_audio"]),
                                   segment["target_duration"], self.media.sample_rate)
            aligned = cache / f"aligned_{alignment_key}.wav"
            if not valid_wav(aligned):
                generated, ratio = self.media.align(segment["tts_audio"], aligned, segment["target_duration"])
            else:
                generated = self.media.duration(segment["tts_audio"])
                ratio = generated / segment["target_duration"]
            segment.update(aligned_audio=str(aligned), generated_duration=generated,
                           timing_text=segment["target_text"], translation_too_long=ratio >= 2,
                           tts_text=segment['target_text'].strip(), voice_key=context)
            remember_voice(segment)
            if ratio >= 2:
                LOG.warning("第 %d 句需要 %.2fx 加速，建议缩短译文", segment["id"], ratio)
            self.save()
            LOG.info("第 %d 句对齐完成", segment["id"], extra={"progress_only": True, "voxlate_progress": {
                "stage": "sentence_timing", "id": segment["id"], "generated_duration": generated,
                "timing_text": segment["target_text"]}})
        if stop_after == 'dub':
            self.save()
            LOG.info('配音已保存，可逐句试听后导出视频。')
            return project_path
        return self.export_audio(video, output, audio, background, duration, sep_key)

    def export_existing(self, video, output, input_key):
        segments = self.project.get('segments', [])
        if not segments:
            raise VoxlateError('请先识别翻译并生成配音')
        duration = self.project['duration']
        validate_segments(segments, duration)
        from .dubbing_state import missing_dubbing_ids
        stale = set(missing_dubbing_ids(self.project, self.cfg, verify_audio=True))
        if stale and not self.allow_missing_dubbing:
            raise VoxlateError('以下句子需要生成配音：' + '、'.join(map(str, sorted(stale)[:20])))
        if stale:
            LOG.info('导出：%d 句未配音或需重配，保留原声。', len(stale))
        # Missing takes are gaps in the temporary dubbing timeline. The mixer
        # restores the original track there; project selection/takes stay intact.
        render_segments = copy.deepcopy([s for s in segments if s['id'] not in stale])
        original_rows = {s['id']: s for s in segments}
        audio, vocals, background = self.cached_media()
        sep_key = self.project['stages']['separate']['key']
        cache = self.work/'segments'
        cache.mkdir(exist_ok=True)
        for s in render_segments:
            original_row = original_rows[s['id']]
            duration_s = s['end']-s['start']
            if not s.get('enabled', True):
                clip = cache/f"source_{s['id']}_{digest(sep_key, s['start'], s['end'], self.media.sample_rate)[:16]}.wav"
                if not valid_wav(clip):
                    self.media.trim(vocals, clip, s['start'], duration_s)
                s.update(source_audio=str(clip), aligned_audio=str(clip))
            else:
                key = digest(CACHE_VERSION, s['tts_key'], file_hash(s['tts_audio']), duration_s, self.media.sample_rate)
                aligned = cache/f'aligned_{key}.wav'
                if not valid_wav(aligned):
                    self.media.align(s['tts_audio'], aligned, duration_s)
                s['aligned_audio'] = str(aligned)
            original_row['aligned_audio'] = s['aligned_audio']
            if not s.get('enabled', True):
                original_row['source_audio'] = s['source_audio']
        return self.export_audio(video, output, audio, background, duration, sep_key, segments=render_segments)

    def export_audio(self, video, output, audio, background, duration, sep_key, *, segments=None):
        segments, cfg = self.project['segments'] if segments is None else segments, self.cfg
        input_key = self.project['input_hash']
        timeline = self.work / "dubbing.wav"
        mix = self.work / "final_audio.wav"
        timeline_key = digest([(s["start"], s["end"], s["aligned_audio"]) for s in segments], duration, self.media.sample_rate)
        self.stage("timeline", timeline_key, [timeline],
                   lambda: build_timeline(segments, timeline, duration, self.media.sample_rate))
        original_intervals = original_audio_intervals(segments, duration)
        mix_key = digest('original-outside-dubbing-v2', timeline_key, sep_key, cfg["audio"], input_key, original_intervals)
        self.stage("mix", mix_key, [mix], lambda: self.media.mix(background, timeline, mix, duration,
                   cfg["audio"]["background_gain"], cfg["audio"]["dubbing_gain"],
                   original=audio, original_intervals=original_intervals))
        self.stage("mux", digest("dual-audio-v1", input_key, mix_key, str(output)), [output],
                   lambda: self.media.mux(video, mix, output, duration, original_audio=audio))
        self.project["output"] = str(output)
        self.save()
        return output
