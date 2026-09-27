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
