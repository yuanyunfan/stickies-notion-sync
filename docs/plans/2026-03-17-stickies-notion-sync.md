# Stickies → Notion 同步 实现计划

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 每 10 分钟检查 Mac Stickies 内容变化，有变化则同步到 Notion "Mac stickies" 页面。

**Architecture:** 单一 Python 脚本读取 Stickies RTF 文件（textutil 转纯文本），MD5 hash 比对变化，调用 Notion REST API 更新页面；LaunchAgent plist 驱动定时执行。

**Tech Stack:** Python 3（stdlib + requests），macOS textutil，Notion REST API v1，launchd

---

## 文件结构

```
stickies-notion-sync/
├── sync_stickies.py                          # 主脚本（全部逻辑）
├── com.user.stickies-sync.plist              # LaunchAgent 配置
├── install.sh                                # 一键安装脚本
├── tests/
│   └── test_sync.py                          # 单元测试
├── docs/
│   ├── specs/2026-03-17-stickies-notion-sync-design.md
│   └── plans/2026-03-17-stickies-notion-sync.md
└── .gitignore
```

---

## Chunk 1: 核心脚本

### Task 1: 项目脚手架

**Files:**
- Create: `.gitignore`
- Create: `tests/__init__.py`

- [ ] **Step 1: 写 .gitignore**

```
state.json
*.log
__pycache__/
*.pyc
.env
com.user.stickies-sync.plist
```

- [ ] **Step 2: 创建空 tests/__init__.py**

```python
# tests package
```

- [ ] **Step 3: 初始提交**

Run: `git add .gitignore tests/__init__.py docs/ && git commit -m "chore: project scaffolding"`

---

### Task 2: RTF 读取函数

**Files:**
- Create: `sync_stickies.py`（只含 read_stickies 函数）
- Create: `tests/test_sync.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_sync.py`：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /Users/yuanyunfan/Code/personal/stickies-notion-sync && python -m pytest tests/test_sync.py -v 2>&1 | head -20`
Expected: `ImportError: No module named 'sync_stickies'`

- [ ] **Step 3: 实现 read_stickies**

创建 `sync_stickies.py`，只包含该函数：

```python
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
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_sync.py::test_read_stickies_returns_list tests/test_sync.py::test_read_stickies_skips_empty tests/test_sync.py::test_read_stickies_handles_textutil_failure -v`
Expected: 3 PASSED（`test_read_stickies_sorted_by_mtime_desc` 在真实目录才有意义，有便签时应也通过）

- [ ] **Step 5: 提交**

Run: `git add sync_stickies.py tests/ && git commit -m "feat: add RTF reader with textutil"`

---

### Task 3: Hash 计算 + 状态管理

**Files:**
- Modify: `sync_stickies.py`（追加 compute_hash、load_state、save_state）
- Modify: `tests/test_sync.py`（追加测试）

- [ ] **Step 1: 写失败测试**

在 `tests/test_sync.py` 追加：

```python
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
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_sync.py -k "hash or state" -v 2>&1 | head -20`
Expected: ImportError 或 FAILED

- [ ] **Step 3: 实现三个函数**

在 `sync_stickies.py` 追加（`read_stickies` 函数后面）：

```python
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_sync.py -k "hash or state" -v`
Expected: 6 PASSED

- [ ] **Step 5: 提交**

Run: `git add sync_stickies.py tests/test_sync.py && git commit -m "feat: add hash computation and state management"`

---

### Task 4: Notion 客户端函数

**Files:**
- Modify: `sync_stickies.py`（追加 notion_* 函数）
- Modify: `tests/test_sync.py`（追加 mock 测试）

- [ ] **Step 1: 写失败测试**

在 `tests/test_sync.py` 追加：

```python
import requests
from unittest.mock import patch, MagicMock
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
        for b in blocks if b["type"] == "paragraph"
    ]
    assert "" not in para_texts
    assert len(para_texts) == 2

