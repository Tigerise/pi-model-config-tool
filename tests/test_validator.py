# -*- coding: utf-8 -*-
import os
import shutil
import unittest

import store
import validator

from .base import snapshot_catalog


class TestCheckConfig(unittest.TestCase):
    def ok_provider(self, **kw):
        p = {"baseUrl": "https://x.com/v1", "api": "openai-completions",
             "apiKey": "sk-1", "models": [{"id": "m", "contextWindow": 1000,
                                           "maxTokens": 100}]}
        p.update(kw)
        return {"providers": {"p": p}}

    def errors(self, data):
        return validator.check_config(data)[0]

    def warnings(self, data):
        return validator.check_config(data)[1]

    def test_good_config_passes(self):
        self.assertEqual(self.errors(self.ok_provider()), [])

    def test_empty_api_key_is_fatal(self):
        errs = self.errors(self.ok_provider(apiKey=""))
        self.assertTrue(errs)
        self.assertIn("密钥", errs[0])

    def test_missing_api_key_is_only_a_warning(self):
        data = self.ok_provider()
        data["providers"]["p"].pop("apiKey")
        self.assertEqual(self.errors(data), [])
        self.assertTrue(self.warnings(data))

    def test_bad_input_kind(self):
        data = self.ok_provider()
        data["providers"]["p"]["models"][0]["input"] = ["text", "video"]
        self.assertTrue(self.errors(data))

    def test_incomplete_cost(self):
        data = self.ok_provider()
        data["providers"]["p"]["models"][0]["cost"] = {"input": 1, "output": 2}
        errs = self.errors(data)
        self.assertTrue(errs)
        self.assertIn("cost", errs[0])

    def test_complete_cost_ok(self):
        data = self.ok_provider()
        data["providers"]["p"]["models"][0]["cost"] = {
            "input": 1, "output": 2, "cacheRead": 0, "cacheWrite": 0}
        self.assertEqual(self.errors(data), [])

    def test_bad_thinking_level_value(self):
        data = self.ok_provider()
        data["providers"]["p"]["models"][0]["thinkingLevelMap"] = {"low": 5}
        self.assertTrue(self.errors(data))

    def test_null_thinking_level_ok(self):
        data = self.ok_provider()
        data["providers"]["p"]["models"][0]["thinkingLevelMap"] = {"off": None}
        self.assertEqual(self.errors(data), [])

    def test_non_string_headers(self):
        self.assertTrue(self.errors(self.ok_provider(headers={"a": 1})))

    def test_placeholder_header_warns(self):
        w = self.warnings(self.ok_provider(headers={"User-Agent": "..."}))
        self.assertTrue(any("示例" in x for x in w))

    def test_model_without_id(self):
        data = self.ok_provider()
        data["providers"]["p"]["models"][0].pop("id")
        self.assertTrue(self.errors(data))

    def test_duplicate_ids_warn(self):
        data = self.ok_provider()
        data["providers"]["p"]["models"].append({"id": "m"})
        self.assertTrue(any("重复" in x for x in self.warnings(data)))

    def test_bad_oauth(self):
        self.assertTrue(self.errors(self.ok_provider(oauth="nope")))

    def test_models_not_a_list(self):
        self.assertTrue(self.errors(self.ok_provider(models={})))

    def test_missing_providers_key(self):
        self.assertTrue(self.errors({}))

    def test_zero_context_window_warns(self):
        data = self.ok_provider()
        data["providers"]["p"]["models"][0]["contextWindow"] = 0
        self.assertEqual(self.errors(data), [])
        self.assertTrue(any("默认值" in x for x in self.warnings(data)))

    def test_model_overrides_checked(self):
        data = self.ok_provider(modelOverrides={"m": {"input": ["audio"]}})
        self.assertTrue(self.errors(data))

    def test_whole_catalog_generates_valid_config(self):
        """把参数库里每个型号都生成一遍，确认工具的输出本身是干净的。"""
        cat = snapshot_catalog()
        models, seen = [], set()
        for e in cat.entries:
            row = store.entry_to_row(e.mid, e, 1.0)
            try:
                d = store.build_model_dict(row)
            except ValueError:
                continue          # 官方没给 ctx / maxTokens 的条目跳过
            if d["id"] in seen:
                continue
            seen.add(d["id"])
            models.append(d)
        self.assertGreater(len(models), 300)
        data = {"providers": {"stress": store.build_provider(
            "https://x.com/v1", "openai-completions", "sk-1", models)}}
        errs, _w = validator.check_config(data)
        self.assertEqual(errs, [])


