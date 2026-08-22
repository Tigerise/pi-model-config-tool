# -*- coding: utf-8 -*-
import base64
import json
import threading
import unittest

import fetcher
import tester


class TestPureHelpers(unittest.TestCase):
    def test_urls_openai(self):
        self.assertEqual(
            tester._urls_for("openai-completions", "https://x.com", "m", "k"),
            ["https://x.com/v1/chat/completions", "https://x.com/chat/completions"])

    def test_urls_openai_versioned(self):
        self.assertEqual(
            tester._urls_for("openai-completions", "https://x.com/v1", "m", "k"),
            ["https://x.com/v1/chat/completions"])

    def test_urls_anthropic_adds_v1(self):
        self.assertEqual(
            tester._urls_for("anthropic-messages", "https://x.com", "m", "k"),
            ["https://x.com/v1/messages"])

    def test_urls_google_has_model_and_key(self):
        url = tester._urls_for("google-generative-ai", "https://x.com", "gem", "k1")[0]
        self.assertIn("/v1beta/models/gem:generateContent", url)
        self.assertIn("key=k1", url)

    def test_base_payload_shapes(self):
        p, f = tester._base_payload("openai-completions", "m", "hi", 8)
        self.assertEqual((p["model"], p["max_tokens"], f), ("m", 8, "max_tokens"))
        p, _f = tester._base_payload("anthropic-messages", "m", "hi", 8)
        self.assertEqual(p["messages"][0]["content"], "hi")
        p, f = tester._base_payload("google-generative-ai", "m", "hi", 8)
        self.assertEqual(f, "maxOutputTokens")
        self.assertEqual(p["_model_id"], "m")

    def test_err_msg_paths(self):
        self.assertEqual(tester.err_msg('{"error":{"message":"boom"}}'), "boom")
        self.assertEqual(tester.err_msg('{"error":"nope"}'), "nope")
        self.assertEqual(tester.err_msg('{"msg":"m"}'), "m")
        self.assertEqual(tester.err_msg("plain text"), "plain text")
        self.assertEqual(tester.err_msg(""), "空响应")

    def test_make_png(self):
        raw = base64.b64decode(tester.make_png_b64(size=8))
        self.assertTrue(raw.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertIn(b"IHDR", raw[:20])
        self.assertIn(b"IEND", raw[-12:])

    def test_effective_strips_v1_for_claude(self):
        prov = {"api": "openai-completions", "baseUrl": "https://x.com/v1"}
        eff = tester.effective(prov, {"api": "anthropic-messages"})
        self.assertEqual(eff["baseUrl"], "https://x.com")
        self.assertEqual(eff["api"], "anthropic-messages")

    def test_effective_keeps_v1_otherwise(self):
        prov = {"api": "openai-completions", "baseUrl": "https://x.com/v1"}
        eff = tester.effective(prov, {})
        self.assertEqual(eff["baseUrl"], "https://x.com/v1")

    def test_overall_of(self):
        def r(key, verdict):
            return {"key": key, "verdict": verdict}
        self.assertEqual(tester.overall_of([r("basic", tester.FAIL)]), tester.FAIL)
        self.assertEqual(tester.overall_of([r("basic", tester.PASS),
                                            r("image", tester.FAIL)]), tester.WARN)
        self.assertEqual(tester.overall_of([r("basic", tester.PASS),
                                            r("image", tester.SKIP)]), tester.PASS)

    def test_collect_and_describe_fixes(self):
        results = [{"fixes": [{"kind": "input_image", "value": False,
                               "why": "被拒"}]},
                   {"fixes": [{"kind": "input_image", "value": True}]}]
        fixes = tester.collect_fixes(results)
        self.assertEqual(len(fixes), 1)      # 同一项以后出现的为准
        self.assertTrue(fixes[0]["value"])
        self.assertIn("图片输入", tester.fix_text(fixes[0]))


class FakeHTTP:
    """按顺序回应，并记下每次发出的地址和请求体。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, method="GET", body=None, timeout=25):
        self.calls.append({"url": url, "headers": headers,
                           "body": json.loads(body) if body else None})
        if self.responses:
            return self.responses.pop(0)
        return 200, json.dumps({"choices": [{"message": {"content": "好"}}]})


def ok_chat(text="好", prompt_tokens=None):
    j = {"choices": [{"message": {"content": text}}]}
    if prompt_tokens:
        j["usage"] = {"prompt_tokens": prompt_tokens}
    return 200, json.dumps(j)


class TestTestModel(unittest.TestCase):
    def setUp(self):
        self._orig = fetcher._http_request

    def tearDown(self):
        fetcher._http_request = self._orig

    def run_test(self, responses, model=None, opts=None, stop=None, state=None):
        fake = FakeHTTP(responses)
        fetcher._http_request = fake
        prov = {"api": "openai-completions", "baseUrl": "https://x.com/v1",
                "apiKey": "sk-secret-123456"}
        model = model or {"id": "m1", "input": ["text"]}
        res = tester.test_model(prov, model, opts or {"image": False,
                                                      "thinking": False,
                                                      "ctx_tokens": 0},
                               stop=stop, state=state)
        return res, fake

    def by_key(self, results, key):
        return next((r for r in results if r["key"] == key), None)

    def test_basic_pass(self):
        res, fake = self.run_test([ok_chat()])
        b = self.by_key(res, "basic")
        self.assertEqual(b["verdict"], tester.PASS)
        self.assertEqual(len(fake.calls), 1)

    def test_body_has_no_internal_field(self):
        """_model_id 只是工具内部用的，不能发给服务商。"""
        _res, fake = self.run_test([ok_chat()])
        self.assertNotIn("_model_id", fake.calls[0]["body"])

    def test_http_error_stops_further_tests(self):
        res, _f = self.run_test([(401, '{"error":{"message":"bad key"}}')],
                                opts={"image": True, "thinking": True,
                                      "ctx_tokens": 4000})
        self.assertEqual(self.by_key(res, "basic")["verdict"], tester.FAIL)
        self.assertIsNone(self.by_key(res, "image"))

    def test_key_is_not_echoed_in_detail(self):
        res, _f = self.run_test([(401, "invalid key sk-secret-123456")])
        self.assertNotIn("sk-secret-123456", self.by_key(res, "basic")["detail"])

    def test_max_completion_tokens_retry_applies_to_later_items(self):
        """曾经只有第一项会换字段重试，后面全报错。现在整轮记住。"""
        state = {}
        res, fake = self.run_test(
            [(400, '{"error":{"message":"use max_completion_tokens"}}'),
             ok_chat(),
             ok_chat("红色"),
             ok_chat("收到", prompt_tokens=3999)],
            model={"id": "m1", "input": ["text", "image"]},
            opts={"image": True, "thinking": False, "ctx_tokens": 4000},
            state=state)
        self.assertEqual(state.get("maxtok_field"), "max_completion_tokens")
        self.assertEqual(self.by_key(res, "basic")["verdict"], tester.PASS)
        self.assertEqual(self.by_key(res, "image")["verdict"], tester.PASS)
        self.assertEqual(self.by_key(res, "ctx")["verdict"], tester.PASS)
        for call in fake.calls[1:]:
            self.assertNotIn("max_tokens", call["body"])
            self.assertIn("max_completion_tokens", call["body"])
        fixes = tester.collect_fixes(res)
        self.assertEqual(fixes[0]["kind"], "maxTokensField")

    def test_image_rejected_although_configured(self):
        res, _f = self.run_test(
            [ok_chat(), (400, '{"error":{"message":"vision is not supported"}}')],
            model={"id": "m1", "input": ["text", "image"]},
            opts={"image": True, "thinking": False, "ctx_tokens": 0})
        img = self.by_key(res, "image")
        self.assertEqual(img["verdict"], tester.FAIL)
        self.assertEqual(img["fixes"][0], {"kind": "input_image", "value": False,
                                           "why": "实测被拒绝，配置里却开着图片"})

    def test_image_works_although_not_configured(self):
        res, _f = self.run_test([ok_chat(), ok_chat("红色")],
                                model={"id": "m1", "input": ["text"]},
                                opts={"image": True, "thinking": False,
                                      "ctx_tokens": 0})
        img = self.by_key(res, "image")
        self.assertEqual(img["verdict"], tester.PASS)
        self.assertEqual(img["fixes"][0]["value"], True)

    def test_text_only_model_refusing_image_is_consistent(self):
        res, _f = self.run_test(
            [ok_chat(), (400, '{"error":{"message":"model does not support image"}}')],
            model={"id": "m1", "input": ["text"]},
            opts={"image": True, "thinking": False, "ctx_tokens": 0})
        self.assertEqual(self.by_key(res, "image")["verdict"], tester.SKIP)

    def test_thinking_skipped_when_not_reasoning(self):
        res, _f = self.run_test([ok_chat()],
                                opts={"image": False, "thinking": True,
                                      "ctx_tokens": 0})
        self.assertEqual(self.by_key(res, "think")["verdict"], tester.SKIP)

    def test_thinking_rejected_suggests_turning_off(self):
        res, _f = self.run_test(
            [ok_chat(), (400, '{"error":{"message":"reasoning_effort not allowed"}}')],
            model={"id": "m1", "input": ["text"], "reasoning": True},
            opts={"image": False, "thinking": True, "ctx_tokens": 0})
        th = self.by_key(res, "think")
        self.assertEqual(th["verdict"], tester.WARN)
        self.assertEqual(th["fixes"][0]["kind"], "reasoning")

    def test_ctx_rejected_flags_overstated_window(self):
        res, _f = self.run_test(
            [ok_chat(), (400, '{"error":{"message":"maximum context length"}}')],
            opts={"image": False, "thinking": False, "ctx_tokens": 4000})
        self.assertEqual(self.by_key(res, "ctx")["verdict"], tester.FAIL)

    def test_opts_can_switch_items_off(self):
        res, fake = self.run_test([ok_chat()],
                                  model={"id": "m1", "input": ["text", "image"],
                                         "reasoning": True},
                                  opts={"image": False, "thinking": False,
                                        "ctx_tokens": 0})
        self.assertEqual(len(fake.calls), 1)
        for key in ("image", "think", "ctx"):
            self.assertEqual(self.by_key(res, key)["verdict"], tester.SKIP)

    def test_stop_flag_short_circuits(self):
        stop = threading.Event()
        stop.set()
        res, fake = self.run_test([ok_chat()], stop=stop)
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.by_key(res, "basic")["verdict"], tester.SKIP)

    def test_env_var_key_warns(self):
        fake = FakeHTTP([ok_chat()])
        fetcher._http_request = fake
        prov = {"api": "openai-completions", "baseUrl": "https://x.com/v1",
                "apiKey": "$MY_KEY"}
        res = tester.test_model(prov, {"id": "m"}, {"image": False,
                                                   "thinking": False,
                                                   "ctx_tokens": 0})
        self.assertEqual(res[0]["key"], "key")
        self.assertEqual(res[0]["verdict"], tester.WARN)


class TestProtocolAndPacing(unittest.TestCase):
    def setUp(self):
        self._orig = fetcher._http_request

    def tearDown(self):
        fetcher._http_request = self._orig

    def run_test(self, responses, api="openai-completions", model=None,
                 provider_extra=None, state=None):
        fake = FakeHTTP(responses)
        fetcher._http_request = fake
        prov = {"api": api, "baseUrl": "https://x.com/v1", "apiKey": "sk-1"}
        prov.update(provider_extra or {})
        model = model or {"id": "m1", "input": ["text"]}
        st = state if state is not None else {}
        res = tester.test_model(prov, model,
                               {"image": False, "thinking": False,
                                "ctx_tokens": 0}, state=st)
        return res, fake, st

    def by_key(self, results, key):
        return next((r for r in results if r["key"] == key), None)

    def test_response_shape(self):
        self.assertEqual(tester.response_shape({"choices": []}),
                         "openai-completions")
        self.assertEqual(tester.response_shape({"object": "chat.completion"}),
                         "openai-completions")
        self.assertEqual(
            tester.response_shape({"content": [{"type": "text", "text": "x"}],
                                   "stop_reason": "end_turn"}),
            "anthropic-messages")
        self.assertEqual(tester.response_shape({"candidates": []}),
                         "google-generative-ai")
        self.assertIsNone(tester.response_shape({"foo": 1}))

    def test_claude_endpoint_returning_openai_body_is_flagged(self):
        """中转常见坑：标了支持 Claude 接口，实际回的是 OpenAI 格式，pi 会解析失败。"""
        res, _f, _st = self.run_test([ok_chat()], api="anthropic-messages")
        proto = self.by_key(res, "protocol")
        self.assertIsNotNone(proto)
        self.assertEqual(proto["verdict"], tester.FAIL)
        self.assertEqual(proto["fixes"][0], {"kind": "api",
                                             "value": "openai-completions",
                                             "why": "实测返回的是OpenAI 兼容的格式"})
        self.assertEqual(tester.overall_of(res), tester.FAIL)

    def test_matching_protocol_has_no_complaint(self):
        res, _f, _st = self.run_test([ok_chat()])
        self.assertIsNone(self.by_key(res, "protocol"))
        self.assertEqual(self.by_key(res, "basic")["verdict"], tester.PASS)

    def test_max_tokens_accepted_is_recorded_as_a_fix(self):
        """pi 对未知中转默认发 max_completion_tokens，实测认 max_tokens 就要写进配置。"""
        res, _f, _st = self.run_test([ok_chat()])
        fixes = tester.collect_fixes(res)
        self.assertEqual(fixes[0]["kind"], "maxTokensField")
        self.assertEqual(fixes[0]["value"], "max_tokens")

    def test_no_suggestion_when_config_already_declares_field(self):
        res, _f, _st = self.run_test(
            [ok_chat()],
            provider_extra={"compat": {"maxTokensField": "max_tokens"}})
        self.assertEqual(tester.collect_fixes(res), [])

    def test_no_suggestion_for_claude_protocol(self):
        res, _f, _st = self.run_test(
            [(200, json.dumps({"content": [{"type": "text", "text": "好"}],
                               "stop_reason": "end_turn"}))],
            api="anthropic-messages")
        self.assertEqual(tester.collect_fixes(res), [])

    def test_detect_abort_patterns(self):
        self.assertTrue(tester.detect_abort(429, ""))
        self.assertTrue(tester.detect_abort(403, "bulk model probing detected"))
        self.assertTrue(tester.detect_abort(403, "IP 已被封禁"))
        self.assertTrue(tester.detect_abort(400, "请求过于频繁"))
        self.assertIsNone(tester.detect_abort(400, "invalid model"))
        self.assertIsNone(tester.detect_abort(-1, "fetch failed"))

    def test_rate_limit_marks_abort_and_stops(self):
        res, fake, st = self.run_test([(429, '{"error":"too many requests"}')])
        self.assertIn("abort", st)
        self.assertEqual(self.by_key(res, "basic")["verdict"], tester.FAIL)
        self.assertEqual(len(fake.calls), 1)

    def test_abort_mid_round_skips_rest(self):
        fake = FakeHTTP([ok_chat(),
                         (429, '{"error":"too many requests"}')])
        fetcher._http_request = fake
        st = {}
        res = tester.test_model(
            {"api": "openai-completions", "baseUrl": "https://x.com/v1",
             "apiKey": "sk"},
            {"id": "m1", "input": ["text", "image"]},
            {"image": True, "thinking": True, "ctx_tokens": 4000}, state=st)
        self.assertIn("abort", st)
        img = self.by_key(res, "image")
        self.assertEqual(img["verdict"], tester.SKIP)
        self.assertIsNone(self.by_key(res, "ctx"))


if __name__ == "__main__":
    unittest.main()
