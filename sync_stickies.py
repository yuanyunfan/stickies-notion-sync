#!/usr/bin/env python3
"""Mac Stickies → Notion 同步脚本"""

import colorsys
import hashlib
import json
import logging
import os
import re
import requests
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import List, Optional, Tuple, TypedDict

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


# ── 富文本数据模型 ──────────────────────────────────────────────────────────────
class Run(TypedDict):
    text: str
    bold: bool
    color: str  # Notion color name, e.g. "default", "red", "blue"


# ── 颜色转换 ──────────────────────────────────────────────────────────────────
def hex_to_notion_color(hex_color: str) -> str:
    """将 #RRGGBB 十六进制颜色转换为 Notion 颜色名称。"""
    hex_color = hex_color.strip()
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", hex_color):
        return "default"

    r = int(hex_color[1:3], 16) / 255.0
    g = int(hex_color[3:5], 16) / 255.0
    b = int(hex_color[5:7], 16) / 255.0

    h, s, _ = colorsys.rgb_to_hsv(r, g, b)
    hue = h * 360  # 0–360

    if s < 0.2:
        return "default"

    if hue <= 20 or hue > 340:
        return "red"
    elif hue <= 45:
        return "orange"
    elif hue <= 70:
        return "yellow"
    elif hue <= 165:
        return "green"
    elif hue <= 260:
        return "blue"
    elif hue <= 290:
        return "purple"
    else:
        return "pink"


# ── HTML 解析 ─────────────────────────────────────────────────────────────────
def _parse_css_classes(style_text: str) -> dict:
    """
    解析 <style> 块，返回 {class_name: {"bold": bool, "color": notion_color}} 映射。
    例如：{"p1": {"bold": True, "color": "default"}, "s1": {"bold": False, "color": "red"}}
    """
    classes = {}
    # 匹配 .classname { ... } 或 p.classname { ... } 或 span.classname { ... }
    for block_match in re.finditer(r"[\w.]*\.(\w+)\s*\{([^}]+)\}", style_text):
        class_name = block_match.group(1)
        body = block_match.group(2)

        # 检测 bold：font-family 包含 Semibold 或 Bold
        font_match = re.search(r"font:[^;']*'([^']+)'", body)
        is_bold = False
        if font_match:
            family = font_match.group(1)
            is_bold = "Semibold" in family or "Bold" in family

        # 检测颜色（排除 background-color）
        color_match = re.search(r"(?<!-)color:\s*(#[0-9a-fA-F]{6})", body)
        notion_color = "default"
        if color_match:
            notion_color = hex_to_notion_color(color_match.group(1))

        classes[class_name] = {"bold": is_bold, "color": notion_color}

    return classes


class _StickyHTMLParser(HTMLParser):
    """解析 textutil 生成的 HTML，提取段落和行内格式。"""

    def __init__(self, css_classes: dict):
        super().__init__()
        self._css = css_classes

        self._in_body = False
        self._in_para = False

        # 段落级属性（由 <p class="..."> 决定）
        self._para_bold = False
        self._para_color = "default"

        # 行内状态
        self._bold_depth = 0
        self._span_color_stack: List[Optional[str]] = []

        # 当前段落累积的 runs
        self._current_runs: List[Run] = []
        self._para_has_content = False

        # 所有段落结果
        self.paragraphs: List[List[Run]] = []

    def _eff_bold(self) -> bool:
        return self._bold_depth > 0 or self._para_bold

    def _eff_color(self) -> str:
        # 取 span_color_stack 中最顶端的非 None 值，否则用段落颜色
        for color in reversed(self._span_color_stack):
            if color is not None:
                return color
        return self._para_color

    def handle_starttag(self, tag: str, attrs):
        attrs_dict = dict(attrs)

        if tag == "body":
            self._in_body = True
            return

        if not self._in_body:
            return

        if tag == "p":
            self._in_para = True
            self._current_runs = []
            self._para_has_content = False
            self._bold_depth = 0
            self._span_color_stack = []

            cls = attrs_dict.get("class", "")
            style = self._css.get(cls, {})
            self._para_bold = style.get("bold", False)
            self._para_color = style.get("color", "default")

        elif tag == "b" and self._in_para:
            self._bold_depth += 1

        elif tag == "span" and self._in_para:
            cls = attrs_dict.get("class", "")
            style = self._css.get(cls, {})
            # 只有 span 有明确颜色样式时才推入颜色，否则推入 None（继承段落颜色）
            color = style.get("color") if cls and cls in self._css else None
            self._span_color_stack.append(color)

    def handle_endtag(self, tag: str):
        if not self._in_body:
            return

        if tag == "p":
            if self._para_has_content:
                self.paragraphs.append(self._current_runs)
            else:
                self.paragraphs.append([])  # 空段落 → 空 rich_text
            self._in_para = False
            self._current_runs = []

        elif tag == "b" and self._in_para:
            self._bold_depth = max(0, self._bold_depth - 1)

        elif tag == "span" and self._in_para:
            if self._span_color_stack:
                self._span_color_stack.pop()

    def handle_data(self, data: str):
        if not (self._in_body and self._in_para):
            return
        if data:
            self._para_has_content = True
            self._current_runs.append(
                Run(text=data, bold=self._eff_bold(), color=self._eff_color())
            )


