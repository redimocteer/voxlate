"""Real FFmpeg integration with deterministic stand-ins for heavyweight models."""
from collections import Counter
import copy
import math
import logging
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid
import wave

from voxlate.common import VoxlateError, load_config, read_json, write_json
from voxlate.media import Media, build_timeline, tempo_filter, restore_original_intervals, original_audio_intervals
from voxlate.pipeline import VideoDubPipeline, validate_segments
from voxlate.tts import valid_wav


class TestDirectory:
    """Use inherited workspace permissions on Windows restricted-token runners."""
    def __init__(self):
        self.base = (Path(__file__).resolve().parents[1] / "temp").resolve()
        self.path = self.base / ("test-中文 " + uuid.uuid4().hex)
        self.path.mkdir(parents=True)
        self.name = str(self.path)

    def __enter__(self):
        return self.name

    def __exit__(self, *args):
        self.cleanup()

    def cleanup(self):
        resolved = self.path.resolve()
        if resolved.parent != self.base or not resolved.name.startswith("test-"):
            raise RuntimeError("Unsafe test cleanup path")
        shutil.rmtree(resolved)


def tone(path, duration, rate=24000, frequency=440, amplitude=0.15):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = b"".join(struct.pack("<h", int(32767 * amplitude * math.sin(2 * math.pi * frequency * i / rate)))
                      for i in range(round(duration * rate)))
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        audio.writeframes(frames)


class Invariants(unittest.TestCase):
    def test_audio_track_selection_rejects_invalid_settings_and_labels_unknown_tracks(self):
        from voxlate.audio_tracks import selected_track, track_label, track_suffix
        self.assertEqual(selected_track({}), 0)
        for value in (-1, True, '1', 1.5, None):
            with self.assertRaises(VoxlateError):
                selected_track({'audio_track':value})
        with self.assertRaises(VoxlateError):
            selected_track([])
        self.assertEqual(track_label(0, {}), '音轨 1')
        self.assertEqual(track_label(1, dict(tags=dict(language='yue'), channels=2)), '音轨 2 · 粤语 · 立体声')
        self.assertEqual(track_suffix({'audio_track_count':2}), '.audio-1')
        self.assertEqual(track_suffix({'audio_track_count':1}), '')
        self.assertEqual(track_suffix({'audio_track':1}), '.audio-2')

    def test_original_covers_leading_trailing_missing_and_discarded_speech(self):
        rows = [dict(start=2, end=3), dict(start=4, end=5, enabled=False), dict(start=7,end=9)]
        self.assertEqual(original_audio_intervals(rows,10), [(0,2),(3,7),(9,10)])
        self.assertEqual(original_audio_intervals([],10), [(0,10)])
        self.assertEqual(original_audio_intervals([dict(start=0,end=10)],10), [])
        self.assertEqual(original_audio_intervals([dict(start=0,end=10,enabled=False)],10), [(0,10)])

    def test_project_cleanup_rejects_active_jobs_and_redirects(self):
        from voxlate.project_storage import clear_video_project, project_root
        from voxlate.pipeline import project_lock
        with TestDirectory() as folder:
            video = Path(folder)/'source.mp4'
            video.write_bytes(b'fixture')
            root = project_root(video)
            profile = root/'en-combined'
            profile.mkdir(parents=True)
            marker = profile/'keep.txt'
            marker.write_text('keep')
            with project_lock(profile):
                with self.assertRaisesRegex(VoxlateError, '正在使用'):
                    clear_video_project(video)
                self.assertTrue(marker.exists())
            with project_lock(root):
                with self.assertRaisesRegex(VoxlateError, '正在清空'):
                    with project_lock(profile):
                        self.fail('must not acquire child lock during cleanup')
            with patch('voxlate.project_storage.project_root', return_value=root), patch('voxlate.project_storage.Path.resolve', return_value=Path(folder)):
                with self.assertRaisesRegex(VoxlateError, '位置异常'):
                    clear_video_project(video)
            self.assertTrue(marker.exists())
            clear_video_project(video)
            self.assertFalse(root.exists())
            self.assertTrue(video.exists())

    def test_original_intervals_preserve_exact_stereo_samples_and_positions(self):
        with TestDirectory() as folder:
            original, mixed = Path(folder)/'original.wav', Path(folder)/'mixed.wav'
            rate, frames = 1000, 2000
            source = b''.join(struct.pack('<hh', i, -i) for i in range(frames))
            before = b''.join(struct.pack('<hh', 8000+i, 9000-i) for i in range(frames))
            for path, data in ((original, source), (mixed, before)):
                with wave.open(str(path), 'wb') as audio:
                    audio.setparams((2, 2, rate, 0, 'NONE', 'not compressed'))
                    audio.writeframes(data)
            restore_original_intervals(mixed, original, [(0, .1), (.35, .7), (.6, .9), (1.7, 2)])
            with wave.open(str(mixed)) as audio:
                self.assertEqual(audio.getnframes(), frames)
                actual = audio.readframes(frames)
            for i in range(frames):
                expected = source if i < 100 or 350 <= i < 900 or i >= 1700 else before
                self.assertEqual(actual[i*4:(i+1)*4], expected[i*4:(i+1)*4])

    def test_elapsed_log_records_success_cache_failure_and_stop(self):
        with TestDirectory() as folder:
            root = Path(folder)
            video = root / "video.mp4"
            video.write_bytes(b"fixture")
            cfg = load_config(Path(__file__).resolve().parents[1] / "config.json")
            pipeline = VideoDubPipeline(cfg)
            for duration, error, label in ((65, None, "完成"), (2, None, "完成"),
                                            (3, VoxlateError("失败"), "未完成"),
                                            (4, VoxlateError("已取消处理"), "已停止")):
                with patch.object(pipeline, "_process", side_effect=error, return_value="done"), patch(
                        "voxlate.pipeline.time.perf_counter", side_effect=[100, 100 + duration]):
                    if error:
                        with self.assertRaises(VoxlateError):
                            pipeline.process(video, root / "out.mp4", root / "project", stop_after="translate")
                    else:
                        pipeline.process(video, root / "out.mp4", root / "project", stop_after="translate")
                log = (root / "project/run.log").read_text(encoding="utf-8")
                self.assertIn("识别并翻译" + label + " · 总耗时 ", log.splitlines()[-1])
            self.assertEqual(len(log.splitlines()), 4)
            self.assertIn("总耗时 00:01:05", log)
            self.assertIn("总耗时 00:00:02", log)

    def test_tempo_extremes(self):
        for ratio in (0.08, 0.5, 1, 1.31, 2, 5, 12):
            factors = [float(s.split("=")[1]) for s in tempo_filter(ratio).split(",")]
            self.assertTrue(all(0.5 <= f <= 2 for f in factors))
            self.assertAlmostEqual(math.prod(factors), ratio)
        for invalid in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(VoxlateError):
                tempo_filter(invalid)

    def test_invalid_edits_rejected(self):
        good = [{"id": 1, "start": 0, "end": 1, "source_text": "hello"},
                {"id": 2, "start": 1, "end": 2, "source_text": "bye"}]
        validate_segments(good, 3)
        for field, value in (("start", 0.5), ("end", 4), ("end", float("nan")), ("id", 1), ("speaker", "B")):
            bad = copy.deepcopy(good)
            bad[1][field] = value
            with self.assertRaises(VoxlateError):
                validate_segments(bad, 3)

    def test_offline_guard(self):
        code = "from voxlate.common import enable_offline; import socket; enable_offline(); socket.create_connection(('example.com', 443))"
        result = subprocess.run([sys.executable, "-c", code], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"VoxlateError", result.stderr)

    def test_timeline_silence_and_exact_placement(self):
        with TestDirectory() as temp:
            folder = Path(temp)
            tone(folder / "a.wav", 0.3)
            tone(folder / "b.wav", 0.2)
            segments = [{"start": 0.2, "end": 0.5, "aligned_audio": str(folder / "a.wav")},
                        {"start": 0.8, "end": 1.0, "aligned_audio": str(folder / "b.wav")}]
            build_timeline(segments, folder / "full.wav", 1.2, 24000)
            with wave.open(str(folder / "full.wav")) as audio:
                self.assertEqual(audio.getnframes(), 28800)
                samples = struct.unpack("<28800h", audio.readframes(28800))
            self.assertFalse(any(samples[:4800]))
            self.assertTrue(any(samples[4800:12000]))
            self.assertFalse(any(samples[12000:19200]))
            self.assertTrue(any(samples[19200:24000]))
            self.assertFalse(any(samples[24000:]))

    def test_truncated_wave_is_not_reused(self):
        with TestDirectory() as temp:
            path = Path(temp) / "truncated.wav"
            tone(path, 0.2)
            self.assertTrue(valid_wav(path))
            path.write_bytes(path.read_bytes()[:-100])
            self.assertFalse(valid_wav(path))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable")
