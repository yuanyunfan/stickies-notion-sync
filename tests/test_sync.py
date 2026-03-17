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
