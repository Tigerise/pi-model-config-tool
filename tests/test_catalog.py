# -*- coding: utf-8 -*-
import os
import unittest

import catalog as catalog_mod

from .base import snapshot_catalog


class TestCatalog(unittest.TestCase):
    def test_snapshot_loads(self):
        c = snapshot_catalog()
        self.assertTrue(c.is_snapshot)
        self.assertGreater(len(c), 500)
        self.assertTrue(os.path.isdir(c.source_dir))
        self.assertIn("快照", c.source_label())

    def test_entries_have_id_and_api(self):
        c = snapshot_catalog()
        for e in c.entries[:50]:
            self.assertTrue(e.mid)
            self.assertTrue(e.api)
            self.assertIn("（", e.display())

    def test_norm_index_is_cached_and_complete(self):
        c = snapshot_catalog()
        idx1 = c.norm_index()
        idx2 = c.norm_index()
        self.assertIs(idx1, idx2)
        total = sum(len(v) for v in idx1.values())
        self.assertEqual(total, len(c))

    def test_env_override_wins(self):
        c = snapshot_catalog()
        old = os.environ.get(catalog_mod.ENV_OVERRIDE)
        os.environ[catalog_mod.ENV_OVERRIDE] = c.source_dir
        try:
            c2 = catalog_mod.Catalog()
            self.assertEqual(os.path.normpath(c2.source_dir),
                             os.path.normpath(c.source_dir))
        finally:
            if old is None:
                os.environ.pop(catalog_mod.ENV_OVERRIDE, None)
            else:
                os.environ[catalog_mod.ENV_OVERRIDE] = old


class TestDescribeThinking(unittest.TestCase):
    """曾经的 bug：官方没写 thinkingLevelMap 的模型，表格显示成“全部不可用”，
    而实际写入时压根不写这个字段、pi 会给全档。这里锁住正确行为。"""

    def test_no_map_means_default(self):
        txt = catalog_mod.describe_thinking({}, True)
        self.assertIn("默认", txt)
        self.assertNotIn("不可用", txt)

    def test_not_reasoning(self):
        self.assertEqual(catalog_mod.describe_thinking({}, False), "不支持思考")

    def test_partial_map_shows_default_for_unspecified(self):
        txt = catalog_mod.describe_thinking({"off": None, "max": "max"}, True)
        self.assertIn("可用：最大", txt)
        self.assertIn("不可用：关", txt)
        self.assertIn("默认：", txt)
        self.assertIn("低", txt)

    def test_full_map(self):
        m = {"off": None, "minimal": None, "low": "low", "medium": "medium",
             "high": "high", "xhigh": "xhigh", "max": "max"}
        txt = catalog_mod.describe_thinking(m, True)
        self.assertIn("可用：低、中、高、超高、最大", txt)
        self.assertIn("不可用：关、极低", txt)
        self.assertNotIn("默认：", txt)


if __name__ == "__main__":
    unittest.main()
