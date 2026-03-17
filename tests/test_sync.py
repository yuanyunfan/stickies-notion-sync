import os
import tempfile
import subprocess
import pytest
from unittest.mock import patch, MagicMock
from sync_stickies import read_stickies

STICKIES_DIR = os.path.expanduser(
    "~/Library/Containers/com.apple.Stickies/Data/Library/Stickies"
)


def test_read_stickies_returns_list():
    """read_stickies 应返回列表，每项为 (text: str, mtime: float)"""
    result = read_stickies(STICKIES_DIR)
    assert isinstance(result, list)
    for text, mtime in result:
        assert isinstance(text, str)
        assert isinstance(mtime, float)


def test_read_stickies_sorted_by_mtime_desc():
    """结果应按 mtime 倒序（最近修改在前）"""
    with tempfile.TemporaryDirectory() as tmpdir:
        # 创建两个 rtfd bundle
        bundle1 = os.path.join(tmpdir, "old.rtfd")
        bundle2 = os.path.join(tmpdir, "new.rtfd")
        os.makedirs(bundle1)
        os.makedirs(bundle2)
        open(os.path.join(bundle1, "TXT.rtf"), "w").close()
        open(os.path.join(bundle2, "TXT.rtf"), "w").close()

        # bundle2 的 mtime 更新（值更大）
        old_mtime = 1000.0
        new_mtime = 2000.0

        def fake_run(cmd, **kwargs):
            # 根据文件路径返回对应文本
            path = cmd[-1]
            if "old.rtfd" in path:
                return MagicMock(returncode=0, stdout="old sticky")
            else:
                return MagicMock(returncode=0, stdout="new sticky")

        def fake_stat(self, *, follow_symlinks=True):
            m = MagicMock()
            if "old.rtfd" in str(self):
                m.st_mtime = old_mtime
            else:
                m.st_mtime = new_mtime
            return m

        with (
            patch("sync_stickies.subprocess.run", side_effect=fake_run),
            patch("pathlib.Path.stat", fake_stat),
        ):
            result = read_stickies(tmpdir)

        assert len(result) == 2
        assert result[0][1] == new_mtime  # 最新的在前
        assert result[1][1] == old_mtime