def test_notion_find_or_create_page_creates_when_not_found():
    """page_id 为 None 时调用 POST /pages"""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id": "new-page-id"}
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
    # 第一次返回 1 个 block + has_more=True，第二次返回空 + has_more=False
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

    with patch("requests.get", side_effect=[block_resp_1, block_resp_2]) as mock_get, \
         patch("requests.delete", return_value=del_resp) as mock_del:
        notion_clear_page("page-123")

    assert mock_get.call_count == 2
    assert mock_del.call_count == 1  # 只有第一批有 1 个 block
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_sync.py -k "notion or stickies_to_blocks" -v 2>&1 | head -30`
Expected: ImportError

- [ ] **Step 3: 实现 Notion 客户端函数**

在 `sync_stickies.py` 追加（状态管理函数后面），同时在文件顶部 imports 处追加 `import requests`：

```python
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
                "title": {
                    "title": [{"type": "text", "text": {"content": PAGE_TITLE}}]
                }
            },
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def notion_clear_page(page_id: str) -> None:
    """删除页面中所有现有 blocks。"""
    log.info("清空页面 blocks: %s", page_id)
    while True:
        resp = requests.get(
            f"{NOTION_API}/blocks/{page_id}/children",
            headers=notion_headers(),
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


def stickies_to_blocks(stickies: List[Tuple[str, float]]) -> list:
    """将便签列表转换为 Notion block 对象列表。"""
    blocks = []
    for i, (text, _) in enumerate(stickies):
        lines = [line for line in text.splitlines() if line.strip()]
        for line in lines:
            blocks.append({
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": line}}]
                },
            })
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_sync.py -k "notion or stickies_to_blocks" -v`
Expected: 7 PASSED

- [ ] **Step 5: 提交**

Run: `git add sync_stickies.py tests/test_sync.py && git commit -m "feat: add Notion client functions"`

---

### Task 5: main 函数 + 完整脚本

**Files:**
- Modify: `sync_stickies.py`（追加 main 函数）
- Modify: `tests/test_sync.py`（追加集成测试）

- [ ] **Step 1: 写失败测试**

在 `tests/test_sync.py` 追加：

```python
from sync_stickies import main

def test_main_no_change_skips_notion(tmp_path):
    """hash 未变化时不调用任何 Notion API"""
    stickies = [("hello", 1.0)]
    current_hash = compute_hash(stickies)
    state_file = str(tmp_path / "state.json")
    save_state(state_file, {"hash": current_hash, "notion_page_id": "existing-id"})

    with patch("sync_stickies.read_stickies", return_value=stickies), \
         patch("requests.post") as mock_post, \
         patch("requests.patch") as mock_patch, \
         patch("requests.get") as mock_get, \
         patch("requests.delete") as mock_del:
        main(state_file=state_file)

    mock_post.assert_not_called()
    mock_patch.assert_not_called()

def test_main_with_change_calls_notion(tmp_path):
    """hash 变化时调用 Notion 清空 + 写入"""
    stickies = [("new content", 1.0)]
    state_file = str(tmp_path / "state.json")
    save_state(state_file, {"hash": "old_hash", "notion_page_id": "existing-id"})

    mock_get_resp = MagicMock()
    mock_get_resp.status_code = 200
    mock_get_resp.json.return_value = {"results": [], "has_more": False}
    mock_get_resp.raise_for_status = MagicMock()

    mock_patch_resp = MagicMock()
    mock_patch_resp.status_code = 200
    mock_patch_resp.raise_for_status = MagicMock()

    with patch("sync_stickies.read_stickies", return_value=stickies), \
         patch("requests.get", return_value=mock_get_resp), \
         patch("requests.patch", return_value=mock_patch_resp):
        main(state_file=state_file)

    # state 应被更新
    new_state = load_state(state_file)
    assert new_state["hash"] == compute_hash(stickies)
    assert new_state["notion_page_id"] == "existing-id"

def test_main_api_failure_does_not_update_state(tmp_path):
    """Notion API 抛出异常时，state.json 不应被更新"""
    stickies = [("some content", 1.0)]
    old_hash = "old_hash"
    state_file = str(tmp_path / "state.json")
    save_state(state_file, {"hash": old_hash, "notion_page_id": "existing-id"})

    with patch("sync_stickies.read_stickies", return_value=stickies), \
         patch("requests.get", side_effect=Exception("API unreachable")):
        try:
            main(state_file=state_file)
        except Exception:
            pass

    # state 必须保持不变
    state_after = load_state(state_file)
    assert state_after["hash"] == old_hash
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_sync.py -k "main" -v 2>&1 | head -20`
Expected: ImportError

- [ ] **Step 3: 实现 main 函数**

在 `sync_stickies.py` 末尾追加：

```python
# ── 主流程 ────────────────────────────────────────────────────────────────────
def main(
    stickies_dir: str = STICKIES_DIR,
    state_file: str = STATE_FILE,
) -> None:
    """主同步流程。"""
    # 1. 读取便签
    stickies = read_stickies(stickies_dir)
    if not stickies:
        log.info("没有便签，退出")
        return

    # 2. 计算 hash，与上次对比
    current_hash = compute_hash(stickies)
    state = load_state(state_file)

    if state["hash"] == current_hash:
        log.info("内容无变化，跳过同步")
        return

    log.info("检测到内容变化，开始同步到 Notion")

    # 3. 同步到 Notion
    page_id = notion_find_or_create_page(state.get("notion_page_id"))
    notion_clear_page(page_id)
    notion_write_stickies(page_id, stickies)

    # 4. 更新状态
    save_state(state_file, {"hash": current_hash, "notion_page_id": page_id})
    log.info("同步完成")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.exception("同步失败: %s", e)
        sys.exit(1)
