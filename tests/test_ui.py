# -*- coding: utf-8 -*-
"""界面层的无头冒烟测试。

界面测试只测逻辑，绝不碰网络：所有会发请求的入口都先换成空操作
（历史上这里踩过坑，假域名的重试链会把测试进程拖死）。
"""
import json
import os
import shutil
import tempfile
import unittest

import catalog as catalog_mod
import store

from .base import pick_one, snapshot_catalog

try:
    import tkinter as tk
    _TK_ERR = None
except Exception as e:  # noqa: BLE001
    tk = None
    _TK_ERR = e


def _make_root():
    root = tk.Tk()
    root.withdraw()
    return root


@unittest.skipIf(tk is None, "没有 tkinter：%s" % _TK_ERR)
class UICase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._old_env = os.environ.get(catalog_mod.ENV_OVERRIDE)
        os.environ[catalog_mod.ENV_OVERRIDE] = snapshot_catalog().source_dir

    @classmethod
    def tearDownClass(cls):
        if cls._old_env is None:
            os.environ.pop(catalog_mod.ENV_OVERRIDE, None)
        else:
            os.environ[catalog_mod.ENV_OVERRIDE] = cls._old_env

    def setUp(self):
        import ui.app as app_mod
        self.app_mod = app_mod
        try:
            self.root = _make_root()
        except tk.TclError as e:
            self.skipTest("这台机器起不了 Tk：%s" % e)
        # 屏蔽网络入口和弹窗，用完在 tearDown 里还原，别污染别的用例
        self.msgs = []
        self._real_fetch = app_mod.App.on_fetch
        app_mod.App.on_fetch = lambda _self: self.msgs.append(("fetch",))

        class FakeBox:
            def __getattr__(inner, name):
                def _f(*a, **kw):
                    self.msgs.append((name,) + a)
                    return True
                return _f
        import ui.dialogs
        import ui.manage
        import ui.results
        self._boxed = [app_mod, ui.dialogs, ui.manage, ui.results]
        self._real_boxes = [(m, m.messagebox) for m in self._boxed]
        for m in self._boxed:
            m.messagebox = FakeBox()
        self.app = app_mod.App(self.root)

    def tearDown(self):
        self.app_mod.App.on_fetch = self._real_fetch
        for m, box in self._real_boxes:
            m.messagebox = box
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass

    def add(self, mid, entry=None, score=0.0):
        return self.app.add_row(mid, entry, score)


