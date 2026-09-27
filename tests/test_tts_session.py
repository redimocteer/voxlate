import sys
import threading
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from test_pipeline import TestDirectory
from voxlate.common import load_config, read_json, VoxlateError
from voxlate.pipeline import VideoDubPipeline
from voxlate.tts_session import TTSSession
from voxlate.media import set_cancel_event


FAKE = '''import sys, time, wave
from pathlib import Path
sys.path.insert(0, sys.argv.pop(1))
from voxlate import tts, worker
class Engine:
    def __init__(self, cfg):
        with open('loads.txt', 'a') as f: f.write('loaded\\n')
    def reset_reference_cache(self):
        with open('resets.txt', 'a') as f: f.write('reset\\n')
    def generate(self, text, ref, output, emotion):
        if text == 'fail': raise RuntimeError('synthetic failure')
        if text == 'wait': time.sleep(30)
        with wave.open(output, 'wb') as f:
            f.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            f.writeframes(b'\\x00\\x00'*2400)
tts.TTSEngine=Engine
worker.main()
'''


class ResidentTTSTests(TestCase):
    def test_reuses_model_and_restarts_after_settings_change_failure_and_parent_eof(self):
        with TestDirectory() as folder:
            root = Path(folder)
            helper = root/'fake_worker.py'
            helper.write_text(FAKE, encoding='utf-8')
            cfg = load_config(Path(__file__).resolve().parents[1]/'config.json')
            cfg['tts']['model_path'] = str(root/'models')
            Path(cfg['tts']['model_path']).mkdir()
            messages = []
            session = TTSSession(messages.append)
            p = VideoDubPipeline(cfg, tts_session=session)
            p.work = root
            def job(number, text='test'):
                return dict(reference=str(root/'ref.wav'), segments=[dict(id=number, target_text=text,
                    source_audio=str(root/'source.wav'), tts_audio=str(root/f'{number}.wav'))], total=1)
            command = lambda cfg, kind, request: [sys.executable, '-u', str(helper), str(Path(__file__).resolve().parents[1]), str(request)]
            try:
                with patch('voxlate.tts_session.worker_command', side_effect=command):
                    self.assertTrue(p.run_worker('tts', job(1)))
                    first = session.process
                    self.assertTrue(p.run_worker('tts', job(2)))
                    self.assertIs(first, session.process)
                    self.assertEqual((root/'loads.txt').read_text().count('loaded'), 1)
                    self.assertTrue(read_json(root/'tts_metrics.json')['reused'])
                    self.assertTrue((root/'resets.txt').exists())
                    lifecycle = (root/'run.log').read_text(encoding='utf-8')
                    self.assertIn('复用音色克隆模型（IndexTTS 2.5）', lifecycle)
                    self.assertEqual(lifecycle.count('正在加载音色克隆模型'), 1)
                    session.close('手动释放')
                    self.assertFalse(session.is_alive)
                    self.assertIsNotNone(first.poll())
                    self.assertTrue((root/'1.wav').is_file())
                    p.run_worker('tts', job(6))
                    self.assertTrue(session.is_alive)
                    self.assertIsNot(first, session.process)
                    self.assertFalse(read_json(root/'tts_metrics.json')['reused'])
                    first = session.process
                    p.cfg['tts']['use_bf16'] = not p.cfg['tts']['use_bf16']
                    p.run_worker('tts', job(3))
                    self.assertIsNot(first, session.process)
                    self.assertIsNotNone(first.poll())
                    self.assertIn('已释放音色克隆模型（IndexTTS 2.5）', messages[-1])
                    self.assertIn('设置已变化', messages[-1])
                    with self.assertRaises(VoxlateError):
                        p.run_worker('tts', job(4, 'fail'))
                    self.assertIsNone(session.process)
                    p.run_worker('tts', job(5))
                    process = session.process
                    process.stdin.close()
                    self.assertEqual(process.wait(timeout=5), 0)
            finally:
                session.close()

    def test_cancel_terminates_resident_worker(self):
        with TestDirectory() as folder:
            root = Path(folder)
            helper = root/'fake_worker.py'
            helper.write_text(FAKE, encoding='utf-8')
            cfg = load_config(Path(__file__).resolve().parents[1]/'config.json')
            cfg['tts']['model_path'] = str(root/'models')
            Path(cfg['tts']['model_path']).mkdir()
            session = TTSSession()
            p = VideoDubPipeline(cfg, tts_session=session)
            p.work = root
            event = threading.Event()
            set_cancel_event(event)
            timer = threading.Timer(.6, event.set)
            timer.start()
            try:
                with patch('voxlate.tts_session.worker_command', return_value=[sys.executable, '-u', str(helper),
                        str(Path(__file__).resolve().parents[1]), str(root/'tts_job.json')]):
                    with self.assertRaisesRegex(VoxlateError, '已取消'):
                        p.run_worker('tts', dict(reference='ref', segments=[dict(id=1, target_text='wait', source_audio='source',
                            tts_audio=str(root/'waiting.wav'))], total=1))
                    self.assertIsNone(session.process)
            finally:
                timer.cancel()
                set_cancel_event(None)
                session.close()
