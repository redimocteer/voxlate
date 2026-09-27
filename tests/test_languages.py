from pathlib import Path
from types import SimpleNamespace
import hashlib
import sys
import unittest
from unittest.mock import Mock, patch

from test_pipeline import TestDirectory
from voxlate.common import load_config, VoxlateError
from voxlate.translator import Translator
from voxlate.asr import transcribe
from voxlate.languages import DIRECTIONS, direction, default_output, speech_settings, translation_settings, LANGUAGES
from voxlate.common import digest, read_json, write_json
from voxlate.pipeline import VideoDubPipeline
from voxlate.project_storage import default_project_directory, existing_project_directory
from voxlate.dubbing_state import voice_key


class LanguageTests(unittest.TestCase):
    def test_all_directions_separate_projects_exports_and_cache_keys(self):
        base = load_config(Path(__file__).resolve().parents[1] / 'config.json')
        projects, outputs, translations, voices = set(), set(), set(), set()
        for source, target in DIRECTIONS:
            cfg = dict(base, source_lang=source, target_lang=target, audio_track=1, audio_track_count=2)
            pipeline = VideoDubPipeline(cfg)
            self.assertEqual(pipeline.cfg['asr']['language'], source)
            self.assertEqual(pipeline.cfg['translator'].get('target_lang', 'zh'), target)
            self.assertEqual(pipeline.cfg['tts'].get('target_lang', 'zh'), target)
            self.assertEqual(voice_key({}, cfg), voice_key({}, pipeline.cfg))
            projects.add(default_project_directory('movie.mkv', cfg))
            outputs.add(default_output('movie.mkv', cfg))
            translations.add(digest(translation_settings(cfg)))
            voices.add(digest(speech_settings(cfg)))
        self.assertEqual(len(projects), 6)
        self.assertEqual(len(outputs), 6)
        self.assertEqual(len(translations), 6)
        self.assertEqual(len(voices), 3)

    def test_old_chinese_projects_and_settings_retain_cache_identity(self):
        cfg = load_config(Path(__file__).resolve().parents[1] / 'config.json')
        cfg.pop('target_lang')
        self.assertEqual(translation_settings(cfg), dict(cfg['translator'], source_lang='en'))
        self.assertEqual(speech_settings(cfg), cfg['tts'])
        with TestDirectory() as folder:
            video = Path(folder)/'movie.mkv'
            legacy = default_project_directory(video, cfg)
            write_json(legacy/'project.json', {'segments': []})
            self.assertEqual(existing_project_directory(video, dict(cfg, audio_track_count=2)), legacy)
            self.assertNotEqual(existing_project_directory(video, dict(cfg, target_lang='ja')), legacy)

    def test_invalid_directions_fail_before_loading_any_model(self):
        cfg = load_config(Path(__file__).resolve().parents[1] / 'config.json')
        for source, target in (('zh','zh'), ('en','en'), ('ja','ja'), ('fr','zh'), ('en',None), ([], 'zh')):
            with self.subTest(source=source, target=target), self.assertRaises(VoxlateError):
                VideoDubPipeline(dict(cfg, source_lang=source, target_lang=target))

    def test_hy_prompts_and_tts_language_codes_follow_all_directions(self):
        from voxlate.hy_translator import HyTranslator
        from voxlate.tts import TTSEngine
        for source, target in DIRECTIONS:
            with self.subTest(source=source, target=target):
                translator = HyTranslator.__new__(HyTranslator)
                translator.cfg = dict(source_lang=source, target_lang=target)
                _, _, prompt, _ = next(translator.batches(['SYNTHETIC'], ['SYNTHETIC'], [0]))
                self.assertIn('从'+LANGUAGES[source]['prompt']+'翻译为自然、准确的'+LANGUAGES[target]['prompt'], prompt)
                modules = {'torch': Mock(), 'indextts': Mock(), 'indextts.infer_v2_5': Mock()}
                with patch.dict(sys.modules, modules), TestDirectory() as folder:
                    cfg = dict(repo_path=folder, model_path=folder, device='cpu', use_bf16=False, target_lang=target)
                    engine = TTSEngine(cfg)
                    sys.path.remove(folder)
                    def infer(**kwargs):
                        from test_pipeline import tone
                        tone(kwargs['output_path'], .1)
                    engine.model.infer.side_effect = infer
                    engine.generate('SYNTHETIC', Path(folder)/'reference.wav', Path(folder)/'output.wav')
                    self.assertEqual(engine.model.infer.call_args.kwargs['lang'], target.upper())

    def test_export_audio_metadata_follows_direction(self):
        from voxlate.media import Media
        cfg = load_config(Path(__file__).resolve().parents[1] / 'config.json')
        for source, target in DIRECTIONS:
            media = Media(dict(cfg, source_lang=source, target_lang=target))
            with patch.object(media, 'render') as render:
                media.mux('video', 'dub.wav', 'output.mp4', 1, original_audio='original.wav')
            args = render.call_args.args[0]
            self.assertIn('language='+LANGUAGES[target]['iso3'], args)
            self.assertIn('language='+LANGUAGES[source]['iso3'], args)

    def test_english_and_japanese_translate_directly_to_chinese_in_one_pass(self):
        cfg = load_config(Path(__file__).resolve().parents[1] / "config.json")["translator"]
        cfg["model_type"] = "m2m100"
        for language in ("en", "ja"):
            engine = Mock()
            engine.translate_batch.return_value = [SimpleNamespace(hypotheses=[["__zh__", "你好", "</s>"]])]
            tokenizer = Mock()
            tokenizer.encode.return_value = ["原文"]
            tokenizer.decode.return_value = "你好"
            ct = Mock(Translator=Mock(return_value=engine))
            modules = {"ctranslate2": ct, "sentencepiece": Mock(SentencePieceProcessor=Mock(return_value=tokenizer)), "sacremoses": Mock()}
            with patch.dict(sys.modules, modules):
                translator = Translator(dict(cfg, source_lang=language))
                self.assertEqual(translator.translate_many(["source"]), ["你好"])
            ct.Translator.assert_called_once()
            engine.translate_batch.assert_called_once()
            self.assertEqual(engine.translate_batch.call_args.args[0], [[f"__{language}__", "原文", "</s>"]])
            self.assertEqual(engine.translate_batch.call_args.kwargs["target_prefix"], [["__zh__"]])
            tokenizer.decode.assert_called_once_with(["你好"])

    def test_recognition_passes_selected_language_and_rejects_english_only_model(self):
        cfg = load_config(Path(__file__).resolve().parents[1] / "config.json")["asr"]
        cfg['combined'] = False
        model = Mock(spec=['model', 'transcribe'], model=Mock(is_multilingual=True))
        model.transcribe.return_value = ([SimpleNamespace(start=0, end=1, text="こんにちは")], None)
        with patch.dict(sys.modules, {"faster_whisper": Mock(WhisperModel=Mock(return_value=model))}):
            segments = transcribe("test.wav", dict(cfg, language="ja"))
            self.assertEqual(model.transcribe.call_args.kwargs["language"], "ja")
            self.assertEqual(segments[0]["source_lang"], "ja")
            model.model.is_multilingual = False
            with self.assertRaisesRegex(VoxlateError, "多语言"):
                transcribe("test.wav", dict(cfg, language="ja"))
            model.model.is_multilingual = True
            transcribe('test.wav', dict(cfg, language='zh'))
            self.assertEqual(model.transcribe.call_args.kwargs['language'], 'zh')
            model.model.is_multilingual = False
            with self.assertRaisesRegex(VoxlateError, '多语言'):
                transcribe('test.wav', dict(cfg, language='zh'))

    def test_preconverted_download_is_pinned_and_verified_without_torch(self):
        from scripts.prepare_models import prepare_translator
        from voxlate.model_downloads import TRANSLATION_FILES, TRANSLATION_REPO, TRANSLATION_REVISION
        with TestDirectory() as folder:
            target = Path(folder)
            payload = b"model-test"
            for name in TRANSLATION_FILES:
                (target / name).write_bytes(payload)
            snapshot = Mock()
            hashes = {"model.bin": hashlib.sha256(payload).hexdigest()}
            with patch.dict(sys.modules, {"huggingface_hub": Mock(snapshot_download=snapshot), "torch": None}), patch("voxlate.model_downloads.TRANSLATION_HASHES", hashes):
                prepare_translator({"translator": {"model_path": folder}})
                snapshot.assert_called_once_with(TRANSLATION_REPO, revision=TRANSLATION_REVISION, local_dir=folder, allow_patterns=TRANSLATION_FILES)
                (target / "model.bin").write_bytes(b"corrupt")
                with self.assertRaisesRegex(RuntimeError, "校验失败"):
                    prepare_translator({"translator": {"model_path": folder}})
