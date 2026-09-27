import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

from test_pipeline import TestDirectory, tone
from voxlate.common import load_config, read_json, write_json
from voxlate.pipeline import VideoDubPipeline
from voxlate.progress import TTSProgress, report_tts, separation_status
from voxlate.worker import main


class TTSProgressTests(unittest.TestCase):
    def test_separator_status_uses_latest_round_and_has_honest_fallback(self):
        with TestDirectory() as folder:
            log=Path(folder)/'separator.log'
            self.assertEqual(separation_status(log,'Demucs htdemucs_ft',65),
                             '人声分离（Demucs htdemucs_ft）· 处理中 · 已用 01:05')
            log.write_bytes(b'100%|finished\r\n  0%|next round\r 42%|progress')
            self.assertIn('本轮 42%',separation_status(log,'Demucs htdemucs_ft',90))
            log.write_bytes(b'100%|finished\r\n')
            self.assertIn('后续处理中',separation_status(log,'Demucs htdemucs_ft',90))

    def test_loading_sentence_count_cache_and_idle_are_distinct(self):
        with TestDirectory() as folder:
            state, log = Path(folder) / "state.json", Path(folder) / "tts.log"
            now = [100]
            tracker = TTSProgress(state, log, total=46, cached=10, clock=lambda: now[0])
            self.assertIn("10/46", tracker.snapshot()["detail"])
            self.assertIn("正在加载音色克隆模型（IndexTTS 2.5）", tracker.snapshot()["detail"])
            report_tts(state, 'preparing', 10, 46)
            self.assertIn('正在准备参考音频', tracker.snapshot()['detail'])
            self.assertNotIn('正在加载', tracker.snapshot()['detail'])
            write_json(state, dict(phase="generating", completed=11, current=15, since=100))
            now[0] = 165
            update = tracker.snapshot()
            self.assertEqual(update["completed"], 11)
            self.assertIn("正在生成第 15 句", update["detail"])
            self.assertNotIn("本句已用", update["detail"])
            self.assertIn("65 秒无新日志", update["detail"])
            log.write_text("new activity")
            self.assertNotIn("无新日志", tracker.snapshot()["detail"])
            state.write_text("{incomplete")
            self.assertEqual(tracker.snapshot()["completed"], 11)

    def test_progress_write_failure_does_not_fail_synthesis(self):
        with patch("voxlate.progress.write_json", side_effect=PermissionError("locked")):
            report_tts("locked.json", "generating", 1, 3, 2)

    def test_worker_counts_only_finished_audio_and_reuses_cached_sentence(self):
        with TestDirectory() as folder:
            folder = Path(folder)
            old = folder / "old.wav"
            tone(old, 0.1)
            fresh = folder / "fresh.wav"
            request = folder / "job.json"
            result, progress = folder / "result.json", folder / "progress.json"
            job = dict(kind="tts", config=dict(repo_path=str(folder), emotion_reference=False),
                       reference=str(old), total=3, result=str(result), progress=str(progress),
                       segments=[dict(id=2, tts_audio=str(old), target_text="旧句"),
                                 dict(id=3, tts_audio=str(fresh), target_text="新句", speaker_reference_audio='per-sentence-reference.wav')])
            write_json(request, job)
            engine = Mock()
            engine.generate.side_effect = lambda text, ref, output, emotion: tone(output, 0.1)
            previous_directory = Path.cwd()
            try:
                with patch("voxlate.worker.enable_offline"), patch.object(sys, "argv", ["worker", str(request)]), patch("voxlate.tts.TTSEngine", return_value=engine):
                    main()
            finally:
                os.chdir(previous_directory)
            self.assertEqual(engine.generate.call_count, 1)
            self.assertEqual(engine.generate.call_args.args[1], 'per-sentence-reference.wav')
            self.assertTrue(read_json(result))
            self.assertEqual(read_json(progress)["completed"], 3)
            self.assertEqual(read_json(progress)["phase"], "completed")

    def test_worker_progress_reaches_parent_without_reading_raw_log(self):
        with TestDirectory() as folder:
            pipeline = VideoDubPipeline(load_config(Path(__file__).resolve().parents[1] / "config.json"))
            pipeline.work = Path(folder)
            code = "import json,sys,time; from pathlib import Path; j=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8')); p=Path(j['progress']); p.write_text(json.dumps(dict(phase='generating',completed=0,current=1,since=time.time())),encoding='utf-8'); time.sleep(1.3); p.write_text(json.dumps(dict(phase='completed',completed=1,since=time.time())),encoding='utf-8'); Path(j['result']).write_text('true')"
            with patch("voxlate.pipeline.worker_command", side_effect=lambda cfg, kind, request: [sys.executable, "-u", "-c", code, str(request)]):
                with self.assertLogs("voxlate", level="INFO") as logs:
                    self.assertTrue(pipeline.run_worker("tts", {"segments": [{"id": 1}], "total": 1}))
            updates = [record.voxlate_progress for record in logs.records if hasattr(record, "voxlate_progress")]
            self.assertTrue(any(item["completed"] == 0 and "正在生成第 1 句" in item["detail"] for item in updates))
            self.assertEqual(updates[-1]["completed"], 1)
