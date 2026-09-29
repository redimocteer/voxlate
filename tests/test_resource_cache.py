import copy
from pathlib import Path
import unittest
from unittest.mock import patch

from test_pipeline import TestDirectory
from voxlate.app_settings import prepare_settings
from voxlate.common import load_config
from voxlate.diagnostics import check_resources
from voxlate.resource_cache import load_resource_cache, save_resource_cache
from voxlate.resources import CATALOG, EXTRAS
from voxlate.translation_models import MODELS


class ResourceCacheTests(unittest.TestCase):
    def test_selected_step_does_not_probe_unrelated_resources_even_without_cache(self):
        with TestDirectory() as directory:
            path, _ = prepare_settings(directory)
            cfg = load_config(path)
            with (patch('voxlate.diagnostics.probe_runtime', side_effect=AssertionError('runtime probe')),
                  patch('voxlate.diagnostics.run_external', side_effect=AssertionError('subprocess')),
                  patch('voxlate.diagnostics.missing_files', side_effect=AssertionError('ASR model scan')),
                  patch('voxlate.resources.os.walk', side_effect=AssertionError('directory scan')),
                  patch('voxlate.diagnostics.shutil.which', side_effect=AssertionError('media tool check'))):
                results = check_resources(cfg, path, keys={'hy7_model'}, quick=True)
            self.assertEqual({r.key for r in results}, set(CATALOG) | {'ffprobe'})
            self.assertTrue(all('尚未检查' in r.detail for r in results if r.key != 'hy7_model'))

    def test_cached_status_is_preserved_but_requirements_follow_selection(self):
        from voxlate.recognition_models import select_model
        from voxlate.installer import resource_root
        with TestDirectory() as directory:
            path, _ = prepare_settings(directory)
            cfg = load_config(path)
            cached = check_resources(cfg, path, quick=True)
            for item in cached:
                item.ready = True
            select_model(cfg, 'asr_qwen_model', resource_root(cfg))
            with patch('voxlate.diagnostics.missing_files', side_effect=AssertionError('scan')):
                results = check_resources(cfg, path, keys=set(), cached=cached, quick=True)
            self.assertTrue(all(r.ready for r in results))
            self.assertEqual({r.key for r in results if r.required and r.key.startswith('asr_')}, {'asr_qwen_model'})
            self.assertTrue(next(r.required for r in results if r.key == 'qwen'))

    def test_quick_check_never_starts_runtimes_or_scans_directories(self):
        with TestDirectory() as directory:
            path, _ = prepare_settings(directory)
            cfg = load_config(path)
            with (patch('voxlate.diagnostics.run_external', side_effect=AssertionError('subprocess')),
                  patch('voxlate.resources.os.walk', side_effect=AssertionError('scan'))):
                results = check_resources(cfg, path, quick=True, probe=lambda *a: self.fail('runtime import'))
            self.assertEqual({r.key for r in results}, set(CATALOG) | {'ffprobe'})
            self.assertEqual(set(MODELS), {'hy7_model'})
            self.assertFalse({'translator_model', 'legacy_asr', 'legacy_translation'} & (set(CATALOG) | set(EXTRAS)))

    def test_persisted_results_match_config_and_ignore_mirror_choice(self):
        with TestDirectory() as directory:
            path, _ = prepare_settings(directory)
            cfg = load_config(path)
            results = check_resources(cfg, path, quick=True)
            results.sizes['tts_model'] = 12345
            save_resource_cache(cfg, path, results)
            restored = load_resource_cache(cfg, path)
            self.assertEqual(list(restored), list(results))
            self.assertEqual(restored.sizes['tts_model'], 12345)
            modified = copy.deepcopy(cfg)
            modified['try_mirrors'] = False
            self.assertIsNotNone(load_resource_cache(modified, path))
            modified['tts']['device'] = 'changed-device'
            self.assertIsNone(load_resource_cache(modified, path))
            path.with_name('resource-status.json').write_text('broken')
            self.assertIsNone(load_resource_cache(cfg, path))
