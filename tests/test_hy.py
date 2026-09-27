import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch, Mock

from test_pipeline import TestDirectory
from voxlate.common import VoxlateError, load_config, write_json, read_json
from voxlate.hy_translator import HyTranslator, parse_translations, protect_child
from voxlate.installer import planned_stages, relocate
from voxlate.diagnostics import check_resources
from voxlate.translation_models import MODELS, selected_key, select_model


class HyTests(unittest.TestCase):
    def make_translator(self, root):
        model = root/'models'
        model.mkdir()
        (model/MODELS['hy7_model']['filename']).write_bytes(b'synthetic')
        engine = root/'engine.exe'
        engine.write_bytes(b'synthetic')
        return HyTranslator(dict(model_type=MODELS['hy7_model']['kind'], model_path=str(model),
                                 engine_path=str(engine)), work_dir=root/'video.mp4.voxlate'/'en')

    def test_failed_batches_resume_and_successful_retranslation_starts_fresh(self):
        with TestDirectory() as folder:
            translator = self.make_translator(Path(folder))
            texts = [f'Synthetic sentence {i}' for i in range(17)]
            checkpoint = translator.work_dir/'.temp'/'translation-resume.json'
            with patch('voxlate.hy_translator.HySession') as session:
                run = session.return_value.__enter__.return_value.translate
                run.side_effect = [json.dumps({str(i+1):f'saved {i}' for i in range(8)}), VoxlateError('interrupted')]
                with self.assertRaisesRegex(VoxlateError, 'interrupted'):
                    translator.translate_many(texts)
                self.assertEqual(len(read_json(checkpoint)['translations']), 8)
                self.assertEqual(list(checkpoint.parent.iterdir()), [checkpoint])
                before = checkpoint.read_bytes()
                run.side_effect = ['{"1":"incomplete batch"}']
                with self.assertRaises(VoxlateError):
                    translator.translate_many(texts)
                self.assertEqual(checkpoint.read_bytes(), before)
                run.reset_mock()
                run.side_effect = lambda prompt,schema: json.dumps({k:'new '+k for k in schema['required']})
                # Recreate the translator to simulate a fresh worker after restart.
                result = HyTranslator(translator.cfg,work_dir=translator.work_dir).translate_many(texts)
                self.assertEqual(result[:8], [f'saved {i}' for i in range(8)])
                self.assertEqual(len(result), 17)
                self.assertEqual(run.call_count, 2)
                self.assertFalse(checkpoint.exists())
                run.reset_mock()
                translator.translate_many(texts)
                self.assertEqual(run.call_count, 3)

    def test_resume_does_not_reuse_changed_requests_models_or_broken_checkpoints(self):
        for changed in ('text', 'context', 'indices', 'settings', 'model', 'corrupt'):
            with self.subTest(changed=changed), TestDirectory() as folder:
                translator = self.make_translator(Path(folder))
                texts = ['Synthetic']*17
                context = ['Synthetic']*19
                indices = list(range(17))
                checkpoint = translator.work_dir/'.temp'/'translation-resume.json'
                with patch('voxlate.hy_translator.HySession') as session:
                    run = session.return_value.__enter__.return_value.translate
                    run.side_effect = [json.dumps({str(i+1):'old' for i in range(8)}), VoxlateError('stopped')]
                    with self.assertRaises(VoxlateError):
                        translator.translate_many(texts, context, indices)
                    if changed == 'text': texts[0] = 'Changed'
                    elif changed == 'context': context[-1] = 'Different background'
                    elif changed == 'indices': indices = list(range(1,18))
                    elif changed == 'settings': translator.cfg['source_lang'] = 'ja'
                    elif changed == 'model': translator.model.write_bytes(b'changed synthetic model')
                    elif changed == 'corrupt': checkpoint.write_text('broken',encoding='utf-8')
                    run.reset_mock()
                    run.side_effect = lambda prompt,schema: json.dumps({k:'fresh' for k in schema['required']})
                    result = translator.translate_many(texts,context,indices)
                    self.assertEqual(result, ['fresh']*17)
                    self.assertEqual(run.call_count, 3)

    def test_translation_files_stay_in_project_and_cleanup_on_success_or_error(self):
        with TestDirectory() as folder:
            root = Path(folder)
            item = MODELS['hy7_model']
            model = root / 'models'
            model.mkdir()
            (model / item['filename']).write_bytes(b'fixture')
            engine = root / 'engine.exe'
            engine.write_bytes(b'fixture')
            cfg = dict(model_type=item['kind'], model_path=str(model), engine_path=str(engine))
            project = root / 'video.mp4.voxlate' / 'en'
            folders = []

            def translate(prompt, schema):
                batch=folders[-1]
                self.assertEqual(batch.parent,project/'.temp')
                self.assertIn('Private test dialogue',prompt)
                self.assertEqual((batch/'prompt.txt').read_text(encoding='utf-8'),prompt)
                self.assertEqual(read_json(batch/'schema.json'),schema)
                if error:
                    raise VoxlateError('本地翻译运行失败')
                return json.dumps({k:'测试译文' for k in schema['required']},ensure_ascii=False)

            for error in (False, True):
                with patch('voxlate.hy_translator.HySession') as session:
                    def construct(cfg,model,directory,work_dir):
                        folders.append(directory)
                        return session.return_value
                    session.side_effect=construct
                    session.return_value.__enter__.return_value.translate.side_effect=translate
                    translator = HyTranslator(cfg, work_dir=project)
                    if error:
                        with self.assertRaisesRegex(VoxlateError, '本地翻译运行失败'):
                            translator.translate_many(['Private test dialogue'])
                    else:
                        self.assertEqual(translator.translate_many(['Private test dialogue']*17), ['测试译文']*17)
                        self.assertEqual(session.return_value.__enter__.return_value.translate.call_count,3)
                    session.assert_called_once()
                    session.return_value.__exit__.assert_called_once()
                self.assertFalse(folders[-1].exists())
                self.assertEqual(list((project/'.temp').iterdir()), [])

    def test_native_session_loads_once_and_closes_after_success_or_error(self):
        from voxlate.hy_session import HySession
        with TestDirectory() as directory:
            root=Path(directory)
            (root/'llama-server.exe').write_bytes(b'fixture')
            cfg=dict(engine_path=str(root/'llama-completion.exe'),model_type='hy_mt2_7b')
            for error in (False,True):
                process=Mock(poll=Mock(return_value=None))
                cleanup=Mock()
                def spawn(args,**kwargs):
                    self.assertIn('--offline',args)
                    self.assertEqual(args[args.index('--host')+1],'127.0.0.1')
                    self.assertEqual(args[args.index('--port')+1],'0')
                    self.assertEqual(Path(kwargs['cwd']),root)
                    key_arg=args[args.index('--api-key-file')+1]
                    self.assertEqual(key_arg,'session.key')
                    self.assertTrue((Path(kwargs['cwd'])/key_arg).is_file())
                    for key in ('TEMP','TMP','TMPDIR'):
                        self.assertEqual(Path(kwargs['env'][key]),root)
                    kwargs['stdout'].write('listening on http://127.0.0.1:12345\n')
                    kwargs['stdout'].flush()
                    return process
                response=(200,dict(choices=[dict(message=dict(content='{"1":"你好"}'),finish_reason='stop')]))
                with patch('voxlate.hy_session.spawn_external',side_effect=spawn) as launch, \
                     patch('voxlate.hy_translator.protect_child',return_value=cleanup), \
                     patch.object(HySession,'request',side_effect=[(200,{'status':'ok'}),response,
                         VoxlateError('test error') if error else response]) as request:
                    try:
                        with HySession(cfg,root/'model.gguf',root,root) as session:
                            session.translate('batch1',{})
                            session.translate('batch2',{})
                    except VoxlateError:
                        self.assertTrue(error)
                    launch.assert_called_once()
                    process.terminate.assert_called_once()
                    process.wait.assert_called_once()
                    cleanup.assert_called_once()
                    payloads=[c.args[2] for c in request.call_args_list if c.args[0]=='POST']
                    self.assertEqual([p['messages'] for p in payloads],
                        [[dict(role='user',content='batch1')],[dict(role='user',content='batch2')]])
                    self.assertTrue(all(not p['cache_prompt'] for p in payloads))

    def test_offline_guard_only_allows_scoped_numeric_local_endpoint(self):
        code='''
import socket, sys
from voxlate.common import enable_offline, local_inference_connection, VoxlateError
enable_offline()
sock=socket.socket()
def blocked(event,*args):
    try: sys.audit(event,*args)
    except VoxlateError: return
    raise AssertionError('Unexpected network permission')
blocked('socket.connect',sock,('127.0.0.1',12345))
with local_inference_connection(12345):
    sys.audit('socket.connect',sock,('127.0.0.1',12345))
    blocked('socket.connect',sock,('127.0.0.1',12346))
    blocked('socket.connect',sock,('192.168.1.1',12345))
    blocked('socket.connect',sock,('8.8.8.8',12345))
    blocked('socket.getaddrinfo','example.com',443,0,0,0)
    blocked('socket.sendto',sock,('8.8.8.8',12345))
blocked('socket.connect',sock,('127.0.0.1',12345))
sock.close()
'''
        result=subprocess.run([sys.executable,'-c',code],capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_translation_never_falls_back_to_system_temp_without_project(self):
        with patch('voxlate.hy_translator.tempfile.TemporaryDirectory') as temporary:
            with self.assertRaisesRegex(VoxlateError, '项目目录'):
                HyTranslator({})
            temporary.assert_not_called()

    def test_worker_uses_job_directory_as_translation_project(self):
        from voxlate import worker
        with TestDirectory() as folder:
            root = Path(folder)
            request = root / 'translator_job.json'
            result = root / 'translator_result.json'
            write_json(request, dict(kind='translator', config={}, texts=['Hello'], result=str(result)))
            import os
            import tempfile
            previous_cwd, previous_temp = Path.cwd(), tempfile.tempdir
            previous_env = dict(os.environ)
            def check_working_directory(*args, **kwargs):
                self.assertEqual(Path.cwd(), root.resolve())
                self.assertTrue(Path(tempfile.gettempdir()).is_relative_to(root.resolve()))
                return ['你好']
            with patch.object(worker, 'enable_offline'), patch.object(sys, 'argv', ['worker', str(request)]), \
                 patch('voxlate.translator.Translator') as translator:
                translator.return_value.translate_many.side_effect = check_working_directory
                worker.main()
            self.assertEqual(Path.cwd(), previous_cwd)
            self.assertEqual(tempfile.tempdir, previous_temp)
            self.assertEqual(dict(os.environ), previous_env)
            translator.assert_called_once_with({}, work_dir=root.resolve())
            self.assertEqual(read_json(result), ['你好'])

    def test_translation_parser_preserves_sentence_order_and_rejects_missing_lines(self):
        self.assertEqual(parse_translations('banner\n{"2":"我们走吧。","1":"能帮帮我们吗？"}\nend', 2),
                         ["能帮帮我们吗？", "我们走吧。"])
        for value in ('{"1":"你好"}', '{"1":"你好","2":""}', '{"1":"你好","3":"再见"}', '{"1":"你好","2":'):
            with self.assertRaises(VoxlateError):
                parse_translations(value, 2)

    def test_retired_translation_config_migrates_and_installs_only_7b(self):
        with TestDirectory() as folder:
            cfg = load_config(Path(__file__).resolve().parents[1] / 'config.json')
            cfg = relocate(cfg, folder)
            cfg['translator'].update(model_type='hy_mt2_1b',model_path=str(Path(folder)/'models/hy-mt2-1.8b-q4'),device='cpu')
            write_json(Path(folder)/'config.json',cfg)
            cfg=load_config(Path(folder)/'config.json')
            self.assertEqual(cfg['translator']['device'],'cpu')
            self.assertEqual(Path(cfg['translator']['model_path']),Path(folder)/MODELS['hy7_model']['relative'])
            cfg = relocate(cfg, folder)
            self.assertEqual(selected_key(cfg), 'hy7_model')
            with patch('voxlate.diagnostics.run_external', side_effect=OSError('missing')):
                result = check_resources(cfg, Path(folder)/'config.json', probe=lambda *args: (True, 'ok'))
            plan = planned_stages(result)
            self.assertNotIn('hy1_model', plan)
            self.assertIn('llm_engine', plan)
            self.assertIn('hy7_model', plan)
            self.assertNotIn('translator_model', plan)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows process lifetime')
    def test_closing_native_job_stops_inference_child(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        try:
            close = protect_child(process)
            close()
            self.assertIsNotNone(process.wait(timeout=5))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
