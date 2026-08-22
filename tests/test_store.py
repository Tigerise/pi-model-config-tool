# -*- coding: utf-8 -*-
import json
import os
import shutil
import tempfile
import time
import unittest

import store

from .base import pick_one, snapshot_catalog


class TempConfigCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pimct_test_")
        self.path = os.path.join(self.dir, "models.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, data):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)


class TestBuildModelDict(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = snapshot_catalog()

    def test_uses_official_values(self):
        e = pick_one(lambda x: x.model.get("contextWindow")
                     and x.model.get("maxTokens") and x.model.get("cost"))
        row = store.entry_to_row("my-alias", e, 1.0)
        d = store.build_model_dict(row)
        self.assertEqual(d["id"], "my-alias")
        self.assertEqual(d["contextWindow"], e.model["contextWindow"])
        self.assertEqual(d["maxTokens"], e.model["maxTokens"])
        self.assertEqual(d["cost"], e.model["cost"])
        self.assertEqual(d["name"], e.model.get("name") or e.mid)

    def test_input_image_flag(self):
        e = pick_one(lambda x: "image" in (x.model.get("input") or []))
        row = store.entry_to_row("m", e, 1.0)
        self.assertEqual(store.build_model_dict(row)["input"],
                         ["text", "image"])
        row.input_image = False
        self.assertEqual(store.build_model_dict(row)["input"], ["text"])

    def test_compat_strips_gateway_routing(self):
        row = store.ModelRow("m")
        row.apply_manual()

        class FakeEntry:
            model = {"id": "m", "compat": {"openRouterRouting": {"only": ["x"]},
                                           "vercelGatewayRouting": {},
                                           "supportsStore": True}}
        row.matched = FakeEntry()
        d = store.build_model_dict(row)
        self.assertEqual(d["compat"], {"supportsStore": True})

    def test_thinking_levels(self):
        row = store.ModelRow("m")
        row.apply_manual()
        row.reasoning = True
        row.tlm["off"] = "no"
        row.tlm["high"] = "yes"
        d = store.build_model_dict(row)
        self.assertEqual(d["thinkingLevelMap"], {"off": None, "high": "high"})
        self.assertNotIn("low", d["thinkingLevelMap"])

    def test_keep_only_means_no_map(self):
        row = store.ModelRow("m")
        row.apply_manual()
        row.reasoning = True
        self.assertNotIn("thinkingLevelMap", store.build_model_dict(row))

    def test_explicit_tlm(self):
        row = store.ModelRow("m")
        row.reasoning = True
        row.tlm["low"] = "yes"
        row.tlm["off"] = "no"
        self.assertEqual(row.explicit_tlm(), {"low": "low", "off": None})

    def test_passthrough_unmanaged_fields(self):
        raw = {"id": "x", "name": "X", "contextWindow": 1000, "maxTokens": 100,
               "cost": {"input": 1, "output": 2, "cacheRead": 0, "cacheWrite": 0},
               "compat": {"forceAdaptiveThinking": True},
               "headers": {"x-a": "b"}, "samplingParams": {"temperature": 0.5}}
        row = store.row_from_config(raw)
        d = store.build_model_dict(row)
        for k in ("cost", "compat", "headers", "samplingParams"):
            self.assertEqual(d[k], raw[k], k)

    def test_rejects_zero_context(self):
        row = store.ModelRow("m")
        row.apply_manual()
        row.context_window = 0
        with self.assertRaises(ValueError):
            store.build_model_dict(row)

    def test_rejects_empty_id(self):
        with self.assertRaises(ValueError):
            store.build_model_dict(store.ModelRow(""))


class TestProviderAndHeaders(unittest.TestCase):
    def test_placeholder_headers_are_dropped(self):
        """以前界面预填的 {"User-Agent": "..."} 会被当真写进配置。"""
        self.assertEqual(store.parse_headers('{"User-Agent": "..."}'), {})
        self.assertEqual(store.parse_headers('{"User-Agent": " "}'), {})
        self.assertEqual(store.parse_headers('{"x-app": "cli"}'), {"x-app": "cli"})

    def test_parse_headers_rejects_non_object(self):
        with self.assertRaises(ValueError):
            store.parse_headers('["a"]')

    def test_headers_round_trip(self):
        h = {"User-Agent": "claude-cli/2.1.0", "x-app": "cli"}
        self.assertEqual(store.parse_headers(store.headers_to_text(h)), h)
        self.assertEqual(store.headers_to_text({}), "")

    def test_empty_key_is_omitted_not_written_empty(self):
        p = store.build_provider("https://x.com/v1", "openai-completions", "",
                                 [{"id": "m"}])
        self.assertNotIn("apiKey", p)

    def test_provider_keeps_unmanaged_existing_fields(self):
        existing = {"baseUrl": "old", "api": "old", "apiKey": "old",
                    "name": "显示名", "compat": {"supportsStore": True},
                    "modelOverrides": {"m": {"name": "n"}}, "oauth": "radius",
                    "models": [{"id": "old"}]}
        p = store.build_provider("https://new/v1", "openai-completions", "sk-new",
                                 [{"id": "m"}], existing=existing)
        self.assertEqual(p["baseUrl"], "https://new/v1")
        self.assertEqual(p["apiKey"], "sk-new")
        self.assertEqual(p["name"], "显示名")
        self.assertEqual(p["compat"], {"supportsStore": True})
        self.assertEqual(p["modelOverrides"], {"m": {"name": "n"}})
        self.assertEqual(p["oauth"], "radius")
        self.assertEqual([m["id"] for m in p["models"]], ["m"])

    def test_auth_header_and_headers(self):
        p = store.build_provider("https://x.com/v1", "anthropic-messages", "sk",
                                 [], headers_text='{"x-app": "cli"}',
                                 auth_header=True)
        self.assertTrue(p["authHeader"])
        self.assertEqual(p["headers"], {"x-app": "cli"})

    def test_provider_name_validation(self):
        store.validate_provider_name("my-svc.2_x")
        for bad in ("", "有中文", "a b", "x" * 65):
            with self.assertRaises(ValueError):
                store.validate_provider_name(bad)


class TestWriteAndBackup(TempConfigCase):
    def prov(self, key="sk-1", mid="m1"):
        return store.build_provider("https://x.com/v1", "openai-completions",
                                    key, [{"id": mid, "contextWindow": 1000,
                                           "maxTokens": 100}])

    def test_merge_write_keeps_others(self):
        store.merge_write(self.path, "a", self.prov())
        store.merge_write(self.path, "b", self.prov(mid="m2"))
        data = self.read()
        self.assertEqual(sorted(data["providers"]), ["a", "b"])

    def test_backup_created_on_second_write(self):
        self.assertIsNone(store.merge_write(self.path, "a", self.prov()))
        bak = store.merge_write(self.path, "a", self.prov())
        self.assertTrue(bak and os.path.exists(bak))

    def test_backup_pruning(self):
        store.merge_write(self.path, "a", self.prov())
        for i in range(6):
            time.sleep(0.01)
            store.write_config(self.path, store.load_config(self.path),
                               keep_backups=3)
        self.assertLessEqual(len(store.list_backups(self.path)), 3)

    def test_write_refuses_invalid_config(self):
        """空密钥会让 pi 判定整份配置非法，所以工具必须在写盘前拦住。"""
        data = {"providers": {"a": {"baseUrl": "https://x", "apiKey": "",
                                    "models": []}}}
        with self.assertRaises(ValueError):
            store.write_config(self.path, data)
        self.assertFalse(os.path.exists(self.path))

    def test_write_allows_skipping_validation(self):
        data = {"providers": {"a": {"baseUrl": "https://x", "apiKey": "",
                                    "models": []}}}
        store.write_config(self.path, data, validate=False)
        self.assertTrue(os.path.exists(self.path))

    def test_preexisting_problem_does_not_block_other_edits(self):
        """配置里本来就有毛病时，删东西、改东西不能因此被堆死。"""
        broken = {"providers": {
            "bad": {"baseUrl": "https://x", "apiKey": "", "models": []},
            "good": {"baseUrl": "https://y", "apiKey": "sk", "models": []}}}
        store.write_config(self.path, broken, validate=False)
        after = {"providers": {
            "bad": {"baseUrl": "https://x", "apiKey": "", "models": []}}}
        warnings = store.check_before_write(after, baseline=broken)
        self.assertTrue(any("本来就有" in w for w in warnings))
        store.write_config(self.path, after)          # 不报错
        self.assertEqual(list(self.read()["providers"]), ["bad"])

    def test_new_problem_still_blocks_even_if_config_was_broken(self):
        broken = {"providers": {
            "bad": {"baseUrl": "https://x", "apiKey": "", "models": []}}}
        worse = {"providers": {
            "bad": {"baseUrl": "https://x", "apiKey": "", "models": []},
            "bad2": {"baseUrl": "https://z", "apiKey": "", "models": []}}}
        with self.assertRaises(ValueError):
            store.check_before_write(worse, baseline=broken)

    def test_restore_backup(self):
        store.merge_write(self.path, "a", self.prov())
        first = self.read()
        bak = store.merge_write(self.path, "b", self.prov(mid="m2"))
        self.assertIn("b", self.read()["providers"])
        store.restore_backup(self.path, bak)
        self.assertEqual(self.read(), first)

    def test_restore_rejects_junk(self):
        junk = os.path.join(self.dir, "junk.json")
        with open(junk, "w", encoding="utf-8") as f:
            json.dump({"nope": 1}, f)
        with self.assertRaises(ValueError):
            store.restore_backup(self.path, junk)

    def test_load_missing_file(self):
        self.assertEqual(store.load_config(self.path), {"providers": {}})

    def test_load_rejects_non_object(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("[]")
        with self.assertRaises(ValueError):
            store.load_config(self.path)


class TestApplyFixes(unittest.TestCase):
    def test_turn_off_image(self):
        m = {"id": "m", "input": ["text", "image"]}
        done = store.apply_fixes(m, [{"kind": "input_image", "value": False}])
        self.assertEqual(m["input"], ["text"])
        self.assertTrue(done)

    def test_turn_on_image(self):
        m = {"id": "m", "input": ["text"]}
        store.apply_fixes(m, [{"kind": "input_image", "value": True}])
        self.assertEqual(m["input"], ["text", "image"])

    def test_no_change_reports_nothing(self):
        m = {"id": "m", "input": ["text"]}
        self.assertEqual(store.apply_fixes(m, [{"kind": "input_image",
                                                "value": False}]), [])

    def test_reasoning_off_drops_level_map(self):
        m = {"id": "m", "reasoning": True, "thinkingLevelMap": {"low": "low"}}
        store.apply_fixes(m, [{"kind": "reasoning", "value": False}])
        self.assertFalse(m["reasoning"])
        self.assertNotIn("thinkingLevelMap", m)

    def test_max_tokens_field(self):
        m = {"id": "m", "compat": {"supportsStore": True}}
        store.apply_fixes(m, [{"kind": "maxTokensField",
                               "value": "max_completion_tokens"}])
        self.assertEqual(m["compat"]["maxTokensField"], "max_completion_tokens")
        self.assertTrue(m["compat"]["supportsStore"])

    def test_context_window(self):
        m = {"id": "m", "contextWindow": 100}
        store.apply_fixes(m, [{"kind": "contextWindow", "value": 200}])
        self.assertEqual(m["contextWindow"], 200)


class TestMixedProtocol(unittest.TestCase):
    """pi 取地址时模型级 baseUrl 优先于服务商级，所以混协议不用拆服务商。"""

    def test_base_for_api(self):
        self.assertEqual(store.base_for_api("openai-completions", "https://x.com"),
                         "https://x.com/v1")
        self.assertEqual(store.base_for_api("openai-completions", "https://x.com/v1/"),
                         "https://x.com/v1")
        self.assertEqual(store.base_for_api("anthropic-messages", "https://x.com/v1"),
                         "https://x.com")
        self.assertEqual(store.base_for_api("google-generative-ai", "https://x.com"),
                         "https://x.com/v1beta")
        self.assertEqual(store.base_for_api("openai-completions", ""), "")

    def row(self, api_override=None):
        r = store.ModelRow("m")
        r.apply_manual()
        r.api_override = api_override
        return r

    def test_model_gets_own_base_url_when_protocol_differs(self):
        d = store.build_model_dict(self.row("anthropic-messages"),
                                   provider_api="openai-completions",
                                   provider_base="https://x.com/v1")
        self.assertEqual(d["api"], "anthropic-messages")
        self.assertEqual(d["baseUrl"], "https://x.com")

    def test_no_base_url_when_protocol_matches(self):
        d = store.build_model_dict(self.row("openai-completions"),
                                   provider_api="openai-completions",
                                   provider_base="https://x.com/v1")
        self.assertNotIn("baseUrl", d)

    def test_stale_base_url_is_replaced_not_inherited(self):
        raw = {"id": "m", "api": "anthropic-messages", "baseUrl": "https://old.com",
               "contextWindow": 1000, "maxTokens": 100}
        row = store.row_from_config(raw)
        d = store.build_model_dict(row, provider_api="openai-completions",
                                   provider_base="https://new.com/v1")
        self.assertEqual(d["baseUrl"], "https://new.com")


class TestRelayCompatPreset(unittest.TestCase):
    def test_relay_compat_openai(self):
        got = store.relay_compat("openai-completions",
                                 {"no_developer_role": True,
                                  "max_tokens_field": True,
                                  "tolerate_no_finish_reason": False})
        self.assertEqual(got, {"supportsDeveloperRole": False,
                               "maxTokensField": "max_tokens"})

    def test_relay_compat_anthropic(self):
        got = store.relay_compat("anthropic-messages",
                                 {"no_eager_tool_streaming": True,
                                  "no_developer_role": True})
        self.assertEqual(got, {"supportsEagerToolInputStreaming": False})

    def test_state_round_trip(self):
        compat = store.relay_compat("openai-completions",
                                    {"no_developer_role": True})
        state = store.relay_compat_state("openai-completions", compat)
        self.assertTrue(state["no_developer_role"])
        self.assertFalse(state["max_tokens_field"])

    def test_state_of_empty_compat_is_all_false(self):
        state = store.relay_compat_state("openai-completions", None)
        self.assertFalse(any(state.values()))

    def test_provider_preset_add_and_remove(self):
        p = store.build_provider("https://x.com/v1", "openai-completions", "sk",
                                 [], relay_options={"no_developer_role": True})
        self.assertEqual(p["compat"], {"supportsDeveloperRole": False})
        p2 = store.build_provider("https://x.com/v1", "openai-completions", "sk",
                                  [], existing=p,
                                  relay_options={"no_developer_role": False})
        self.assertNotIn("compat", p2)

    def test_preset_does_not_touch_other_compat_keys(self):
        existing = {"compat": {"supportsStore": True,
                               "supportsDeveloperRole": False}}
        p = store.build_provider("https://x.com/v1", "openai-completions", "sk",
                                 [], existing=existing,
                                 relay_options={"no_developer_role": False})
        self.assertEqual(p["compat"], {"supportsStore": True})


class TestApplyApiFix(unittest.TestCase):
    def test_switch_to_provider_protocol_drops_model_base_url(self):
        m = {"id": "m", "api": "anthropic-messages", "baseUrl": "https://x.com"}
        done = store.apply_fixes(m, [{"kind": "api", "value": "openai-completions"}],
                                 provider_api="openai-completions",
                                 provider_base="https://x.com/v1")
        self.assertTrue(done)
        self.assertEqual(m["api"], "openai-completions")
        self.assertNotIn("baseUrl", m)

    def test_switch_to_other_protocol_sets_base_url(self):
        m = {"id": "m"}
        store.apply_fixes(m, [{"kind": "api", "value": "anthropic-messages"}],
                          provider_api="openai-completions",
                          provider_base="https://x.com/v1")
        self.assertEqual(m["api"], "anthropic-messages")
        self.assertEqual(m["baseUrl"], "https://x.com")

    def test_no_change_when_already_right(self):
        m = {"id": "m", "api": "openai-completions"}
        self.assertEqual(
            store.apply_fixes(m, [{"kind": "api", "value": "openai-completions"}]),
            [])


if __name__ == "__main__":
    unittest.main()
