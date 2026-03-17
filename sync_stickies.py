#!/usr/bin/env python3
"""Mac Stickies → Notion 同步脚本"""

import hashlib
import json
import logging
import os
import requests
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


# ── Hash + 状态 ───────────────────────────────────────────────────────────────
def compute_hash(stickies: List[Tuple[str, float]]) -> str:
    """根据便签文本内容（忽略 mtime）计算 MD5 hash。"""
    combined = "\n---\n".join(text for text, _ in stickies)
    return hashlib.md5(combined.encode("utf-8")).hexdigest()


def load_state(state_file: str = STATE_FILE) -> dict:
    """读取 state.json，损坏或缺失时返回默认值。"""
    default = {"hash": None, "notion_page_id": None}
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_state(state_file: str, state: dict) -> None:
    """持久化 state 到 JSON 文件，目录不存在时自动创建。"""
    os.makedirs(os.path.dirname(state_file) or ".", exist_ok=True)
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# ── Notion 客户端 ─────────────────────────────────────────────────────────────
def notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def notion_find_or_create_page(page_id: str | None) -> str:
    """
    若 page_id 已有则直接返回；否则在 workspace 根目录创建 "Mac stickies" 页面。
    """
    if page_id:
        return page_id

    log.info("创建 Notion 页面: %s", PAGE_TITLE)
    resp = requests.post(
        f"{NOTION_API}/pages",
        headers=notion_headers(),
        json={
            "parent": {"type": "workspace", "workspace": True},
            "properties": {
                "title": {"title": [{"type": "text", "text": {"content": PAGE_TITLE}}]}
            },
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def notion_clear_page(page_id: str) -> None:
    """删除页面中所有现有 blocks。"""
    log.info("清空页面 blocks: %s", page_id)
    cursor = None
    while True:
        params = {"start_cursor": cursor} if cursor else {}
        resp = requests.get(
            f"{NOTION_API}/blocks/{page_id}/children",
            headers=notion_headers(),
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        for block in data.get("results", []):
            del_resp = requests.delete(
                f"{NOTION_API}/blocks/{block['id']}",
                headers=notion_headers(),
                timeout=10,
            )
            del_resp.raise_for_status()

        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")


def stickies_to_blocks(stickies: List[Tuple[str, float]]) -> list:
    """将便签列表转换为 Notion block 对象列表。"""
    blocks = []
    for i, (text, _) in enumerate(stickies):
        lines = [line for line in text.splitlines() if line.strip()]
        for line in lines:
            blocks.append(
                {
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": line}}]
                    },
                }
            )
        if i < len(stickies) - 1:
            blocks.append({"type": "divider", "divider": {}})
    return blocks


def notion_write_stickies(page_id: str, stickies: List[Tuple[str, float]]) -> None:
    """将便签内容写入 Notion 页面（每批最多 100 blocks）。"""
    blocks = stickies_to_blocks(stickies)
    log.info("写入 %d 个 blocks 到页面 %s", len(blocks), page_id)

    # Notion API 每次最多追加 100 个 blocks
    batch_size = 100
    for i in range(0, len(blocks), batch_size):
        batch = blocks[i : i + batch_size]
        resp = requests.patch(
            f"{NOTION_API}/blocks/{page_id}/children",
            headers=notion_headers(),
            json={"children": batch},
            timeout=10,
        )
        resp.raise_for_status()
