# -*- coding: utf-8 -*-
import unittest

import matcher

from .base import pick_one, snapshot_catalog


class TestNormalize(unittest.TestCase):
    def test_strips_vendor_prefix(self):
        self.assertEqual(matcher.normalize("deepseek-ai/some-model"),
                         "some-model")

    def test_strips_tag_and_underscore(self):
        self.assertEqual(matcher.normalize("Kimi_K2:free"), "kimi-k2")

    def test_strips_known_suffixes(self):
        for raw, want in (("abc-preview", "abc"), ("abc-latest", "abc"),
                          ("abc-thinking", "abc"), ("abc-instruct", "abc"),
                          ("abc-free", "abc"), ("abc-cc", "abc")):
            self.assertEqual(matcher.normalize(raw), want)

    def test_cc_suffix_matches_official_model(self):
        """中转把 Claude Code 式转发模型叫 xxx-cc，要能认回官方型号。"""
        e = pick_one(lambda x: not x.mid.endswith("-cc") and len(x.mid) > 6
                     and ":" not in x.mid)
        r = matcher.match(snapshot_catalog(), e.mid + "-cc")
        self.assertTrue(r)
        self.assertEqual(r[0].entry.mid, e.mid)

    def test_strips_dates(self):
        self.assertEqual(matcher.normalize("model-20251001"), "model")
        self.assertEqual(matcher.normalize("model-0528"), "model")

    def test_empty(self):
        self.assertEqual(matcher.normalize(None), "")


class TestMatch(unittest.TestCase):
    """断言都从快照里现取，快照升级也不会无故失败。"""

    @classmethod
    def setUpClass(cls):
        cls.cat = snapshot_catalog()

    def test_exact_id(self):
        e = self.cat.entries[0]
        r = matcher.match(self.cat, e.mid)
        self.assertTrue(r)
        self.assertGreaterEqual(r[0].score, 1.0)
        self.assertEqual(r[0].entry.mid, e.mid)

    def test_vendor_prefix_form(self):
        e = pick_one(lambda x: "/" not in x.mid and len(x.mid) > 6)
        r = matcher.match(self.cat, "some-vendor/" + e.mid)
        self.assertTrue(r)
        self.assertEqual(r[0].entry.mid, e.mid)

    def test_free_tag_form(self):
        e = pick_one(lambda x: ":" not in x.mid and len(x.mid) > 6)
        r = matcher.match(self.cat, e.mid + ":free")
        self.assertTrue(r)
        self.assertEqual(r[0].entry.mid, e.mid)

    def test_thinking_suffix_form(self):
        e = pick_one(lambda x: x.model.get("reasoning")
                     and not x.mid.endswith("-thinking"))
        r = matcher.match(self.cat, e.mid + "-thinking")
        self.assertTrue(r)
        self.assertEqual(r[0].entry.mid, e.mid)

    def test_unknown_model_is_not_confident(self):
        r = matcher.match(self.cat, "totally-unknown-model-xyz-9987")
        self.assertFalse([x for x in r if x.score >= 0.9])

    def test_empty_input(self):
        self.assertEqual(matcher.match(self.cat, ""), [])

    def test_first_party_wins_on_tie(self):
        """同一个 ID 同时出现在厂商文件和网关文件里时，优先采信厂商官方数据。"""
        by_mid = {}
        for e in self.cat.entries:
            by_mid.setdefault(e.mid, []).append(e)
        target = None
        for mid, es in by_mid.items():
            vendors = {e.vendor for e in es}
            if vendors & matcher._FIRST_PARTY and vendors - matcher._FIRST_PARTY:
                target = mid
                break
        if not target:
            self.skipTest("快照里没有同 ID 跨厂商的副本")
        r = matcher.match(self.cat, target)
        self.assertIn(r[0].entry.vendor, matcher._FIRST_PARTY)

    def test_prefer_api_bonus(self):
        e = pick_one(lambda x: True)
        r = matcher.match(self.cat, e.mid, prefer_api=e.api)
        self.assertEqual(r[0].entry.api, e.api)

    def test_score_label(self):
        self.assertEqual(matcher.score_label(1.0), "精确")
        self.assertEqual(matcher.score_label(0.95), "很可能")
        self.assertEqual(matcher.score_label(0.8), "疑似")
        self.assertEqual(matcher.score_label(0.5), "低")

    def test_works_with_plain_object(self):
        """matcher 不能只认 Catalog，替身对象（没有 norm_index）也要能用。"""
        class Fake:
            entries = self.cat.entries[:20]
        r = matcher.match(Fake(), Fake.entries[3].mid)
        self.assertEqual(r[0].entry.mid, Fake.entries[3].mid)


if __name__ == "__main__":
    unittest.main()