class TestMainWindow(UICase):
    def test_builds_and_loads_catalog(self):
        self.assertGreater(len(self.app.catalog), 100)
        self.assertIn("pi 模型配置工具", self.root.title())

    def test_key_field_is_masked_by_default(self):
        self.assertEqual(str(self.app.ent_key.cget("show")), "•")
        self.app.var_showkey.set(True)
        self.app._toggle_key()
        self.assertEqual(str(self.app.ent_key.cget("show")), "")

    def test_headers_box_starts_empty(self):
        """以前预填的示例会被当真写进配置，现在必须是空的。"""
        self.assertEqual(self.app.txt_headers.get("1.0", "end").strip(), "")

    def test_thinking_column_shows_default_not_unavailable(self):
        e = pick_one(lambda x: x.model.get("reasoning")
                     and not x.model.get("thinkingLevelMap"))
        if e is None:
            self.skipTest("快照里没有这类模型")
        iid = self.add(e.mid, e, 1.0)
        think = self.app._row_values(iid)[5]
        self.assertIn("默认", think)
        self.assertNotIn("不可用", think)

    def test_thinking_column_for_partial_map(self):
        e = pick_one(lambda x: x.model.get("reasoning")
                     and 0 < len(x.model.get("thinkingLevelMap") or {}) < 7)
        if e is None:
            self.skipTest("快照里没有这类模型")
        iid = self.add(e.mid, e, 1.0)
        think = self.app._row_values(iid)[5]
        self.assertTrue("可用" in think or "默认" in think)

    def test_duplicate_id_is_not_added_twice(self):
        e = self.app.catalog.entries[0]
        self.assertIsNotNone(self.add(e.mid, e, 1.0))
        self.assertIsNone(self.add(e.mid, e, 1.0))
        self.assertEqual(len(self.app.rowdata), 1)

    def test_search_filters_rows(self):
        self.add("alpha-model")
        self.add("beta-model")
        self.app.var_search.set("alpha")
        self.assertEqual(len(self.app.tree.get_children()), 1)
        self.app.var_search.set("")
        self.assertEqual(len(self.app.tree.get_children()), 2)

    def test_filter_unmatched_only(self):
        e = self.app.catalog.entries[0]
        self.add(e.mid, e, 1.0)
        self.add("totally-unknown-xyz-1")
        self.app.var_filter.set(self.app_mod.FILTER_UNMATCHED)
        self.app.apply_filter()
        shown = [self.app.rowdata[i].mid for i in self.app.tree.get_children()]
        self.assertEqual(shown, ["totally-unknown-xyz-1"])

    def test_check_all_only_touches_visible_rows(self):
        self.add("alpha-model")
        self.add("beta-model")
        self.app.var_search.set("alpha")
        self.app.check_all(True)
        checked = [r.mid for r in self.app.rowdata.values() if r.checked]
        self.assertEqual(checked, ["alpha-model"])

    def test_sort_by_id(self):
        for mid in ("c-model", "a-model", "b-model"):
            self.add(mid)
        self.app.sort_by("id")
        order = [self.app.rowdata[i].mid for i in self.app.tree.get_children()]
        self.assertEqual(order, ["a-model", "b-model", "c-model"])
        self.app.sort_by("id")
        order = [self.app.rowdata[i].mid for i in self.app.tree.get_children()]
        self.assertEqual(order, ["c-model", "b-model", "a-model"])

    def test_claude_auto_rule(self):
        self.add("claude-sonnet-9-fake")
        self.app._apply_auto_rules()
        row = list(self.app.rowdata.values())[0]
        self.assertEqual(row.api_override, "anthropic-messages")

    def test_claude_auto_rule_can_be_switched_off(self):
        self.app.var_claude_auto.set(False)
        self.add("claude-sonnet-9-fake")
        self.app._apply_auto_rules()
        self.assertIsNone(list(self.app.rowdata.values())[0].api_override)

    def test_empty_key_write_is_blocked(self):
        self.app.var_name.set("svc")
        self.app.var_url.set("https://x.com/v1")
        self.app.var_key.set("")
        self.add("m1")
        list(self.app.rowdata.values())[0].checked = True
        self.app.on_write()
        names = [m[0] for m in self.msgs]
        self.assertIn("showwarning", names)
        said = " ".join(str(x) for m in self.msgs for x in m)
        self.assertIn("密钥", said)


