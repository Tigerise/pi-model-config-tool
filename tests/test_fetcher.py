# -*- coding: utf-8 -*-
import json
import unittest

import fetcher


class TestUrlVariants(unittest.TestCase):
    def test_openai_without_version(self):
        self.assertEqual(fetcher._url_variants("https://x.com", fetcher.API_OPENAI),
                         ["https://x.com/v1/models", "https://x.com/models"])

    def test_openai_with_version(self):
        self.assertEqual(fetcher._url_variants("https://x.com/v1/",
                                               fetcher.API_OPENAI),
                         ["https://x.com/v1/models"])

    def test_google_adds_v1beta(self):
        self.assertEqual(
            fetcher._url_variants("https://x.com", fetcher.API_GOOGLE)[0],
            "https://x.com/v1beta/models")

    def test_anthropic_like_openai_paths(self):
        self.assertIn("https://x.com/v1/models",
                      fetcher._url_variants("https://x.com", fetcher.API_ANTHROPIC))

    def test_requires_scheme(self):
        with self.assertRaises(fetcher.FetchError):
            fetcher._url_variants("x.com", fetcher.API_OPENAI)


class TestParsers(unittest.TestCase):
    def test_openai_data_list(self):
        self.assertEqual(fetcher._parse_openai({"data": [{"id": "a"}, {"id": "b"}]}),
                         ["a", "b"])

    def test_openai_plain_list(self):
        self.assertEqual(fetcher._parse_openai(["a", "b"]), ["a", "b"])

    def test_openai_nested_models(self):
        self.assertEqual(fetcher._parse_openai({"data": {"models": [{"name": "a"}]}}),
                         ["a"])

    def test_openai_ignores_junk(self):
        self.assertEqual(fetcher._parse_openai({"data": [{"x": 1}, None]}), [])

    def test_google_strips_prefix(self):
        self.assertEqual(
            fetcher._parse_google({"models": [{"name": "models/gemini-x"},
                                              {"name": "plain"}]}),
            ["gemini-x", "plain"])


class TestRedact(unittest.TestCase):
    def test_hides_key(self):
        self.assertEqual(fetcher.redact("Bearer sk-123456 bad", "sk-123456"),
                         "Bearer *** bad")

    def test_hides_url_encoded_key(self):
        key = "sk-a/b+c=="
        text = "https://x.com?key=" + "sk-a%2Fb%2Bc%3D%3D"
        self.assertNotIn("sk-a%2F", fetcher.redact(text, key))

    def test_short_values_left_alone(self):
        self.assertEqual(fetcher.redact("abc", "abc"), "abc")

    def test_handles_none(self):
        self.assertEqual(fetcher.redact(None, "sk-123456"), "")


class TestFetchModels(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self._orig = fetcher._http_request

    def tearDown(self):
        fetcher._http_request = self._orig

    def fake(self, mapping, default=(401, '{"error":"bad key"}')):
        def _f(url, headers, method="GET", body=None, timeout=25):
            self.calls.append((url, headers))
            for frag, resp in mapping.items():
                if frag in url:
                    return resp
            return default
        fetcher._http_request = _f

    def test_auto_falls_back_to_anthropic(self):
        self.fake({"/v1/models": (401, '{"error":"no"}')})
        # openai 与 anthropic 用同一路径，这里让第二次（带 x-api-key）成功
        seq = {"n": 0}

        def _f(url, headers, method="GET", body=None, timeout=25):
            seq["n"] += 1
            if "x-api-key" in headers:
                return 200, json.dumps({"data": [{"id": "claude-x"}]})
            return 401, '{"error":"no"}'
        fetcher._http_request = _f
        kind, ids, base = fetcher.fetch_models("https://x.com", "sk-1")
        self.assertEqual(kind, fetcher.API_ANTHROPIC)
        self.assertEqual(ids, ["claude-x"])
        self.assertEqual(base, "https://x.com/v1")

    def test_dedups_ids(self):
        self.fake({"models": (200, json.dumps({"data": [{"id": "a"}, {"id": "a"},
                                                        {"id": "b"}]}))})
        _kind, ids, _base = fetcher.fetch_models("https://x.com/v1", "sk-1",
                                                 fetcher.API_OPENAI)
        self.assertEqual(ids, ["a", "b"])

    def test_error_message_has_no_key(self):
        self.fake({}, default=(401, "invalid key sk-secret-123456"))
        with self.assertRaises(fetcher.FetchError) as cm:
            fetcher.fetch_models("https://x.com/v1", "sk-secret-123456",
                                 fetcher.API_OPENAI)
        self.assertNotIn("sk-secret-123456", str(cm.exception))

    def test_google_key_not_in_returned_base(self):
        self.fake({"models": (200, json.dumps({"models": [{"name": "models/g"}]}))})
        _kind, _ids, base = fetcher.fetch_models("https://x.com", "sk-1",
                                                 fetcher.API_GOOGLE)
        self.assertNotIn("key=", base)

    def test_all_three_fail(self):
        self.fake({}, default=(500, "boom"))
        with self.assertRaises(fetcher.FetchError):
            fetcher.fetch_models("https://x.com", "sk-1")

    def test_empty_list_is_an_error(self):
        self.fake({"models": (200, json.dumps({"data": []}))})
        with self.assertRaises(fetcher.FetchError):
            fetcher.fetch_models("https://x.com/v1", "sk-1", fetcher.API_OPENAI)


class TestNodeBridge(unittest.TestCase):
    def test_script_is_bundled(self):
        self.assertTrue(fetcher._script_path(), "nodefetch.mjs 应该在项目里")

    def test_no_node_means_python_path(self):
        old = fetcher._NODE_PATH
        fetcher._NODE_PATH = None
        try:
            self.assertIsNone(fetcher._node_request("https://x", {}, "GET", None, 5))
        finally:
            fetcher._NODE_PATH = old

    @unittest.skipUnless(fetcher._NODE_PATH, "本机没装 node")
    def test_node_bridge_does_not_leak_key_in_argv(self):
        """密钥必须走标准输入。这里检查命令行参数里只有脚本路径。"""
        import subprocess
        seen = {}
        orig = subprocess.run

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            seen["input"] = kw.get("input")
            return orig([fetcher._NODE_PATH, "-e", "console.log('@@STATUS@@200');console.log('ok')"],
                        **{k: v for k, v in kw.items() if k != "input"})
        subprocess.run = fake_run
        try:
            status, text = fetcher._http_request(
                "https://x.com", {"Authorization": "Bearer sk-secret-123456"})
        finally:
            subprocess.run = orig
        self.assertEqual(status, 200)
        self.assertNotIn("sk-secret-123456", " ".join(seen["cmd"]))
        self.assertIn("sk-secret-123456", seen["input"])


if __name__ == "__main__":
    unittest.main()
