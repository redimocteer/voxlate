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


class LanguageTests(unittest.TestCase):
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
            transcribe("test.wav", dict(cfg, language="en"))
            self.assertEqual(model.transcribe.call_args.kwargs["language"], "en")

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