class TestProviderEntries(UICase):
    def rows(self):
        r1 = store.ModelRow("gpt-x")
        r1.apply_manual()
        r2 = store.ModelRow("claude-x")
        r2.apply_manual()
        r2.api_override = "anthropic-messages"
        return [r1, r2]

    def build(self, provs=None, **kw):
        for key, val in kw.items():
            if key == "split":
                self.app.var_split.set(val)
            else:
                self.app.relay_vars[key].set(val)
        return dict(self.app._build_provider_entries(
            "svc", "https://x.com/v1", "openai-completions", "sk-1",
            self.rows(), "", False, provs or {}))

    def test_default_keeps_one_provider_with_model_level_base_url(self):
        """pi 允许模型级 baseUrl，所以混协议不用再拆服务商。"""
        got = self.build()
        self.assertEqual(list(got), ["svc"])
        models = {m["id"]: m for m in got["svc"]["models"]}
        self.assertEqual(got["svc"]["baseUrl"], "https://x.com/v1")
        self.assertNotIn("baseUrl", models["gpt-x"])
        self.assertNotIn("api", models["gpt-x"])
        self.assertEqual(models["claude-x"]["api"], "anthropic-messages")
        self.assertEqual(models["claude-x"]["baseUrl"], "https://x.com")

    def test_split_mode_still_available(self):
        got = self.build(split=True)
        self.assertEqual(sorted(got), ["svc", "svc-claude"])
        self.assertEqual(got["svc"]["baseUrl"], "https://x.com/v1")
        self.assertEqual(got["svc-claude"]["baseUrl"], "https://x.com")
        # 拆开之后每组地址已经对了，模型不需要再自带地址
        self.assertNotIn("baseUrl", got["svc-claude"]["models"][0])

    def test_relay_preset_written_to_provider_compat(self):
        got = self.build()
        self.assertEqual(got["svc"]["compat"]["supportsDeveloperRole"], False)
        self.assertEqual(got["svc"]["compat"]["maxTokensField"], "max_tokens")
        # Claude 协议的模型自带对应协议的预设
        claude = [m for m in got["svc"]["models"] if m["id"] == "claude-x"][0]
        self.assertIs(claude["compat"]["supportsEagerToolInputStreaming"], False)

    def test_unchecking_preset_removes_only_preset_keys(self):
        provs = {"svc": {"baseUrl": "old", "api": "openai-completions",
                         "apiKey": "sk-1",
                         "compat": {"supportsStore": True,
                                    "supportsDeveloperRole": False},
                         "models": []}}
        got = self.build(provs=provs, no_developer_role=False,
                         max_tokens_field=False)
        self.assertEqual(got["svc"]["compat"], {"supportsStore": True})

    def test_keeps_existing_provider_extras(self):
        provs = {"svc": {"baseUrl": "old", "api": "openai-completions",
                         "apiKey": "sk-1", "name": "旧名字",
                         "compat": {"supportsStore": True},
                         "models": []}}
        got = self.build(provs=provs)
        p = got["svc"]
        self.assertEqual(p["name"], "旧名字")
        self.assertTrue(p["compat"]["supportsStore"])       # 原有的留着
        self.assertIs(p["compat"]["supportsDeveloperRole"], False)  # 预设也加上


class TestLoadExisting(UICase):
    def test_headers_and_auth_header_are_restored(self):
        data = {"providers": {"svc": {
            "baseUrl": "https://x.com", "api": "anthropic-messages",
            "apiKey": "sk-1", "authHeader": True,
            "headers": {"User-Agent": "claude-cli/2.1.0", "x-app": "cli"},
            "name": "旧名字",
            "models": [{"id": "claude-x", "contextWindow": 200000,
                        "maxTokens": 64000, "reasoning": True,
                        "input": ["text", "image"]}]}}}
        self.app.load_existing_provider(data, "svc")
        self.assertEqual(self.app.var_key.get(), "sk-1")
        self.assertTrue(self.app.var_auth_header.get())
        self.assertEqual(store.parse_headers(self.app.txt_headers.get("1.0", "end")),
                         {"User-Agent": "claude-cli/2.1.0", "x-app": "cli"})
        row = list(self.app.rowdata.values())[0]
        self.assertTrue(row.from_config and row.checked)
        # 原配置没写这些兼容项，导入后预设就该是未勾选，别偷偷改人家的配置
        self.assertFalse(self.app.relay_vars["no_developer_role"].get())
        self.assertFalse(self.app.relay_vars["max_tokens_field"].get())
        # 重新写入时请求头、authHeader 和 name 都要还在
        entries = self.app._build_provider_entries(
            "svc", "https://x.com", "anthropic-messages", "sk-1",
            [row], self.app.txt_headers.get("1.0", "end"),
            self.app.var_auth_header.get(), data["providers"])
        p = entries[0][1]
        self.assertEqual(p["headers"]["x-app"], "cli")
        self.assertTrue(p["authHeader"])
        self.assertEqual(p["name"], "旧名字")