@unittest.skipUnless(validator.pi_available(),
                     "本机没装 pi 或没有 node，跳过 pi 复核相关测试")
class TestPiValidator(unittest.TestCase):
    """有 pi 的机器上，用 pi 自己的校验器验证我们的判断和它一致。"""

    def test_pi_accepts_good_config(self):
        data = {"providers": {"p": {"baseUrl": "https://x.com/v1",
                                    "api": "openai-completions", "apiKey": "sk",
                                    "models": [{"id": "m"}]}}}
        ok, msg = validator.pi_validate_data(data)
        self.assertTrue(ok, msg)

    def test_pi_rejects_empty_key(self):
        data = {"providers": {"p": {"baseUrl": "https://x.com/v1",
                                    "apiKey": "", "models": [{"id": "m"}]}}}
        ok, _msg = validator.pi_validate_data(data)
        self.assertIs(ok, False)

    def test_pi_rejects_incomplete_cost(self):
        data = {"providers": {"p": {"baseUrl": "https://x.com/v1", "apiKey": "sk",
                                    "models": [{"id": "m",
                                                "cost": {"input": 1,
                                                         "output": 2}}]}}}
        ok, _msg = validator.pi_validate_data(data)
        self.assertIs(ok, False)

    def test_pi_accepts_whole_catalog_output(self):
        cat = snapshot_catalog()
        models, seen = [], set()
        for e in cat.entries:
            row = store.entry_to_row(e.mid, e, 1.0)
            try:
                d = store.build_model_dict(row)
            except ValueError:
                continue
            if d["id"] in seen:
                continue
            seen.add(d["id"])
            models.append(d)
        data = {"providers": {"stress": store.build_provider(
            "https://x.com/v1", "openai-completions", "sk-1", models)}}
        ok, msg = validator.pi_validate_data(data)
        self.assertTrue(ok, msg)


@unittest.skipUnless(shutil.which("pi"), "本机没装 pi，跳过沙盒预演测试")
class TestPiDryRun(unittest.TestCase):
    """schema 挑不出的毛病，得让真正的 pi 读一遍才知道。"""

    def test_clean_config_has_no_warning(self):
        data = {"providers": {"good": {
            "baseUrl": "https://a.com/v1", "api": "openai-completions",
            "apiKey": "sk-1",
            "models": [{"id": "m", "contextWindow": 1000, "maxTokens": 100}]}}}
        ok, txt = validator.pi_dry_run(data)
        self.assertTrue(ok, txt)
        self.assertIn("good", txt)

    def test_missing_api_is_caught_only_here(self):
        data = {"providers": {"no_api": {"apiKey": "sk-1",
                                         "models": [{"id": "m"}]}}}
        self.assertEqual(validator.check_config(data)[0], [])   # 自检看不出来
        self.assertIs(validator.pi_validate_data(data)[0], True)  # schema 也放行
        ok, txt = validator.pi_dry_run(data)                    # pi 真读才报
        self.assertIs(ok, False)
        self.assertIn("api", txt.lower())

    def test_sandbox_does_not_touch_real_config(self):
        import store as store_mod
        before = None
        if os.path.exists(store_mod.DEFAULT_CONFIG_PATH):
            with open(store_mod.DEFAULT_CONFIG_PATH, encoding="utf-8") as f:
                before = f.read()
        validator.pi_dry_run({"providers": {"x": {"baseUrl": "https://a/v1",
                                                 "api": "openai-completions",
                                                 "apiKey": "sk",
                                                 "models": [{"id": "m"}]}}})
        if before is not None:
            with open(store_mod.DEFAULT_CONFIG_PATH, encoding="utf-8") as f:
                self.assertEqual(f.read(), before)


if __name__ == "__main__":
    unittest.main()
