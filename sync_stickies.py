#!/usr/bin/env python3
"""Mac Stickies → Notion 同步脚本"""

import hashlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

# ── 配置 ──────────────────────────────────────────────────────────────────────
STICKIES_DIR = os.path.expanduser(
    "~/Library/Containers/com.apple.Stickies/Data/Library/Stickies"
)
STATE_DIR = os.path.expanduser("~/.local/share/stickies-sync")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
NOTION_TOKEN = os.environ.get(
    "NOTION_TOKEN", ""
)  # NOTE: use .get() not [] so tests can import without env var
NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
PAGE_TITLE = "Mac stickies"

# ── 日志 ──────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── RTF 读取 ──────────────────────────────────────────────────────────────────
def read_stickies(stickies_dir: str) -> List[Tuple[str, float]]:
    """
    读取所有 Stickies 便签，返回 [(plain_text, mtime), ...] 按 mtime 倒序。
    空内容或解析失败的便签会被跳过。
    """
    stickies_path = Path(stickies_dir)
    if not stickies_path.exists():
        log.warning("Stickies 目录不存在: %s", stickies_dir)
        return []

    results: List[Tuple[str, float]] = []

    for bundle in stickies_path.glob("*.rtfd"):
        rtf_file = bundle / "TXT.rtf"
        if not rtf_file.exists():
            log.warning("跳过无 TXT.rtf 的 bundle: %s", bundle)
            continue

        proc = subprocess.run(
            ["textutil", "-convert", "txt", "-stdout", str(rtf_file)],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if proc.returncode != 0:
            log.warning("textutil 解析失败: %s", bundle.name)
            continue

        text = proc.stdout.strip()
        if not text:
            log.debug("跳过空便签: %s", bundle.name)
            continue

        mtime = rtf_file.stat().st_mtime
        results.append((text, mtime))

    results.sort(key=lambda x: x[1], reverse=True)
    return results