def test_read_stickies_skips_empty():
    """全空内容的便签应被跳过"""
    with tempfile.TemporaryDirectory() as tmpdir:
        # 模拟一个 rtfd bundle，textutil 返回空字符串
        bundle = os.path.join(tmpdir, "empty.rtfd")
        os.makedirs(bundle)
        open(os.path.join(bundle, "TXT.rtf"), "w").close()

        with patch("sync_stickies.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="   \n  ")
            result = read_stickies(tmpdir)
        assert result == []


def test_read_stickies_handles_textutil_failure():
    """textutil 失败时跳过该便签，不抛异常"""
    with tempfile.TemporaryDirectory() as tmpdir:
        bundle = os.path.join(tmpdir, "bad.rtfd")
        os.makedirs(bundle)
        open(os.path.join(bundle, "TXT.rtf"), "w").close()

        with patch("sync_stickies.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            result = read_stickies(tmpdir)
        assert result == []


from sync_stickies import compute_hash, load_state, save_state


def test_compute_hash_deterministic():
    """相同输入，hash 相同"""
    stickies = [("hello world", 1.0), ("second sticky", 2.0)]
    h1 = compute_hash(stickies)
    h2 = compute_hash(stickies)
    assert h1 == h2
    assert len(h1) == 32  # MD5 hex


def test_compute_hash_sensitive_to_content():
    """内容不同，hash 不同"""
    h1 = compute_hash([("aaa", 1.0)])
    h2 = compute_hash([("bbb", 1.0)])
    assert h1 != h2


def test_compute_hash_ignores_mtime():
    """mtime 不影响 hash（只检测内容变化）"""
    h1 = compute_hash([("same text", 1.0)])
    h2 = compute_hash([("same text", 999.0)])
    assert h1 == h2


def test_load_state_returns_defaults_when_missing(tmp_path):
    """state.json 不存在时返回默认值"""
    state = load_state(str(tmp_path / "state.json"))
    assert state == {"hash": None, "notion_page_id": None}


def test_load_state_returns_defaults_on_corrupt(tmp_path):
    """state.json 内容损坏时返回默认值"""
    bad_file = tmp_path / "state.json"
    bad_file.write_text("not json")
    state = load_state(str(bad_file))
    assert state == {"hash": None, "notion_page_id": None}


def test_save_and_load_state_roundtrip(tmp_path):
    """save → load 应得到相同数据"""
    path = str(tmp_path / "state.json")
    data = {"hash": "abc123", "notion_page_id": "xxx-yyy"}
    save_state(path, data)
    assert load_state(path) == data


import requests
from sync_stickies import (
    notion_headers,
    notion_find_or_create_page,
    notion_clear_page,
    notion_write_stickies,
    stickies_to_blocks,
)


def test_notion_headers_contain_auth():
    headers = notion_headers()
    assert "Authorization" in headers
    assert headers["Authorization"].startswith("Bearer ")
    assert headers["Notion-Version"] == "2022-06-28"


def test_stickies_to_blocks_single():
    """单条便签 → 若干 paragraph blocks，无 divider"""
    stickies = [("line1\nline2", 1.0)]
    blocks = stickies_to_blocks(stickies)
    types = [b["type"] for b in blocks]
    assert "paragraph" in types
    assert "divider" not in types


def test_stickies_to_blocks_multiple_has_dividers():
    """多条便签 → 便签间有 divider"""
    stickies = [("a", 1.0), ("b", 2.0)]
    blocks = stickies_to_blocks(stickies)
    types = [b["type"] for b in blocks]
    assert "divider" in types
    # divider 不在末尾
    assert types[-1] == "paragraph"


def test_stickies_to_blocks_skips_empty_lines():
    """空行不产生 paragraph block"""
    stickies = [("line1\n\n\nline2", 1.0)]
    blocks = stickies_to_blocks(stickies)
    para_texts = [
        b["paragraph"]["rich_text"][0]["text"]["content"]
        for b in blocks
        if b["type"] == "paragraph"
    ]
    assert "" not in para_texts
    assert len(para_texts) == 2


def test_notion_find_or_create_page_creates_when_not_found():
    """page_id 为 None 时调用 POST /pages"""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id": "new-page-id"}
    mock_resp.raise_for_status = MagicMock()
    with patch("requests.post", return_value=mock_resp) as mock_post:
        page_id = notion_find_or_create_page(None)
    assert page_id == "new-page-id"
    mock_post.assert_called_once()


def test_notion_find_or_create_page_reuses_existing():
    """page_id 已存在时直接返回，不调用 API"""
    with patch("requests.post") as mock_post:
        page_id = notion_find_or_create_page("existing-id")
    assert page_id == "existing-id"
    mock_post.assert_not_called()


def test_notion_clear_page_handles_pagination():
    """has_more=True 时应继续循环，直到 has_more=False 才停止"""
    block_resp_1 = MagicMock()
    block_resp_1.raise_for_status = MagicMock()
    block_resp_1.json.return_value = {
        "results": [{"id": "block-1"}],
        "has_more": True,
    }
    block_resp_2 = MagicMock()
    block_resp_2.raise_for_status = MagicMock()
    block_resp_2.json.return_value = {"results": [], "has_more": False}

    del_resp = MagicMock()
    del_resp.raise_for_status = MagicMock()

    with (
        patch("requests.get", side_effect=[block_resp_1, block_resp_2]) as mock_get,
        patch("requests.delete", return_value=del_resp) as mock_del,
    ):
        notion_clear_page("page-123")

    assert mock_get.call_count == 2
    assert mock_del.call_count == 1
