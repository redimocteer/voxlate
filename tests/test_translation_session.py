"""Synthetic resident translation lifecycle; never load real models or user media."""
import sys
import threading
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch, Mock

from test_pipeline import TestDirectory
from voxlate.common import load_config, VoxlateError
from voxlate.media import set_cancel_event
from voxlate.pipeline import VideoDubPipeline
from voxlate.tts_session import TTSSession
from voxlate.translation_models import MODELS

FAKE = '''import sys, json, time
from pathlib import Path
sys.path.insert(0, sys.argv.pop(1))
from voxlate import hy_translator, worker
class Session:
    def __init__(self, cfg, model, folder, work):
        self.folder=folder
    def __enter__(self):
        with open('loads.txt','a') as f: f.write('loaded\\n')
        return self
    def translate(self,prompt,schema):
        if 'SYNTHETIC_FAIL' in prompt: raise RuntimeError('synthetic failure')
        if 'SYNTHETIC_WAIT' in prompt: time.sleep(30)
        return json.dumps({k:'translated '+k for k in schema['required']})
hy_translator.HySession=Session
worker.main()
'''


class TranslationSessionTests(TestCase):
    def test_reuse_across_editor_requests_mutual_exclusion_failure_cancel_and_scope(self):
        with TestDirectory() as folder:
            root=Path(folder)
            helper=root/'fake.py'; helper.write_text(FAKE,encoding='utf-8')
            cfg=load_config(Path(__file__).resolve().parents[1]/'config.json')
            model=root/'model'; model.mkdir()
            (model/MODELS['hy7_model']['filename']).write_bytes(b'synthetic')
            engine=root/'llama.exe'; engine.write_bytes(b'synthetic')
            cfg['translator'].update(model_path=str(model),engine_path=str(engine))
            session=TTSSession(kind='translator')
            tts=Mock()
            pipeline=VideoDubPipeline(cfg,tts_session=tts,translation_session=session)
            pipeline.session_root=root/'synthetic.mp4.voxlate'; pipeline.session_root.mkdir()
            def run(index,text='Synthetic'):
                pipeline.work=pipeline.session_root/'.temp'/str(index)
                pipeline.work.mkdir(parents=True,exist_ok=True)
                return pipeline.run_worker('translator',dict(texts=[text]))
            command=lambda cfg,kind,request:[sys.executable,'-u',str(helper),str(Path(__file__).resolve().parents[1]),str(request)]
            try:
                with patch('voxlate.tts_session.worker_command',side_effect=command):
                    self.assertEqual(run(1),['translated 1'])
                    first=session.process
                    self.assertEqual(run(2),['translated 1'])
                    self.assertIs(session.process,first)
                    self.assertEqual((pipeline.session_root/'.temp/1/loads.txt').read_text().count('loaded'),1)
                    self.assertEqual(tts.close.call_count,2)
                    # Starting TTS closes the translator before submitting the next model.
                    def verify_release(*args):
                        self.assertFalse(session.is_alive)
                        raise VoxlateError('synthetic stop before TTS launch')
                    tts.submit.side_effect=verify_release
                    with self.assertRaisesRegex(VoxlateError,'synthetic stop'):
                        pipeline.run_worker('tts',dict(segments=[],total=0))
                    self.assertIsNotNone(first.poll())
                    run(3)
                    with self.assertRaises(VoxlateError): run(4,'SYNTHETIC_FAIL')
                    self.assertFalse(session.is_alive)
                    run(5)
                    event=threading.Event(); timer=threading.Timer(.5,event.set)
                    set_cancel_event(event); timer.start()
                    try:
                        with self.assertRaisesRegex(VoxlateError,'已取消'): run(6,'SYNTHETIC_WAIT')
                        self.assertFalse(session.is_alive)
                    finally:
                        timer.cancel(); set_cancel_event(None)
                    run(7)
                    prior=session.process
                    pipeline.session_root=root/'another.mp4.voxlate'; pipeline.session_root.mkdir()
                    run(1)
                    self.assertIsNot(session.process,prior)
                    self.assertIsNotNone(prior.poll())
                    last=session.process
                    session.close('手动释放')
                    self.assertIsNotNone(last.poll())
            finally:
                session.close()
