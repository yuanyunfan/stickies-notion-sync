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

    重要：所有对 ~/Library/Containers/ 的访问均通过 subprocess 系统工具完成，
    避免 Python 直接触碰受 TCC "Other App Data" 保护的路径，从而消除
    LaunchAgent 运行时反复弹出的权限提示窗。
    """
    # 用 find 列出所有 TXT.rtf 文件，避免 Python 直接访问受保护目录
    find_proc = subprocess.run(
        ["find", stickies_dir, "-name", "TXT.rtf", "-type", "f"],
        capture_output=True,
        text=True,
    )

    if find_proc.returncode != 0:
        stderr = find_proc.stderr.strip()
        if "No such file or directory" in stderr:
            log.warning("Stickies 目录不存在: %s", stickies_dir)
        elif "Permission denied" in stderr or "Operation not permitted" in stderr:
            log.error("无权限访问 Stickies 目录: %s。stderr: %s", stickies_dir, stderr)
        else:
            log.error("find 命令失败 (returncode=%d): %s", find_proc.returncode, stderr)
        return []

    rtf_files = [p for p in find_proc.stdout.splitlines() if p.strip()]
    if not rtf_files:
        log.warning("Stickies 目录中未找到任何 TXT.rtf 文件: %s", stickies_dir)
        return []

    results: List[Tuple[List[List[Run]], float]] = []

    for rtf_path in rtf_files:
        bundle_name = Path(rtf_path).parent.name

        # 用 textutil 将 RTF 转为 HTML（已是 subprocess，保持不变）
        proc = subprocess.run(
            ["textutil", "-convert", "html", "-stdout", rtf_path],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if proc.returncode != 0:
            log.warning("textutil 解析失败: %s", bundle_name)
            continue

        paragraphs = parse_html_to_paragraphs(proc.stdout)

        # 检查是否有实质性内容（至少一个 run 含非空文本）
        has_content = any(any(r["text"].strip() for r in para) for para in paragraphs)
        if not has_content:
            log.debug("跳过空便签: %s", bundle_name)
            continue

        # 用 stat 命令获取 mtime，避免 Python 直接调用 os.stat()
        stat_proc = subprocess.run(
            ["stat", "-f", "%m", rtf_path],
            capture_output=True,
            text=True,
        )

        if stat_proc.returncode != 0:
            log.warning("stat 命令失败，跳过: %s", bundle_name)
            continue

        try:
            mtime = float(stat_proc.stdout.strip())
        except ValueError:
            log.warning("stat 输出无法解析为 float，跳过: %s", stat_proc.stdout.strip())
            continue

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
        return json.load(open(state_file, "r", encoding="utf-8"))
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


def notion_create_page() -> str:
    """在 workspace 根级别创建新的空白页面，返回 page_id。"""
    log.info("创建新页面: %s", PAGE_TITLE)
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


def notion_archive_page(page_id: str) -> None:
    """归档（软删除）指定页面，只需一次 PATCH 请求。"""
    log.info("归档页面: %s", page_id)
    resp = requests.patch(
        f"{NOTION_API}/pages/{page_id}",
        headers=notion_headers(),
        json={"archived": True},
        timeout=30,
    )
    resp.raise_for_status()


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
    """主同步流程（Shadow Page 方案）。"""
    # 1. 读取便签
    stickies = read_stickies(stickies_dir)
    if not stickies:
        log.info("没有便签，退出")
        return

    # 2. 计算 hash，加载 state
    current_hash = compute_hash(stickies)
    state = load_state(state_file)

    # 3. 启动清理：若 pending_page_id 存在 → 上次写入中途中断，归档残留影子页
    if state.get("pending_page_id"):
        log.warning(
            "检测到未完成的影子页面 %s，归档并重新同步", state["pending_page_id"]
        )
        try:
            notion_archive_page(state["pending_page_id"])
        except Exception as e:
            log.warning("归档 pending_page_id 失败（忽略）: %s", e)
        state = {k: v for k, v in state.items() if k != "pending_page_id"}
        save_state(state_file, state)

    # 4. 启动清理：若 old_page_id 存在 → 上次归档未完成，补做归档
    if state.get("old_page_id"):
        log.info("清理上次遗留的旧页面 %s", state["old_page_id"])
        try:
            notion_archive_page(state["old_page_id"])
        except Exception as e:
            log.warning("归档 old_page_id 失败（忽略）: %s", e)
        state = {k: v for k, v in state.items() if k != "old_page_id"}
        save_state(state_file, state)

    # 5. hash 相同 → 跳过
    if state["hash"] == current_hash:
        log.info("内容无变化，跳过同步")
        return

    log.info("检测到内容变化，开始同步到 Notion")

    # 6. 创建新的空白影子页
    shadow_id = notion_create_page()

    # 7. 记录影子页 ID（中断后启动可归档）
    old_id = state.get("notion_page_id")
    save_state(state_file, {**state, "pending_page_id": shadow_id})

    # 8. 写入全部内容到影子页（旧页面此时完好）
    notion_write_stickies(shadow_id, stickies)

    # 9. 原子切换：state 指向新页面，记录旧页待归档
    state = {"hash": current_hash, "notion_page_id": shadow_id}
    if old_id:
        state["old_page_id"] = old_id
    save_state(state_file, state)

    # 10. 归档旧页
    if old_id:
        try:
            notion_archive_page(old_id)
        except Exception as e:
            log.warning("归档旧页面失败（下次启动时重试）: %s", e)

    # 11. 清理 old_page_id
    save_state(state_file, {"hash": current_hash, "notion_page_id": shadow_id})
    log.info("同步完成")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.exception("同步失败: %s", e)
        sys.exit(1)
