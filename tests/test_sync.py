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
    result = read_stickies(STICKIES_DIR)
    if len(result) >= 2:
        assert result[0][1] >= result[1][1]


def test_read_stickies_skips_empty():
    """全空内容的便签应被跳过"""
    with tempfile.TemporaryDirectory() as tmpdir:
        # 模拟一个 rtfd bundle，textutil 返回空字符串
        bundle = os.path.join(tmpdir, "empty.rtfd")
        os.makedirs(bundle)
        open(os.path.join(bundle, "TXT.rtf"), "w").close()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="   \n  ")
            result = read_stickies(tmpdir)
        assert result == []


def test_read_stickies_handles_textutil_failure():
    """textutil 失败时跳过该便签，不抛异常"""
    with tempfile.TemporaryDirectory() as tmpdir:
        bundle = os.path.join(tmpdir, "bad.rtfd")
        os.makedirs(bundle)
        open(os.path.join(bundle, "TXT.rtf"), "w").close()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            result = read_stickies(tmpdir)
        assert result == []
