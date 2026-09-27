import unittest
from unittest.mock import MagicMock

from voxlate.tts import release_idle_cuda_memory


class MemoryPolicyTests(unittest.TestCase):
    def test_cpu_never_calls_cuda(self):
        torch = MagicMock()
        release_idle_cuda_memory(torch, "cpu")
        self.assertFalse(torch.cuda.mock_calls)

    def test_releases_idle_cache_or_pressure_on_selected_device(self):
        mib = 1024**2
        for idle, free, expected in ((600, 3000, True), (100, 500, True), (100, 3000, False)):
            with self.subTest(idle=idle, free=free):
                torch = MagicMock()
                torch.cuda.memory_allocated.return_value = 5000 * mib
                torch.cuda.memory_reserved.return_value = (5000 + idle) * mib
                torch.cuda.mem_get_info.return_value = (free * mib, 10000 * mib)
                release_idle_cuda_memory(torch, "cuda:1")
                self.assertEqual(torch.cuda.empty_cache.called, expected)
                if expected:
                    torch.cuda.device.assert_called_once_with("cuda:1")