def parse_html_to_paragraphs(html: str) -> List[List[Run]]:
    """将 textutil -convert html 输出的 HTML 解析为段落列表。"""
    # 提取 <style> 块
    style_match = re.search(
        r"<style[^>]*>(.*?)</style>", html, re.DOTALL | re.IGNORECASE
    )
    style_text = style_match.group(1) if style_match else ""
    css_classes = _parse_css_classes(style_text)

    parser = _StickyHTMLParser(css_classes)
    parser.feed(html)
    return parser.paragraphs


# ── RTF 读取 ──────────────────────────────────────────────────────────────────
def read_stickies(stickies_dir: str) -> List[Tuple[List[List[Run]], float]]:
    """
    读取所有 Stickies 便签，返回 [(paragraphs, mtime), ...] 按 mtime 倒序。
    paragraphs 为 List[List[Run]]，每个内层列表代表一个段落。
    空内容或解析失败的便签会被跳过。
    """
    stickies_path = Path(stickies_dir)
    if not stickies_path.exists():
        log.warning("Stickies 目录不存在: %s", stickies_dir)
        return []

    results: List[Tuple[List[List[Run]], float]] = []

    for bundle in stickies_path.glob("*.rtfd"):
        rtf_file = bundle / "TXT.rtf"
        if not rtf_file.exists():
            log.warning("跳过无 TXT.rtf 的 bundle: %s", bundle)
            continue

        proc = subprocess.run(
            ["textutil", "-convert", "html", "-stdout", str(rtf_file)],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if proc.returncode != 0:
            log.warning("textutil 解析失败: %s", bundle.name)
            continue

        paragraphs = parse_html_to_paragraphs(proc.stdout)

        # 检查是否有实质性内容（至少一个 run 含非空文本）
        has_content = any(any(r["text"].strip() for r in para) for para in paragraphs)
        if not has_content:
            log.debug("跳过空便签: %s", bundle.name)
            continue

        mtime = rtf_file.stat().st_mtime
        results.append((paragraphs, mtime))

    results.sort(key=lambda x: x[1], reverse=True)
    return results


# ── Hash + 状态 ───────────────────────────────────────────────────────────────
def compute_hash(stickies: List[Tuple[List[List[Run]], float]]) -> str:
    """根据便签段落内容（忽略 mtime）计算 MD5 hash。bold/color 变化也会触发重算。"""
    combined = json.dumps(
        [paragraphs for paragraphs, _ in stickies],
        ensure_ascii=False,
        sort_keys=True,
    )
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
    若 page_id 已有则直接返回；否则先搜索 "Mac stickies" 页面，
    找到则复用，找不到才创建（需要 integration 有对应 parent 访问权限）。
    """
    if page_id:
        return page_id

    # 先搜索已有页面
    log.info("搜索已有 Notion 页面: %s", PAGE_TITLE)
    search_resp = requests.post(
        f"{NOTION_API}/search",
        headers=notion_headers(),
        json={"query": PAGE_TITLE, "filter": {"value": "page", "property": "object"}},
        timeout=30,
    )
    search_resp.raise_for_status()
    results = search_resp.json().get("results", [])
    for page in results:
        props = page.get("properties", {})
        title_parts = props.get("title", {}).get("title", [])
        title_text = "".join(p.get("plain_text", "") for p in title_parts)
        if title_text.strip() == PAGE_TITLE:
            found_id = page["id"]
            log.info("找到已有页面: %s", found_id)
            return found_id

    log.info("未找到已有页面，创建新页面: %s", PAGE_TITLE)
    resp = requests.post(
        f"{NOTION_API}/pages",
        headers=notion_headers(),
        json={
            "parent": {"type": "workspace", "workspace": True},
            "properties": {
                "title": {"title": [{"type": "text", "text": {"content": PAGE_TITLE}}]}
            },
        },
        timeout=30,
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
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        for block in data.get("results", []):
            if block.get("archived") or block.get("in_trash"):
                continue
            del_resp = requests.delete(
                f"{NOTION_API}/blocks/{block['id']}",
                headers=notion_headers(),
                timeout=30,
            )
            del_resp.raise_for_status()

        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")


def stickies_to_blocks(stickies: List[Tuple[List[List[Run]], float]]) -> list:
    """将便签列表转换为 Notion block 对象列表。"""
    blocks = []
    for i, (paragraphs, _) in enumerate(stickies):
        for para in paragraphs:
            # 过滤掉空文本的 run
            content_runs = [r for r in para if r["text"]]
            rich_text = [
                {
                    "type": "text",
                    "text": {"content": r["text"]},
                    "annotations": {
                        "bold": r["bold"],
                        "color": r["color"],
                    },
                }
                for r in content_runs
            ]
            blocks.append(
                {
                    "type": "paragraph",
                    "paragraph": {"rich_text": rich_text},
                }
            )
        if i < len(stickies) - 1:
            blocks.append({"type": "divider", "divider": {}})
    return blocks


def notion_write_stickies(
    page_id: str, stickies: List[Tuple[List[List[Run]], float]]
) -> None:
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
            timeout=30,
        )
        resp.raise_for_status()


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