class PipelineIntegration(unittest.TestCase):
    def test_separation_and_recognition_are_independent_and_reusable(self):
        model_path = self.cfg['asr']['model_path']
        self.cfg['asr']['model_path'] = str(self.root/'absent-asr')
        self.run_pipeline(stop_after='separate')
        self.assertEqual(self.calls, Counter(separator=1))
        project = read_json(self.work/'project.json')
        self.assertEqual(project['segments'], [])
        self.assertIn('prepared_audio_key', project)
        self.run_pipeline(stop_after='separate')
        self.assertEqual(self.calls, Counter(separator=1))
        self.cfg['asr']['model_path'] = model_path
        # Removing separator weights after preparation must not prevent ASR.
        marker = Path(self.cfg['separator']['model_path'])/'model.bin'
        if marker.exists():
            marker.unlink()
        self.run_pipeline(stop_after='recognize', recognition_only=True, force_recognition=True)
        self.assertEqual(self.calls, Counter(separator=1, asr=1))
        self.assertTrue(read_json(self.work/'project.json')['segments'])
        before = (self.work/'project.json').read_bytes()
        self.cfg['separator']['shifts'] += 1
        with self.assertRaisesRegex(VoxlateError, '请先点击'):
            self.run_pipeline(stop_after='recognize', recognition_only=True, force_recognition=True)
        self.assertEqual((self.work/'project.json').read_bytes(), before)

    def test_recognition_alone_never_silently_separates(self):
        with self.assertRaisesRegex(VoxlateError, '请先点击'):
            self.run_pipeline(stop_after='recognize', recognition_only=True)
        self.assertEqual(self.calls, Counter())
        self.assertFalse((self.work/'original.wav').exists())

    def test_selected_audio_track_is_extracted_isolated_and_retained_in_export(self):
        from voxlate.audio_tracks import read_audio_tracks
        from voxlate.project_storage import default_project_directory
        video = self.root/'日本語 English 双音轨.mkv'
        self.media.render(['-i', self.video, '-f', 'lavfi', '-i', 'sine=frequency=1300:duration=3',
            '-map', '0:v:0', '-map', '0:a:0', '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'pcm_s16le',
            '-metadata:s:a:0', 'language=eng', '-metadata:s:a:1', 'language=jpn',
            '-metadata:s:a:1', 'title=日本語'], video)
        tracks = read_audio_tracks(video, self.cfg)
        self.assertEqual([t['index'] for t in tracks], [0, 1])
        self.assertIn('英语', tracks[0]['label'])
        self.assertIn('日语', tracks[1]['label'])
        self.video = video
        first_work = default_project_directory(video, self.cfg)
        self.cfg['audio_track'] = 1
        self.work = default_project_directory(video, self.cfg)
        self.assertNotEqual(first_work, self.work)
        self.assertTrue(self.work.name.endswith('-audio-2'))
        self.run_pipeline(auto_export=True, voice_mode='individual')
        path = self.work/'project.json'
        before = path.read_bytes()
        self.assertEqual(read_json(path)['audio_track'], 1)
        with wave.open(str(self.work/'original.wav')) as audio:
            audio.setpos(4410)
            values = struct.unpack('<'+ 'h'*8820, audio.readframes(4410))[::2]
        def strength(freq):
            return abs(sum(v*complex(math.cos(2*math.pi*freq*i/44100),
                                    math.sin(2*math.pi*freq*i/44100)) for i,v in enumerate(values)))
        self.assertGreater(strength(1300), 20*strength(900))
        original = self.root/'exported-original.wav'
        self.media.render(['-i', self.output, '-map', '0:a:1', '-acodec', 'pcm_s16le'], original)
        with wave.open(str(original)) as audio:
            audio.setpos(4410)
            values = struct.unpack('<'+'h'*8820, audio.readframes(4410))[::2]
        self.assertGreater(strength(1300), 20*strength(900))
        self.cfg['audio_track'] = 0
        with self.assertRaisesRegex(VoxlateError, '音轨与项目不同'):
            self.run_pipeline(stop_after='translate', translate_only=True)
        self.assertEqual(path.read_bytes(), before)
        self.work = first_work
        self.run_pipeline(stop_after='recognize')
        self.assertEqual(read_json(self.work/'project.json')['audio_track'], 0)
        self.assertEqual(path.read_bytes(), before)
        self.cfg['audio_track'] = 5
        self.work = default_project_directory(video, self.cfg)
        with self.assertRaisesRegex(VoxlateError, '没有音轨'):
            self.run_pipeline(stop_after='recognize')

    def test_four_steps_keep_recognition_checkpoint_and_retry_only_translation(self):
        unusual = self.root/"中日English & # [字幕] '1%.mp4"
        self.video.rename(unusual)
        self.video = unusual
        self.work = Path(str(unusual)+'.voxlate')/'en'
        self.output = self.root/"中日English & # [字幕] '1%.zh.mp4"
        translator_path = self.cfg['translator']['model_path']
        self.cfg['translator']['model_path'] = str(self.root/'missing-translator')
        self.run_pipeline(stop_after='recognize', force_recognition=True, voice_mode='individual')
        path = self.work/'project.json'
        recognized = read_json(path)
        self.assertEqual(self.calls, Counter(separator=1, asr=1))
        self.assertTrue(all(not s['target_text'] for s in recognized['segments']))
        self.cfg['translator']['model_path'] = translator_path
        for section in ('asr', 'separator'):
            self.cfg[section]['model_path'] = str(self.root/'absent-model')
        pipeline = VideoDubPipeline(self.cfg, self.runner)
        with patch.object(pipeline, 'runner', side_effect=VoxlateError('synthetic translation failure')):
            with self.assertRaisesRegex(VoxlateError, 'translation failure'):
                pipeline.process(self.video, self.output, self.work, stop_after='translate', translate_only=True)
        self.assertEqual(read_json(path), recognized)
        self.run_pipeline(stop_after='translate', translate_only=True, force_translation=True)
        translated = read_json(path)
        self.assertTrue(all(s['target_text'] for s in translated['segments']))
        for old, new in zip(recognized['segments'], translated['segments']):
            for key in ('id', 'start', 'end', 'source_text'):
                self.assertEqual(old[key], new[key])
        self.assertEqual(self.calls, Counter(separator=1, asr=1, translator=1))
        self.run_pipeline(stop_after='dub', require_translated=True, voice_mode='individual')
        calls = self.calls.copy()
        self.run_pipeline(export_only=True, voice_mode='individual')
        self.assertEqual(calls, self.calls)
        self.assertTrue(self.output.is_file())

    def test_recognize_alone_backs_up_manual_results_and_translate_preserves_rows(self):
        self.run_pipeline(stop_after='translate')
        path = self.work/'project.json'
        old = read_json(path)
        old['segments'][0].update(source_text='Manual source', target_text='手动译文')
        old['manual_edits'] = [dict(synthetic=True)]
        write_json(path, old)
        self.run_pipeline(stop_after='translate', translate_only=True, force_translation=True)
        translated = read_json(path)
        self.assertEqual(translated['manual_edits'], old['manual_edits'])
        self.assertEqual(translated['segments'][0]['source_text'], 'Manual source')
        backup = translated['translation_history'][-1]['backup']
        self.assertEqual(read_json(backup), old)
        self.run_pipeline(stop_after='recognize', force_recognition=True)
        recognized = read_json(path)
        self.assertEqual(recognized['segments'][0]['source_text'], 'Hello.')
        self.assertTrue(all(not s['target_text'] for s in recognized['segments']))
        self.assertEqual(read_json(recognized['recognition_history'][-1]['backup']), translated)

    def test_one_click_preserves_manual_text_and_boundaries_and_reuses_ready_audio(self):
        self.run_pipeline(auto_export=True, voice_mode='individual')
        path = self.work/'project.json'
        first = read_json(path)
        self.assertTrue(self.output.exists())
        calls = self.calls.copy()
        self.run_pipeline(auto_export=True, voice_mode='individual')
        self.assertEqual(calls, self.calls)
        first['segments'][0].update(target_text='人工改过的译文', start=.3)
        first['manual_edits'] = [dict(synthetic=True)]
        write_json(path, first)
        self.tts_ids.clear()
        self.run_pipeline(auto_export=True, voice_mode='individual')
        result = read_json(path)
        self.assertEqual(result['segments'][0]['target_text'], '人工改过的译文')
        self.assertEqual(result['segments'][0]['start'], .3)
        self.assertEqual(result['manual_edits'], first['manual_edits'])
        self.assertEqual(self.tts_ids, [1])
        for stage in ('asr', 'translator', 'separator'):
            self.assertEqual(self.calls[stage], calls[stage])

    def test_one_click_resumes_recognition_after_translation_failure_and_groups_roles(self):
        self.run_pipeline(stop_after='recognize', voice_mode='roles')
        self.run_pipeline(auto_export=True, voice_mode='roles')
        project = read_json(self.work/'project.json')
        self.assertTrue(project['roles_initialized'])
        self.assertTrue(all(s['role_id'] == 'a' for s in project['segments']))
        self.assertEqual(self.calls['asr'], 1)
        self.assertEqual(self.calls['speakers'], 1)
        self.assertTrue(self.output.exists())

    def test_worker_release_notice_is_reported_once_with_or_without_worker_event(self):
        from unittest.mock import Mock
        import json
        for reported in (False, True):
            with self.subTest(reported=reported):
                pipeline=VideoDubPipeline(self.cfg)
                pipeline.work=self.work
                self.work.mkdir(parents=True,exist_ok=True)
                def spawn(*args,**kwargs):
                    if reported:
                        (self.work/'model_events.jsonl').write_text(
                            json.dumps('已释放识别模型（Whisper test）')+'\n',encoding='utf-8')
                    write_json(self.work/'asr_result.json',[])
                    return Mock(wait=Mock(side_effect=[subprocess.TimeoutExpired('worker',.25),0]))
                with patch('voxlate.pipeline.spawn_external',side_effect=spawn), \
                        patch('voxlate.pipeline.worker_command',return_value=['synthetic-worker']), \
                        patch.object(pipeline,'record_elapsed') as record:
                    self.assertEqual(pipeline.run_worker('asr',{'audio':'synthetic.wav'}),[])
                messages=[call.args[0] for call in record.call_args_list]
                self.assertEqual(sum(message.startswith('已释放识别模型') for message in messages),1)
                self.assertTrue(any(message.startswith('识别完成') for message in messages))

    def test_role_references_partial_updates_and_prior_mode_cache(self):
        from voxlate.dubbing_state import voice_key, sentence_ready, select_voice_version
        self.run_pipeline(stop_after='translate', voice_mode='uniform')
        self.run_pipeline(stop_after='dub', require_translated=True, voice_mode='uniform', reference_sentence_id=1)
        path = self.work/'project.json'
        project = read_json(path)
        uniform_audio = [s['tts_audio'] for s in project['segments']]
        project['roles'] = [dict(id='a',name='角色 A',reference_sentence_id=1),
            dict(id='b',name='角色 B',reference_sentence_id=2)]
        for s, role in zip(project['segments'], ['a','b']):
            s['role_id'] = role
        write_json(path, project)
        self.run_pipeline(stop_after='dub',require_translated=True,voice_mode='roles')
        roles = read_json(path)
        for s in roles['segments']:
            self.assertIn('role_reference_', s['speaker_reference_audio'])
            self.assertAlmostEqual(self.media.duration(s['speaker_reference_audio']),s['end']-s['start'],delta=.02)
            self.assertTrue(sentence_ready(s, voice_key(roles,self.cfg,s)))
        calls = dict(self.calls)
        roles['roles'][0]['name'] = '改名'
        write_json(path, roles)
        self.run_pipeline(stop_after='dub',require_translated=True,voice_mode='roles')
        self.assertEqual(dict(self.calls),calls)
        roles = read_json(path)
        untouched = copy.deepcopy(roles['segments'][1])
        roles['segments'][0]['role_id'] = 'b'
        write_json(path,roles)
        self.tts_ids.clear()
        self.run_pipeline(stop_after='dub',require_translated=True,voice_mode='roles',sentence_ids=[1])
        current = read_json(path)
        self.assertEqual(current['segments'][1],untouched)
        self.assertEqual(self.tts_ids,[1])
        self.run_pipeline(export_only=True,voice_mode='uniform',reference_sentence_id=1)
        self.assertEqual([s['tts_audio'] for s in read_json(path)['segments']],uniform_audio)
        self.assertTrue(all(Path(p).is_file() for p in uniform_audio))

    def test_sentence_voice_modes_choose_correct_reference_and_invalidate_export(self):
        self.cfg['tts']['emotion_reference'] = False  # Legacy settings are overridden for the new UI modes.
        self.run_pipeline(stop_after='translate', voice_mode='uniform')
        project = read_json(self.work/'project.json')
        self.assertIsNone(project['reference_sentence_id'])
        jobs = []
        def runner(kind, job):
            if kind == 'tts':
                jobs.append(copy.deepcopy(job))
            return self.runner(kind, job)
        def dub(mode, ident):
            return VideoDubPipeline(self.cfg, runner).process(self.video, self.output, self.work,
                stop_after='dub', require_translated=True, voice_mode=mode, reference_sentence_id=ident)
        dub('uniform', 1)
        first = read_json(self.work/'project.json')
        self.assertTrue(all(s['speaker_reference_audio'] == str(self.work/'speaker_A.wav') for s in jobs[-1]['segments']))
        self.assertAlmostEqual(self.media.duration(self.work/'speaker_A.wav'), .85, delta=.02)
        with self.assertRaisesRegex(VoxlateError, '需要生成配音'):
            self.run_pipeline(export_only=True, voice_mode='individual', reference_sentence_id=1)
        dub('individual', 1)
        self.assertTrue(all(s['speaker_reference_audio'] == s['source_audio'] for s in jobs[-1]['segments']))
        individual = read_json(self.work/'project.json')
        self.assertEqual(first['segments'][0]['tts_audio'], individual['segments'][0]['tts_audio'])
        self.assertNotEqual(first['segments'][1]['tts_audio'], individual['segments'][1]['tts_audio'])
        count = len(jobs)
        dub('individual', 2)  # In this mode the uniform reference number is irrelevant.
        self.assertEqual(len(jobs), count)
        # Switching restores a completed version immediately, including export,
        # and does not require running either model again.
        self.run_pipeline(export_only=True, voice_mode='uniform', reference_sentence_id=1)
        restored = read_json(self.work/'project.json')
        self.assertEqual([s['tts_audio'] for s in restored['segments']],
                         [s['tts_audio'] for s in first['segments']])
        self.run_pipeline(export_only=True, voice_mode='individual', reference_sentence_id=2)
        restored = read_json(self.work/'project.json')
        self.assertEqual([s['tts_audio'] for s in restored['segments']],
                         [s['tts_audio'] for s in individual['segments']])
        self.assertEqual(len(jobs), count)
        with self.assertRaisesRegex(VoxlateError, '不存在'):
            dub('uniform', 99)

    def test_empty_reference_recommends_caches_and_migrates_old_default(self):
        self.run_pipeline(stop_after='translate', voice_mode='uniform')
        path = self.work/'project.json'
        project = read_json(path)
        project.update(reference_sentence_id=None, reference_auto_longest=True)
        project.pop('reference_auto_recommend', None)
        write_json(path, project)
        self.run_pipeline(stop_after='dub', require_translated=True, voice_mode='uniform')
        automatic = read_json(path)
        self.assertIsNone(automatic['reference_sentence_id'])
        self.assertTrue(automatic['reference_auto_recommend'])
        self.assertNotIn('reference_auto_longest', automatic)
        from voxlate.dubbing_state import voice_selection
        self.assertEqual(voice_selection(automatic), ('uniform', 1))
        selected = automatic['segments'][0]
        self.assertAlmostEqual(self.media.duration(self.work/'speaker_A.wav'),
                               selected['end']-selected['start'], delta=.02)
        calls = dict(self.calls)
        self.assertEqual(calls['speakers'], 1)
        self.run_pipeline(stop_after='dub', require_translated=True, voice_mode='uniform')
        self.assertEqual(dict(self.calls), calls)
        self.run_pipeline(stop_after='dub', require_translated=True,
                          voice_mode='uniform', reference_sentence_id=1)
        manual = read_json(path)
        self.assertFalse(manual['reference_auto_recommend'])
        self.assertEqual(manual['reference_sentence_id'], 1)
        self.assertEqual(dict(self.calls), calls)
        self.assertEqual([s['tts_audio'] for s in automatic['segments']],
                         [s['tts_audio'] for s in manual['segments']])
        manual['segments'][1]['end'] -= .05
        write_json(path, manual)
        self.run_pipeline(stop_after='dub', require_translated=True, voice_mode='uniform')
        self.assertEqual(self.calls['speakers'], calls['speakers']+1)

    def test_three_steps_and_export_does_not_call_models(self):
        self.run_pipeline(stop_after='translate')
        for section in ('asr', 'translator', 'separator'):
            (Path(self.cfg[section]['model_path'])/'model.bin').unlink()
        self.run_pipeline(stop_after='dub', require_translated=True)
        self.assertFalse(self.output.exists())
        project = read_json(self.work/'project.json')
        self.assertNotIn('mux', project['stages'])
        self.assertTrue(all(valid_wav(s['aligned_audio']) for s in project['segments']))
        calls = dict(self.calls)
        (Path(self.cfg['tts']['model_path'])/'model.bin').unlink()
        self.run_pipeline(export_only=True)
        self.assertTrue(self.output.exists())
        self.assertEqual(dict(self.calls), calls)

    def test_forced_translation_replaces_edits_but_reuses_recognition(self):
        self.run_pipeline(stop_after='translate')
        path = self.work/'project.json'
        original = read_json(path)
        original['segments'][0]['target_text'] = '手动修改'
        write_json(path, original)
        calls = dict(self.calls)
        self.run_pipeline(stop_after='translate', force_translation=True)
        result = read_json(path)
        self.assertNotEqual(result['segments'][0]['target_text'], '手动修改')
        self.assertEqual(self.calls['asr'], calls['asr'])
        self.assertEqual(self.calls['translator'], calls['translator']+1)

    def test_changed_recognition_settings_do_not_mutate_existing_media_or_project(self):
        self.run_pipeline(stop_after='translate')
        path = self.work/'project.json'
        original = path.read_bytes()
        calls = dict(self.calls)
        for section, option, value in [('separator', 'overlap', .3), ('asr', 'beam_size', 7)]:
            with self.subTest(section=section):
                previous = self.cfg[section][option]
                self.cfg[section][option] = value
                try:
                    with self.assertRaisesRegex(VoxlateError, '识别模型或分离配置已变化'):
                        self.run_pipeline(stop_after='translate')
                    self.assertEqual(dict(self.calls), calls)
                    self.assertEqual(path.read_bytes(), original)
                finally:
                    self.cfg[section][option] = previous

    def test_forced_recognition_replaces_manual_results_and_backs_up_original(self):
        self.run_pipeline(stop_after='translate')
        self.run_pipeline(stop_after='dub',require_translated=True)
        path=self.work/'project.json'
        edited=read_json(path)
        old_start=edited['segments'][0]['start']
        edited['segments'][0].update(start=old_start+.1,manual_boundary=True,enabled=False,target_text='手动译文')
        edited.update(reference_sentence_id=2,reference_auto_recommend=False,roles_initialized=True)
        write_json(path,edited)
        calls=dict(self.calls)
        self.cfg['asr']['beam_size']=7
        self.run_pipeline(stop_after='translate',force_recognition=True,force_translation=True)
        result=read_json(path)
        self.assertEqual(result['segments'][0]['start'],old_start)
        self.assertTrue(result['segments'][0].get('enabled',True))
        self.assertNotEqual(result['segments'][0]['target_text'],'手动译文')
        self.assertTrue(all('tts_audio' not in s and 'manual_boundary' not in s for s in result['segments']))
        self.assertIsNone(result['reference_sentence_id'])
        self.assertFalse(result['roles_initialized'])
        self.assertEqual(self.calls['asr'],calls['asr']+1)
        self.assertEqual(self.calls['translator'],calls['translator']+1)
        self.assertEqual(self.calls['separator'],calls['separator'])
        self.assertEqual(read_json(result['recognition_history'][-1]['backup']),edited)

    def test_failed_fresh_recognition_or_translation_keeps_original_project(self):
        self.run_pipeline(stop_after='translate')
        path=self.work/'project.json'
        original=path.read_bytes()
        for failure in ('asr','translator','empty'):
            with self.subTest(failure=failure):
                def guarded(kind,job):
                    if kind==failure:
                        raise VoxlateError('synthetic failure')
                    if kind=='asr' and failure=='empty':
                        return []
                    return self.runner(kind,job)
                pipeline=VideoDubPipeline(self.cfg,runner=guarded)
                with self.assertRaises(VoxlateError):
                    pipeline.process(self.video,self.output,self.work,stop_after='translate',force_recognition=True)
                self.assertEqual(path.read_bytes(),original)

    def test_single_sentence_regeneration_preserves_others_and_blocks_stale_export(self):
        self.run_pipeline(stop_after='translate')
        self.run_pipeline(stop_after='dub', require_translated=True)
        path = self.work/'project.json'
        original = read_json(path)
        untouched = original['segments'][1]['tts_audio']
        original_bytes = Path(untouched).read_bytes()
        self.tts_ids.clear()
        self.run_pipeline(stop_after='dub', require_translated=True, sentence_ids=[1], force_tts=True)
        self.assertEqual(self.tts_ids, [1])
        regenerated = read_json(path)
        self.assertNotEqual(regenerated['segments'][0]['tts_audio'], original['segments'][0]['tts_audio'])
        self.assertEqual(regenerated['segments'][1]['tts_audio'], untouched)
        self.assertEqual(Path(untouched).read_bytes(), original_bytes)
        regenerated['segments'][1]['target_text'] = '改过的第二句'
        write_json(path, regenerated)
        with self.assertRaisesRegex(VoxlateError, '需要生成配音：2'):
            self.run_pipeline(export_only=True)
        self.assertFalse(self.output.exists())
        self.tts_ids.clear()
        self.run_pipeline(stop_after='dub', require_translated=True)
        self.assertEqual(self.tts_ids, [2])
        self.run_pipeline(export_only=True)
        self.assertTrue(self.output.exists())

    def test_first_single_sentence_dub_prepares_only_chosen_audio(self):
        self.run_pipeline(stop_after='translate', voice_mode='individual')
        path = self.work/'project.json'
        untouched = read_json(path)['segments'][1]
        pipeline = VideoDubPipeline(self.cfg, self.runner)
        with patch.object(pipeline.media, 'trim', wraps=pipeline.media.trim) as trim:
            pipeline.process(self.video, self.output, self.work, stop_after='dub',
                             require_translated=True, sentence_ids=[1], force_tts=True)
        source_clips = [call for call in trim.call_args_list if Path(call.args[1]).name.startswith('source_')]
        self.assertEqual(len(source_clips), 1)
        self.assertTrue(Path(source_clips[0].args[1]).name.startswith('source_1_'))
        self.assertEqual(self.tts_ids, [1])
        self.assertEqual(read_json(path)['segments'][1], untouched)
        self.assertEqual(len(list((self.work/'segments').glob('source_*.wav'))), 1)
        with self.assertRaisesRegex(VoxlateError, '需要生成配音：2'):
            self.run_pipeline(export_only=True)
        self.assertFalse(self.output.exists())

    def test_single_sentence_can_borrow_another_original_voice(self):
        self.run_pipeline(stop_after='translate', voice_mode='uniform')
        path = self.work/'project.json'
        project = read_json(path)
        project['segments'][0]['voice_reference_sentence_id'] = 2
        untouched = project['segments'][1].copy()
        write_json(path,project)
        pipeline = VideoDubPipeline(self.cfg,self.runner)
        with patch.object(pipeline,'analyze_reference_voices') as analyze, patch.object(pipeline.media,'trim',wraps=pipeline.media.trim) as trim:
            pipeline.process(self.video,self.output,self.work,stop_after='dub',require_translated=True,sentence_ids=[1])
            analyze.assert_not_called()
        references = [c for c in trim.call_args_list if Path(c.args[1]).name.startswith('sentence_reference_')]
        self.assertEqual(len(references),1)
        self.assertAlmostEqual(references[0].args[2],untouched['start'])
        self.assertAlmostEqual(references[0].args[3],untouched['end']-untouched['start'])
        current = read_json(path)
        self.assertEqual(current['segments'][0]['speaker_reference_audio'],str(references[0].args[1]))
        self.assertEqual(current['segments'][1],untouched)
        self.assertEqual(self.tts_ids,[1])

    def test_export_after_disabling_sentence_keeps_absolute_timeline(self):
        self.run_pipeline(stop_after='translate')
        self.run_pipeline(stop_after='dub', require_translated=True)
        path = self.work/'project.json'
        project = read_json(path)
        second = project['segments'][1]['start']
        project['segments'][0]['enabled'] = False
        write_json(path, project)
        self.tts_ids.clear()
        self.run_pipeline(export_only=True)
        current = read_json(path)
        self.assertEqual(self.tts_ids, [])
        self.assertEqual(current['segments'][1]['start'], second)
        self.assertEqual(current['segments'][0]['aligned_audio'], current['segments'][0]['source_audio'])

    def test_combined_cache_depends_on_both_models(self):
        for name in ('faster-whisper-large-v3', 'faster-whisper-large-v3-turbo'):
            folder = self.root / name
            folder.mkdir()
            (folder / 'model.bin').write_bytes(b'first')
        self.cfg['asr'].update(combined=True, model_path=str(self.root/'faster-whisper-large-v3'))
        self.run_pipeline(stop_after='translate')
        self.assertEqual(read_json(self.work/'project.json')['recognition_model'], 'asr_combined')
        (self.root/'faster-whisper-large-v3-turbo/model.bin').write_bytes(b'replacement-model')
        with self.assertRaisesRegex(VoxlateError, '识别模型或分离配置已变化'):
            self.run_pipeline(stop_after='translate')

    def test_japanese_project_preserves_language_and_cannot_reuse_english_project(self):
        self.cfg["source_lang"] = "ja"
        pipeline = VideoDubPipeline(self.cfg, runner=self.runner)
        self.assertEqual(pipeline.cfg["asr"]["language"], "ja")
        self.assertEqual(pipeline.cfg["translator"]["source_lang"], "ja")
        pipeline.process(self.video, self.output, self.work, stop_after="translate")
        project = read_json(self.work / "project.json")
        self.assertEqual(project["source_lang"], "ja")
        self.assertTrue(all(s["source_lang"] == "ja" for s in project["segments"]))
        self.cfg["source_lang"] = "en"
        with self.assertRaisesRegex(VoxlateError, "源语言与项目不同"):
            VideoDubPipeline(self.cfg, runner=self.runner).process(self.video, self.output, self.work, stop_after="translate")

    def setUp(self):
        self.temp = TestDirectory()
        self.root = Path(self.temp.name)
        self.cfg = load_config(Path(__file__).resolve().parents[1] / "config.json")
        self.cfg['asr']['combined'] = False
        for section in ("asr", "translator", "separator", "tts"):
            model = self.root / section
            model.mkdir()
            (model / "model.bin").write_bytes(b"test-model")
            self.cfg[section]["model_path"] = str(model)
        repo = self.root / "tts-repo"
        (repo / "indextts").mkdir(parents=True)
        (repo / "indextts/infer_v2_5.py").write_text("# test adapter")
        self.cfg["tts"]["repo_path"] = str(repo)
        self.media = Media(self.cfg)
        self.video = self.root / "英文 input.mp4"
        self.media.render(["-f", "lavfi", "-i", "color=c=blue:s=160x90:r=25:d=3",
                           "-f", "lavfi", "-i", "sine=frequency=900:duration=3",
                           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest"], self.video)
        self.work = self.root / "project"
        self.output = self.root / "output.mp4"
        self.calls = Counter()
        self.tts_ids = []
        self.fail_once = False

    def tearDown(self):
        self.temp.cleanup()

    def runner(self, kind, job):
        self.calls[kind] += 1
        if kind == 'speakers':
            return dict(roles=[dict(id='a', name='角色A', reference_sentence_id=1)],
                        assignments={str(s['id']): dict(role_id='a', role_uncertain=False) for s in job['segments']},
                        recommended_sentence_id=1)
        if kind == "separator":
            folder = Path(job["directory"]) / self.cfg[kind]["model"] / Path(job["audio"]).stem
            folder.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(job["audio"], folder / "vocals.wav")
            tone(folder / "no_vocals.wav", 3, frequency=220, amplitude=0.05)
            return str(folder / "vocals.wav"), str(folder / "no_vocals.wav")
        if kind == "asr":
            return [{"id": 1, "start": 0.25, "end": 1.1, "speaker": "A", "source_text": "Hello.", "target_text": ""},
                    {"id": 2, "start": 1.6, "end": 2.5, "speaker": "A", "source_text": "Welcome.", "target_text": ""}]
        if kind == "translator":
            return ["你好。" if s == "Hello." else "欢迎。" for s in job["texts"]]
        if kind == "tts":
            for seg in job["segments"]:
                self.tts_ids.append(seg["id"])
                tone(seg["tts_audio"], 1.25, frequency=440)
                if self.fail_once:
                    self.fail_once = False
                    raise VoxlateError("Simulated interrupted synthesis")
            return True
        self.fail(kind)

    def run_pipeline(self, **kwargs):
        return VideoDubPipeline(self.cfg, self.runner).process(self.video, self.output, self.work, **kwargs)

    def test_render_resume_and_single_sentence_edit(self):
        self.run_pipeline()
        self.assertEqual(self.calls, Counter(separator=1, asr=1, translator=1, tts=1))
        info = self.media.probe(self.output)
        self.assertEqual([s["codec_type"] for s in info["streams"]], ["video", "audio", "audio"])
        self.assertEqual(info["streams"][1]["disposition"]["default"], 1)
        self.assertEqual(info["streams"][2]["disposition"]["default"], 0)
        self.assertEqual(info["streams"][2]["tags"]["handler_name"], "Voxlate Original")
        self.assertAlmostEqual(float(info["format"]["duration"]), 3, delta=0.1)
        # Unrecognized gaps preserve the original 900 Hz signal, not separated background.
        with wave.open(str(self.work / "final_audio.wav")) as audio:
            audio.setpos(int(1.25 * 44100))
            values = struct.unpack("<" + "h" * (4410 * 2), audio.readframes(4410))[::2]
        def strength(freq):
            return abs(sum(v * complex(math.cos(2 * math.pi * freq * i / 44100),
                                       math.sin(2 * math.pi * freq * i / 44100)) for i, v in enumerate(values)))
        self.assertGreater(strength(900), strength(220) * 20)
        before = dict(self.calls)
        self.run_pipeline()
        self.assertEqual(dict(self.calls), before)
        project = read_json(self.work / "project.json")
        original_second = project["segments"][1]["tts_audio"]
        project["segments"][0]["target_text"] = "大家好！"
        write_json(self.work / "project.json", project)
        self.tts_ids.clear()
        self.run_pipeline()
        self.assertEqual(self.tts_ids, [1])
        self.assertEqual(self.calls["asr"], 1)
        self.assertEqual(self.calls["translator"], 1)
        self.assertEqual(read_json(self.work / "project.json")["segments"][1]["tts_audio"], original_second)

    def test_translated_snapshot_is_emitted_before_tts_and_detached_from_mutations(self):
        events = []
        class Capture(logging.Handler):
            def emit(self, record):
                update = getattr(record, "voxlate_progress", {})
                if update.get("stage") == "project_ready":
                    events.append(update)
        logger = logging.getLogger("voxlate")
        handler, previous = Capture(), logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        original_runner = self.runner
        def runner(kind, job):
            if kind == "tts":
                self.assertEqual(len(events), 1)
                self.assertTrue(all(s['target_text'] for s in events[0]['project']['segments']))
                self.assertNotIn('tts_audio', events[0]['project']['segments'][0])
            return original_runner(kind, job)
        try:
            VideoDubPipeline(self.cfg, runner).process(self.video, self.output, self.work)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)
        self.assertNotIn('tts_audio', events[0]['project']['segments'][0])
        self.assertEqual(events[0]['path'], str(self.work / 'project.json'))

    def test_resume_after_partial_tts_failure(self):
        self.fail_once = True
        with self.assertRaises(VoxlateError):
            self.run_pipeline()
        self.assertFalse((self.work / ".lock").exists())
        self.tts_ids.clear()
        self.run_pipeline()
        self.assertEqual(self.tts_ids, [2])
        self.assertEqual(self.calls["asr"], 1)

    def test_export_restores_unrecognized_gaps_without_regenerating_dubbing(self):
        self.run_pipeline()
        counts = self.calls.copy()
        path = self.work/'project.json'
        project = read_json(path)
        project['stages']['mix']['key'] = 'previous-export-with-background-only-gaps'
        write_json(path, project)
        self.run_pipeline(export_only=True)
        self.assertEqual(self.calls, counts)
        with wave.open(str(self.work/'original.wav')) as original, wave.open(str(self.work/'final_audio.wav')) as mixed:
            rate = original.getframerate()
            self.assertEqual(original.getnframes(), mixed.getnframes())
            for start, end in ((0,.25),(1.1,1.6),(2.5,3)):
                first,last = round(start*rate),round(end*rate)
                original.setpos(first); mixed.setpos(first)
                self.assertEqual(mixed.readframes(last-first),original.readframes(last-first))
            original.setpos(round(.3*rate)); mixed.setpos(round(.3*rate))
            self.assertNotEqual(mixed.readframes(1000),original.readframes(1000))

    def test_unchecked_sentence_keeps_original_voice_and_can_reuse_tts_when_reenabled(self):
        self.run_pipeline(stop_after="translate")
        path = self.work / "project.json"
        project = read_json(path)
        project["segments"][0]["enabled"] = False
        write_json(path, project)
        self.run_pipeline()
        self.assertEqual(self.tts_ids, [2])
        original_mix_key = read_json(path)["stages"]["mix"]["key"]
        with wave.open(str(self.work / "dubbing.wav")) as audio:
            audio.setpos(round(1.6 * audio.getframerate()))
            later_audio = audio.readframes(audio.getnframes())
            total_frames = audio.getnframes()

        # Restore original PCM, not the separator's synthetic 220 Hz background.
        with wave.open(str(self.work / "final_audio.wav")) as audio:
            audio.setpos(int(0.5 * 44100))
            samples = struct.unpack("<" + "h" * (4410 * 2), audio.readframes(4410))[::2]
        def strength(freq):
            return abs(sum(v * complex(math.cos(2 * math.pi * freq * i / 44100),
                                       math.sin(2 * math.pi * freq * i / 44100)) for i, v in enumerate(samples)))
        self.assertGreater(strength(900), strength(440) * 20)
        self.assertGreater(strength(900), strength(220) * 20)
        with wave.open(str(self.work/'original.wav')) as original, wave.open(str(self.work/'final_audio.wav')) as mixed:
            start, end = round(.25*44100), round(1.1*44100)
            original.setpos(start)
            mixed.setpos(start)
            self.assertEqual(mixed.readframes(end-start), original.readframes(end-start))

        project = read_json(path)
        project["segments"][0]["enabled"] = True
        write_json(path, project)
        self.tts_ids.clear()
        self.run_pipeline()
        self.assertEqual(self.tts_ids, [1])
        self.assertNotEqual(read_json(path)["stages"]["mix"]["key"], original_mix_key)
        with wave.open(str(self.work / "dubbing.wav")) as audio:
            self.assertEqual(audio.getnframes(), total_frames)
            audio.setpos(round(1.6 * audio.getframerate()))
            self.assertEqual(audio.readframes(audio.getnframes()), later_audio)
        self.tts_ids.clear()
        for enabled in (False, True):
            project = read_json(path)
            project["segments"][0]["enabled"] = enabled
            write_json(path, project)
            self.run_pipeline()
        self.assertEqual(self.tts_ids, [])

    def test_all_unchecked_translates_but_skips_tts_and_exports_original(self):
        self.run_pipeline(stop_after="translate")
        path = self.work / "project.json"
        project = read_json(path)
        for s in project["segments"]:
            s.update(enabled=False, target_text="")
        write_json(path, project)
        self.cfg["tts"]["repo_path"] = str(self.root / "no-tts-needed")
        self.run_pipeline()
        self.assertTrue(self.output.is_file())
        self.assertEqual(self.calls["tts"], 0)
        self.assertEqual(self.calls["translator"], 2)
        project = read_json(path)
        self.assertTrue(all(s['target_text'] and not s['enabled'] for s in project['segments']))
        self.assertTrue(all(s["aligned_audio"] == s["source_audio"] for s in project["segments"]))

    def test_translation_and_one_click_include_discarded_sentences_without_dubbing_them(self):
        self.run_pipeline(stop_after='recognize')
        path = self.work/'project.json'
        project = read_json(path)
        project['segments'][0].update(enabled=False, timing_fallback=True)
        write_json(path, project)
        self.run_pipeline(stop_after='translate', translate_only=True)
        translated = read_json(path)
        self.assertEqual([s['target_text'] for s in translated['segments']], ['你好。', '欢迎。'])
        self.assertFalse(translated['segments'][0]['enabled'])
        translated['segments'][0]['target_text'] = ''
        write_json(path, translated)
        self.run_pipeline(auto_export=True)
        result = read_json(path)
        self.assertEqual(result['segments'][0]['target_text'], '你好。')
        self.assertFalse(result['segments'][0]['enabled'])
        self.assertEqual(self.tts_ids, [2])
        self.assertEqual(result['segments'][0]['aligned_audio'], result['segments'][0]['source_audio'])

    def test_translate_only_and_input_protection(self):
        self.run_pipeline(stop_after="translate")
        self.assertFalse(self.output.exists())
        self.assertEqual(self.calls["tts"], 0)
        with self.assertRaises(VoxlateError):
            VideoDubPipeline(self.cfg, self.runner).process(self.video, self.video, self.work)
        with self.assertRaises(VoxlateError):
            VideoDubPipeline(self.cfg, self.runner).process(self.video, self.work / "output.mp4", self.work)

    def test_reviewed_export_never_silently_translates(self):
        with self.assertRaisesRegex(VoxlateError, "先完成"):
            self.run_pipeline(require_translated=True)
        self.assertEqual(sum(self.calls.values()), 0)
        self.run_pipeline(stop_after="translate", auto_reference=False)
        self.assertFalse(read_json(self.work / "project.json")["auto_reference"])
        self.run_pipeline(require_translated=True, auto_reference=True)
        self.assertEqual(self.calls["translator"], 1)
        self.cfg["translator"]["beam_size"] += 1
        with self.assertRaisesRegex(VoxlateError, "译文需要更新"):
            self.run_pipeline(require_translated=True)
        self.assertEqual(self.calls["translator"], 1)

    def test_mp4_incompatible_video_is_transcoded_with_existing_audio(self):
        incompatible = self.root / "lossless.mkv"
        audio = self.root / "dub.wav"
        tone(audio, 3)
        self.media.render(["-i", self.video, "-an", "-c:v", "ffv1"], incompatible)
        self.media.mux(incompatible, audio, self.output, 3)
        streams = self.media.probe(self.output)["streams"]
        self.assertEqual([s["codec_name"] for s in streams], ["h264", "aac"])
        self.assertAlmostEqual(self.media.duration(self.output), 3, delta=0.1)
        for message in ("已取消", "No space left on device"):
            with patch.object(self.media, "render", side_effect=VoxlateError(message)) as render:
                with self.assertRaisesRegex(VoxlateError, message):
                    self.media.mux(incompatible, audio, self.output, 3)
                self.assertEqual(render.call_count, 1)

    def test_selected_reference_is_trimmed_from_separated_voice_and_persisted(self):
        self.run_pipeline(reference_range=[0, 3])
        project = read_json(self.work / "project.json")
        self.assertEqual(project["reference_range"], [0, 3])
        self.assertAlmostEqual(self.media.duration(self.work / "speaker_A.wav"), 3, delta=0.03)
        old_key = project["segments"][0]["tts_key"]
        self.run_pipeline()
        self.assertNotEqual(read_json(self.work / "project.json")["segments"][0]["tts_key"], old_key)
        with self.assertRaisesRegex(VoxlateError, "起止时间"):
            self.run_pipeline(reference_range=[1, 4])

    def test_deleted_tts_invalidates_old_alignment(self):
        self.run_pipeline()
        first = read_json(self.work / "project.json")["segments"][0]
        old_alignment = first["aligned_audio"]
        Path(first["tts_audio"]).unlink()
        original_runner = self.runner

        def changed_tts(kind, job):
            if kind == "tts":
                for s in job["segments"]:
                    tone(s["tts_audio"], 0.7, frequency=600)
                return True
            return original_runner(kind, job)

        VideoDubPipeline(self.cfg, changed_tts).process(self.video, self.output, self.work)
        first = read_json(self.work / "project.json")["segments"][0]
        self.assertNotEqual(first["aligned_audio"], old_alignment)
        self.assertAlmostEqual(first["generated_duration"], 0.7, places=2)


if __name__ == "__main__":
    unittest.main()