```

- [ ] **Step 4: 运行全部测试**

Run: `python -m pytest tests/ -v`
Expected: 所有测试 PASSED

- [ ] **Step 5: 手动运行脚本验证一次**

Run: `python /Users/yuanyunfan/Code/personal/stickies-notion-sync/sync_stickies.py`
Expected: 日志输出 "同步完成" 或 "内容无变化"

- [ ] **Step 6: 提交**

Run: `git add sync_stickies.py tests/test_sync.py && git commit -m "feat: add main orchestration function"`

---

## Chunk 2: LaunchAgent + 安装

### Task 6: LaunchAgent plist

**Files:**
- Create: `com.user.stickies-sync.plist`

- [ ] **Step 1: 创建 plist 文件**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.stickies-sync</string>

    <key>ProgramArguments</key>
    <array>
        <string>/opt/miniconda3/bin/python3</string>
        <string>/Users/yuanyunfan/Code/personal/stickies-notion-sync/sync_stickies.py</string>
    </array>

    <key>EnvironmentVariables</key>
    <dict>
        <key>NOTION_TOKEN</key>
        <string>NOTION_TOKEN_REDACTED</string>
    </dict>

    <key>StartInterval</key>
    <integer>600</integer>

    <key>RunAtLoad</key>
    <true/>

    <key>StandardOutPath</key>
    <string>/Users/yuanyunfan/.local/share/stickies-sync/sync.log</string>

    <key>StandardErrorPath</key>
    <string>/Users/yuanyunfan/.local/share/stickies-sync/sync_error.log</string>
</dict>
</plist>
```

- [ ] **Step 2: 验证 plist 格式**

Run: `plutil -lint /Users/yuanyunfan/Code/personal/stickies-notion-sync/com.user.stickies-sync.plist`
Expected: `com.user.stickies-sync.plist: OK`

- [ ] **Step 3: 提交**

Run: `git add com.user.stickies-sync.plist && git commit -m "feat: add LaunchAgent plist"`

---

### Task 7: 安装脚本

**Files:**
- Create: `install.sh`

- [ ] **Step 1: 创建 install.sh**

```bash
#!/bin/bash
set -e

PLIST_SRC="$(cd "$(dirname "$0")" && pwd)/com.user.stickies-sync.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.user.stickies-sync.plist"
STATE_DIR="$HOME/.local/share/stickies-sync"
LABEL="com.user.stickies-sync"

echo "==> 创建状态目录"
mkdir -p "$STATE_DIR"

echo "==> 安装 LaunchAgent"
cp "$PLIST_SRC" "$PLIST_DST"

echo "==> 卸载旧任务（若存在）"
launchctl unload "$PLIST_DST" 2>/dev/null || true

echo "==> 加载 LaunchAgent"
launchctl load "$PLIST_DST"

echo "==> 验证任务已加载"
launchctl list | grep "$LABEL" && echo "✓ 安装成功" || echo "✗ 安装失败，请检查日志"
```

- [ ] **Step 2: 赋予执行权限**

Run: `chmod +x /Users/yuanyunfan/Code/personal/stickies-notion-sync/install.sh`

- [ ] **Step 3: 运行安装**

Run: `cd /Users/yuanyunfan/Code/personal/stickies-notion-sync && ./install.sh`
Expected: 输出 "✓ 安装成功" 和任务状态行

- [ ] **Step 4: 确认日志有输出**

Run: `sleep 10 && cat ~/.local/share/stickies-sync/sync.log`
Expected: 看到 "同步完成" 或 "内容无变化" 的日志

- [ ] **Step 5: 最终提交**

Run: `git add install.sh && git commit -m "feat: add install script and complete setup"`

---

## 验收标准

- [ ] `python -m pytest tests/ -v` 全部通过
- [ ] `launchctl list | grep stickies-sync` 显示任务已加载
- [ ] `~/.local/share/stickies-sync/sync.log` 有成功同步日志
- [ ] Notion workspace 中出现 "Mac stickies" 页面，内容与 Mac Stickies 一致
- [ ] 修改一条便签，等待 10 分钟，Notion 页面内容自动更新
