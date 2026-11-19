# -*- coding: utf-8 -*-
"""纯函数单元测试：不需要浏览器，直接 python tests/test_unit.py 运行"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import xhs_video_downloader as x  # noqa: E402


class TestUtils(unittest.TestCase):
    def test_sanitize_filename(self):
        self.assertEqual(x.sanitize_filename('a/b\\c:d*e?f"g<h>i|j'), "a_b_c_d_e_f_g_h_i_j")
        self.assertEqual(x.sanitize_filename("   "), "untitled")
        self.assertEqual(len(x.sanitize_filename("标题" * 30)), 60)
        self.assertEqual(x.sanitize_filename("  hello   world  "), "hello world")

    def test_parse_user_id(self):
        url = ("https://www.xiaohongshu.com/user/profile/67a4612d000000000d009085"
               "?xsec_token=PLACEHOLDER_TOKEN&xsec_source=app_share")
        self.assertEqual(x.parse_user_id(url), "67a4612d000000000d009085")
        self.assertEqual(x.parse_user_id("67A4612D000000000D009085"), "67a4612d000000000d009085")
        with self.assertRaises(ValueError):
            x.parse_user_id("https://www.example.com/page")
        with self.assertRaises(ValueError):
            x.parse_user_id("")

    def test_pick_best_stream(self):
        stream = {
            "h264": [{"master_url": "https://cdn/720.mp4", "height": 720}],
            "h265": [{"master_url": "https://cdn/1080.mp4", "height": 1080,
                      "backup_urls": ["https://cdn/1080b.mp4"]}],
        }
        # h264 优先，即使 h265 清晰度更高
        self.assertEqual(x.pick_best_stream(stream)["master_url"], "https://cdn/720.mp4")
        # 只有 h265 时取最高清
        self.assertEqual(x.pick_best_stream({"h265": stream["h265"]})["height"], 1080)
        # 同编码取清晰度高
        multi = {"h264": [{"master_url": "u1", "height": 540},
                          {"master_url": "u2", "height": 1080}]}
        self.assertEqual(x.pick_best_stream(multi)["master_url"], "u2")
        # 无 master_url 时用 backup
        self.assertEqual(
            x.pick_best_stream({"h264": [{"backup_urls": ["https://cdn/b.mp4"], "height": 540}]})
            ["backup_urls"], ["https://cdn/b.mp4"])
        self.assertIsNone(x.pick_best_stream({}))
        self.assertIsNone(x.pick_best_stream(None))

    def test_ms_to_date(self):
        self.assertEqual(x.ms_to_date(1729234567000), "2024-10-18".replace("-", ""))
        self.assertEqual(len(x.ms_to_date(None)), 8)


class TestDownloadState(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = x.DownloadState.load(path)
            state.downloaded["n1"] = "a.mp4"
            state.failed["n2"] = "boom"
            state.skipped["n3"] = "非视频"
            state.index.append({"note_id": "n1"})
            state.save()
            state2 = x.DownloadState.load(path)
            self.assertEqual(state2.downloaded, {"n1": "a.mp4"})
            self.assertEqual(state2.failed, {"n2": "boom"})
            self.assertEqual(state2.skipped, {"n3": "非视频"})
            self.assertEqual(state2.index, [{"note_id": "n1"}])

    def test_load_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{broken json", encoding="utf-8")
            state = x.DownloadState.load(path)
            self.assertEqual(state.downloaded, {})


class TestSelectPending(unittest.TestCase):
    def test_video_and_unknown_included(self):
        notes = [
            {"id": "v1", "type": "video"},
            {"id": "u1", "type": "unknown"},
            {"id": "n1", "type": "normal"},
        ]
        pending = x.select_pending(notes, {})
        self.assertEqual([n["id"] for n in pending], ["v1", "u1"])

    def test_downloaded_skipped(self):
        notes = [{"id": "v1", "type": "video"}, {"id": "u1", "type": "unknown"}]
        pending = x.select_pending(notes, {"v1": "a.mp4"})
        self.assertEqual([n["id"] for n in pending], ["u1"])

    def test_confirmed_image_skipped_but_failed_retried(self):
        notes = [{"id": "u1", "type": "unknown"}, {"id": "u2", "type": "unknown"}]
        skipped = {"u1": "非视频笔记", "u2": "详情解析失败（可能已删除或暂无权限）"}
        pending = x.select_pending(notes, {}, skipped)
        self.assertEqual([n["id"] for n in pending], ["u2"])

    def test_empty(self):
        self.assertEqual(x.select_pending([], {}), [])
        self.assertEqual(x.select_pending([{"id": "n1", "type": "normal"}], None), [])


class TestFindInJson(unittest.TestCase):
    def test_nested_search(self):
        payload = {"a": {"b": [{"c": 1}, {"video": {"x": 1}}]}}
        found = x.find_in_json(payload, lambda d: isinstance(d.get("video"), dict))
        self.assertEqual(found, {"video": {"x": 1}})

    def test_not_found(self):
        self.assertIsNone(x.find_in_json({"a": {"b": 1}}, lambda d: d.get("video")))
        self.assertIsNone(x.find_in_json([1, 2, [3]], lambda d: isinstance(d, dict) and d.get("x")))

    def test_top_level_hit(self):
        found = x.find_in_json({"video": {}, "id": "n1"}, lambda d: "video" in d)
        self.assertIn("video", found)

    def test_depth_limit(self):
        # 超过 12 层的嵌套不再下钻，不会爆栈
        deep = current = {}
        for _ in range(30):
            current["child"] = {}
            current = current["child"]
        current["video"] = {"deep": True}
        self.assertIsNone(x.find_in_json(deep, lambda d: isinstance(d.get("video"), dict)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