class TestManagementDialog(UICase):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp(prefix="pimct_ui_")
        self.path = os.path.join(self.dir, "models.json")
        import ui.manage as manage_mod
        self.manage_mod = manage_mod
        self._real_path = manage_mod.CONFIG_PATH
        manage_mod.CONFIG_PATH = self.path

    def tearDown(self):
        self.manage_mod.CONFIG_PATH = self._real_path
        shutil.rmtree(self.dir, ignore_errors=True)
        super().tearDown()

    def write_cfg(self, providers):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"providers": providers}, f)

    def open_dialog(self):
        try:
            return self.manage_mod.ManagementDialog(self.app)
        except tk.TclError as e:
            self.skipTest("这台机器开不了子窗口：%s" % e)

    def test_duplicate_model_ids_do_not_crash(self):
        """以前用 hash 当行号，同 ID 会撞车让整个管理窗口打不开。"""
        self.write_cfg({"p": {"baseUrl": "https://x/v1", "api": "openai-completions",
                              "apiKey": "sk", "models": [
                                  {"id": "same", "contextWindow": 1},
                                  {"id": "same", "contextWindow": 2}]}})
        dlg = self.open_dialog()
        dlg.cur = "p"
        dlg._fill_models()
        self.assertEqual(len(dlg.mtv.get_children()), 2)
        dlg.destroy()

    def test_pending_summary_counts_edits(self):
        self.write_cfg({"p": {"baseUrl": "https://x/v1", "api": "openai-completions",
                              "apiKey": "sk", "models": [
                                  {"id": "m1", "contextWindow": 1000,
                                   "maxTokens": 100}]}})
        dlg = self.open_dialog()
        dlg.cur = "p"
        dlg._fill_models()
        row = store.row_from_config({"id": "m1", "contextWindow": 2000,
                                     "maxTokens": 200})
        dlg._save_edit(row)
        self.assertIn("待修改模型 1 个", dlg.var_pending.get())
        text = dlg._build_preview_text(dlg.cfg["providers"])
        self.assertIn("将更新参数的模型", text)
        dlg.destroy()

    def test_apply_fixes_records_edits(self):
        self.write_cfg({"p": {"baseUrl": "https://x/v1", "api": "openai-completions",
                              "apiKey": "sk", "models": [
                                  {"id": "m1", "contextWindow": 1000,
                                   "maxTokens": 100,
                                   "input": ["text", "image"]}]}})
        dlg = self.open_dialog()
        dlg.cur = "p"
        dlg._fill_models()
        dlg._apply_fixes({"m1": [{"kind": "input_image", "value": False,
                                  "why": "实测被拒"}]})
        self.assertEqual(dlg.m_edit["p"]["m1"]["input"], ["text"])
        dlg.destroy()

    def test_delete_marks(self):
        self.write_cfg({"p": {"baseUrl": "https://x/v1", "api": "openai-completions",
                              "apiKey": "sk", "models": [{"id": "m1"}]},
                        "q": {"baseUrl": "https://y/v1", "api": "openai-completions",
                              "apiKey": "sk", "models": []}})
        dlg = self.open_dialog()
        dlg.p_mark.add("q")
        dlg._refresh_pending()
        self.assertIn("待删除服务商 1 个", dlg.var_pending.get())
        self.assertEqual(str(dlg.btn_apply.cget("state")), "normal")
        dlg.destroy()


class TestDialogsConstruct(UICase):
    """拆分成多个模块后，每个窗口都要能正常建起来。"""

    def open(self, factory):
        try:
            return factory()
        except tk.TclError as e:
            self.skipTest("这台机器开不了子窗口：%s" % e)

    def test_edit_dialog(self):
        import ui.dialogs as dialogs
        e = pick_one(lambda x: x.model.get("reasoning"))
        row = store.entry_to_row(e.mid, e, 1.0)
        dlg = self.open(lambda: dialogs.EditDialog(self.root, self.app.catalog,
                                                   row, on_ok=lambda _r: None))
        dlg.var_search.set(e.mid)
        dlg._search()
        self.assertTrue(dlg.candidates)
        dlg.lst.selection_set(0)
        dlg._adopt()
        dlg._ok()
        self.assertEqual(row.mid, e.mid)

    def test_preview_dialog(self):
        import ui.dialogs as dialogs
        entries = [("svc", store.build_provider("https://x.com/v1",
                                                "openai-completions", "sk-1",
                                                [{"id": "m"}]))]
        dlg = self.open(lambda: dialogs.PreviewDialog(
            self.app, entries, notes=["新增服务商 svc"],
            warnings=["随便一条提醒"], pi_note="已用 pi 校验器复核通过。"))
        dlg.destroy()

    def test_manual_paste_dialog(self):
        import ui.dialogs as dialogs
        dlg = self.open(lambda: dialogs.ManualPasteDialog(self.app))
        dlg.destroy()
        self.app.add_manual_ids(["alpha-x", "beta-y"])
        self.assertEqual(len(self.app.rowdata), 2)

    def test_provider_picker_dialog(self):
        import ui.dialogs as dialogs
        data = {"providers": {"p": {"baseUrl": "https://x",
                                    "api": "openai-completions",
                                    "apiKey": "sk", "models": [{"id": "m"}]}}}
        dlg = self.open(lambda: dialogs.ProviderPickerDialog(self.app, data))
        self.assertEqual(len(dlg.tv.get_children()), 1)
        dlg.destroy()

    def test_write_result_dialog(self):
        import ui.dialogs as dialogs
        dlg = self.open(lambda: dialogs.WriteResultDialog(
            self.root, "标题", "摘要", "明细", backup_path=None, ok=True))
        dlg.destroy()

    def test_results_window_collects_fixes(self):
        import ui.results as results
        applied = {}
        trw = self.open(lambda: results.TestResultsWindow(
            self.root, 1, on_apply_fixes=applied.update))
        trw.q.put(("result", "m1", "图片识别", "fail", 12.0, "被拒",
                   [{"kind": "input_image", "value": False, "why": "被拒"}]))
        trw.q.put(("overall", "m1", "warn"))
        trw.q.put(("progress", "测试中"))
        trw._poll()
        self.assertEqual(len(trw.tv.get_children()), 2)
        self.assertIn("m1", trw.fixes)
        self.assertEqual(str(trw.btn_fix.cget("state")), "normal")
        trw._apply_fixes()
        self.assertIn("m1", applied)
        trw.destroy()


class TestWriteFlow(UICase):
    """完整跑一遍写入：自检、备份、落盘、复核。"""

    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp(prefix="pimct_write_")
        self.path = os.path.join(self.dir, "models.json")
        self._real_path = self.app_mod.CONFIG_PATH
        self._real_validator = self.app_mod.validator
        self.app_mod.CONFIG_PATH = self.path

        class FakeValidator:
            check_config = staticmethod(self._real_validator.check_config)

            @staticmethod
            def pi_available():
                return False

            @staticmethod
            def pi_list_models(name=None, timeout=60):
                return None, "测试环境不调 pi"
        self.app_mod.validator = FakeValidator

    def tearDown(self):
        self.app_mod.CONFIG_PATH = self._real_path
        self.app_mod.validator = self._real_validator
        shutil.rmtree(self.dir, ignore_errors=True)
        super().tearDown()

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def entry(self, name="svc", key="sk-1", mid="m1"):
        row = store.ModelRow(mid)
        row.apply_manual()
        return [(name, store.build_provider("https://x.com/v1",
                                            "openai-completions", key,
                                            [store.build_model_dict(row)]))]

    def test_do_write_creates_file_and_backup(self):
        self.app.do_write(self.entry())
        self.assertEqual(list(self.read()["providers"]), ["svc"])
        self.app.do_write(self.entry(mid="m2"))
        self.assertTrue(store.list_backups(self.path))

    def test_do_write_keeps_other_providers(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"providers": {"keepme": {"baseUrl": "https://y/v1",
                                                "apiKey": "sk-y",
                                                "models": []}}}, f)
        self.app.do_write(self.entry())
        self.assertEqual(sorted(self.read()["providers"]), ["keepme", "svc"])

    def test_restore_after_write(self):
        self.app.do_write(self.entry())
        before = self.read()
        self.app.do_write(self.entry(mid="m9"))
        bak = store.list_backups(self.path)[0]
        self.app._restore(bak)
        self.assertEqual(self.read(), before)

    def test_invalid_config_never_reaches_disk(self):
        bad = [("svc", {"baseUrl": "https://x/v1", "api": "openai-completions",
                        "apiKey": "sk",
                        "models": [{"id": "m", "input": ["text", "video"]}]})]
        self.app.do_write(bad)
        self.assertFalse(os.path.exists(self.path))
        self.assertTrue(any(m[0] == "showerror" for m in self.msgs))


if __name__ == "__main__":
    unittest.main()
