import os
import tempfile
import subprocess
import pytest
import requests
from unittest.mock import patch, MagicMock
from sync_stickies import (
    Run,
    read_stickies,
    compute_hash,
    load_state,
    save_state,
    notion_headers,
    notion_create_page,
    notion_archive_page,
    notion_write_stickies,
    stickies_to_blocks,
    main,
    hex_to_notion_color,
    parse_html_to_paragraphs,
)

STICKIES_DIR = os.path.expanduser(
    "~/Library/Containers/com.apple.Stickies/Data/Library/Stickies"
)

# ── 测试辅助函数 ───────────────────────────────────────────────────────────────


def _run(text: str, bold: bool = False, color: str = "default") -> Run:
    return Run(text=text, bold=bold, color=color)


def _simple_sticky(text: str, mtime: float = 1.0):
    """构造一个单段落便签（格式与 read_stickies 返回值一致）"""
    return ([[_run(text)]], mtime)


# ── hex_to_notion_color ────────────────────────────────────────────────────────


def test_hex_to_notion_color_red():
    assert hex_to_notion_color("#ff0000") == "red"


def test_hex_to_notion_color_blue():
    assert hex_to_notion_color("#0000ff") == "blue"


def test_hex_to_notion_color_low_saturation():
    """低饱和度（灰色）→ default"""
    assert hex_to_notion_color("#808080") == "default"


def test_hex_to_notion_color_black():
    assert hex_to_notion_color("#000000") == "default"


def test_hex_to_notion_color_invalid():
    assert hex_to_notion_color("not-a-color") == "default"


def test_hex_to_notion_color_orange():
    assert hex_to_notion_color("#ff8000") == "orange"


def test_hex_to_notion_color_green():
    assert hex_to_notion_color("#00cc00") == "green"


# ── parse_html_to_paragraphs ───────────────────────────────────────────────────


def test_parse_html_bold():
    """<b> 标签 → bold=True"""
    html = "<html><body><p class='p1'><b>Bold text</b></p></body></html>"
    paragraphs = parse_html_to_paragraphs(html)
    assert len(paragraphs) == 1
    assert paragraphs[0][0]["bold"] is True
    assert paragraphs[0][0]["text"] == "Bold text"


def test_parse_html_paragraph_color():
    """段落 CSS class 中有颜色 → run 的 color 正确"""
    html = """<html><head><style>
p.p1 {color: #ff0000}
</style></head><body><p class="p1">Red text</p></body></html>"""
    paragraphs = parse_html_to_paragraphs(html)
    assert len(paragraphs) == 1
    assert paragraphs[0][0]["color"] == "red"


def test_parse_html_inline_span_color():
    """span 的颜色覆盖段落颜色"""
    html = """<html><head><style>
p.p1 {color: #000000}
span.s1 {color: #ff0000}
</style></head><body><p class="p1"><span class="s1">Inline red</span></p></body></html>"""
    paragraphs = parse_html_to_paragraphs(html)
    assert len(paragraphs) == 1
    assert paragraphs[0][0]["color"] == "red"


def test_parse_html_mixed_bold_and_color():
    """span 颜色 + b 标签 → bold=True, color=red"""
    html = """<html><head><style>
p.p1 {color: #000000}
span.s1 {color: #ff0000}
</style></head><body><p class="p1"><span class="s1"><b>Bold red</b></span></p></body></html>"""
    paragraphs = parse_html_to_paragraphs(html)
    assert len(paragraphs) == 1
    run = paragraphs[0][0]
    assert run["bold"] is True
    assert run["color"] == "red"


def test_parse_html_empty_paragraph():
    """空 <p></p> → 空列表"""
    html = "<html><body><p></p></body></html>"
    paragraphs = parse_html_to_paragraphs(html)
    assert len(paragraphs) == 1
    assert paragraphs[0] == []


def test_parse_html_paragraph_bold_via_class():
    """段落 CSS class 含 Semibold → bold=True"""
    html = """<html><head><style>
p.p1 {font: 12.0px 'PingFang SC Semibold'; color: #000000}
</style></head><body><p class="p1">Bold para</p></body></html>"""
    paragraphs = parse_html_to_paragraphs(html)
    assert len(paragraphs) == 1
    assert paragraphs[0][0]["bold"] is True


def test_parse_html_multiple_paragraphs():
    """多个段落各自独立"""
    html = "<html><body><p>First</p><p>Second</p></body></html>"
    paragraphs = parse_html_to_paragraphs(html)
    assert len(paragraphs) == 2
    assert paragraphs[0][0]["text"] == "First"
    assert paragraphs[1][0]["text"] == "Second"


# ── read_stickies ─────────────────────────────────────────────────────────────


def test_read_stickies_returns_list():
    """read_stickies 应返回列表，每项为 (paragraphs: list, mtime: float)"""
    result = read_stickies(STICKIES_DIR)
    assert isinstance(result, list)
    for paragraphs, mtime in result:
        assert isinstance(paragraphs, list)
        assert isinstance(mtime, float)


def test_read_stickies_sorted_by_mtime_desc():
    """结果应按 mtime 倒序（最近修改在前）"""
    with tempfile.TemporaryDirectory() as tmpdir:
        bundle1 = os.path.join(tmpdir, "old.rtfd")
        bundle2 = os.path.join(tmpdir, "new.rtfd")
        os.makedirs(bundle1)
        os.makedirs(bundle2)
        open(os.path.join(bundle1, "TXT.rtf"), "w").close()
        open(os.path.join(bundle2, "TXT.rtf"), "w").close()

        old_mtime = 1000.0
        new_mtime = 2000.0

        # find 命令的真实输出（两个 TXT.rtf 路径）
        old_rtf = os.path.join(bundle1, "TXT.rtf")
        new_rtf = os.path.join(bundle2, "TXT.rtf")
        find_output = f"{old_rtf}\n{new_rtf}\n"

        def fake_run(cmd, **kwargs):
            # find 命令
            if cmd[0] == "find":
                return MagicMock(returncode=0, stdout=find_output, stderr="")
            # textutil 命令
            if cmd[0] == "textutil":
                path = cmd[-1]
                if "old.rtfd" in path:
                    return MagicMock(
                        returncode=0,
                        stdout="<html><body><p>old sticky</p></body></html>",
                    )
                else:
                    return MagicMock(
                        returncode=0,
                        stdout="<html><body><p>new sticky</p></body></html>",
                    )
            # stat 命令
            if cmd[0] == "stat":
                path = cmd[-1]
                if "old.rtfd" in path:
                    return MagicMock(returncode=0, stdout=f"{int(old_mtime)}\n")
                else:
                    return MagicMock(returncode=0, stdout=f"{int(new_mtime)}\n")
            return MagicMock(returncode=1, stdout="", stderr="unknown command")

        with patch("sync_stickies.subprocess.run", side_effect=fake_run):
            result = read_stickies(tmpdir)

        assert len(result) == 2
        assert result[0][1] == new_mtime
        assert result[1][1] == old_mtime


def test_read_stickies_skips_empty():
    """全空内容的便签应被跳过"""
    with tempfile.TemporaryDirectory() as tmpdir:
        bundle = os.path.join(tmpdir, "empty.rtfd")
        os.makedirs(bundle)
        rtf_path = os.path.join(bundle, "TXT.rtf")
        open(rtf_path, "w").close()

        find_output = f"{rtf_path}\n"

        def fake_run(cmd, **kwargs):
            if cmd[0] == "find":
                return MagicMock(returncode=0, stdout=find_output, stderr="")
            if cmd[0] == "textutil":
                return MagicMock(
                    returncode=0, stdout="<html><body><p>   </p></body></html>"
                )
            return MagicMock(returncode=1, stdout="", stderr="")

        with patch("sync_stickies.subprocess.run", side_effect=fake_run):
            result = read_stickies(tmpdir)
        assert result == []


def test_read_stickies_handles_textutil_failure():
    """textutil 失败时跳过该便签，不抛异常"""
    with tempfile.TemporaryDirectory() as tmpdir:
        bundle = os.path.join(tmpdir, "bad.rtfd")
        os.makedirs(bundle)
        rtf_path = os.path.join(bundle, "TXT.rtf")
        open(rtf_path, "w").close()

        find_output = f"{rtf_path}\n"

        def fake_run(cmd, **kwargs):
            if cmd[0] == "find":
                return MagicMock(returncode=0, stdout=find_output, stderr="")
            if cmd[0] == "textutil":
                return MagicMock(returncode=1, stdout="")
            return MagicMock(returncode=1, stdout="", stderr="")

        with patch("sync_stickies.subprocess.run", side_effect=fake_run):
            result = read_stickies(tmpdir)
        assert result == []


# ── compute_hash ──────────────────────────────────────────────────────────────


def test_compute_hash_deterministic():
    """相同输入，hash 相同"""
    stickies = [
        _simple_sticky("hello world", 1.0),
        _simple_sticky("second sticky", 2.0),
    ]
    h1 = compute_hash(stickies)
    h2 = compute_hash(stickies)
    assert h1 == h2
    assert len(h1) == 32  # MD5 hex


def test_compute_hash_sensitive_to_content():
    """内容不同，hash 不同"""
    h1 = compute_hash([_simple_sticky("aaa")])
    h2 = compute_hash([_simple_sticky("bbb")])
    assert h1 != h2


def test_compute_hash_ignores_mtime():
    """mtime 不影响 hash"""
    h1 = compute_hash([_simple_sticky("same text", 1.0)])
    h2 = compute_hash([_simple_sticky("same text", 999.0)])
    assert h1 == h2


def test_compute_hash_sensitive_to_bold():
    """bold 不同 → hash 不同"""
    s1 = ([[_run("hello", bold=False)]], 1.0)
    s2 = ([[_run("hello", bold=True)]], 1.0)
    assert compute_hash([s1]) != compute_hash([s2])


def test_compute_hash_sensitive_to_color():
    """color 不同 → hash 不同"""
    s1 = ([[_run("hello", color="default")]], 1.0)
    s2 = ([[_run("hello", color="red")]], 1.0)
    assert compute_hash([s1]) != compute_hash([s2])


# ── load_state / save_state ───────────────────────────────────────────────────


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


# ── notion_headers ────────────────────────────────────────────────────────────


def test_notion_headers_contain_auth():
    headers = notion_headers()
    assert "Authorization" in headers
    assert headers["Authorization"].startswith("Bearer ")
    assert headers["Notion-Version"] == "2022-06-28"


# ── stickies_to_blocks ────────────────────────────────────────────────────────


def test_stickies_to_blocks_single():
    """单条便签 → paragraph blocks，无 divider"""
    stickies = [([[_run("line1")], [_run("line2")]], 1.0)]
    blocks = stickies_to_blocks(stickies)
    types = [b["type"] for b in blocks]
    assert "paragraph" in types
    assert "divider" not in types


def test_stickies_to_blocks_multiple_has_dividers():
    """多条便签 → 便签间有 divider"""
    stickies = [_simple_sticky("a"), _simple_sticky("b")]
    blocks = stickies_to_blocks(stickies)
    types = [b["type"] for b in blocks]
    assert "divider" in types
    assert types[-1] == "paragraph"


def test_stickies_to_blocks_empty_paragraphs_become_empty_blocks():
    """空段落（来自空 <p>）→ rich_text=[] 的 paragraph block"""
    # 一个便签含两个实质段落和一个空段落
    paragraphs = [[_run("line1")], [], [_run("line2")]]
    stickies = [(paragraphs, 1.0)]
    blocks = stickies_to_blocks(stickies)
    para_blocks = [b for b in blocks if b["type"] == "paragraph"]
    assert len(para_blocks) == 3
    # 第二个（空段落）的 rich_text 为空列表
    assert para_blocks[1]["paragraph"]["rich_text"] == []


def test_stickies_to_blocks_annotations():
    """rich_text 应包含正确的 annotations（bold, color）"""
    stickies = [([[_run("hello", bold=True, color="red")]], 1.0)]
    blocks = stickies_to_blocks(stickies)
    para = blocks[0]["paragraph"]
    rt = para["rich_text"][0]
    assert rt["annotations"]["bold"] is True
    assert rt["annotations"]["color"] == "red"


# ── notion_create_page ────────────────────────────────────────────────────────


def test_notion_create_page_calls_post():
    """notion_create_page 应调用 POST /pages 并返回新页面 ID"""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"id": "new-page-id"}

    with patch("requests.post", return_value=mock_resp) as mock_post:
        page_id = notion_create_page()

    assert page_id == "new-page-id"
    mock_post.assert_called_once()
    call_url = mock_post.call_args[0][0]
    assert call_url.endswith("/pages")


# ── notion_archive_page ───────────────────────────────────────────────────────


def test_notion_archive_page_calls_patch_with_archived_true():
    """notion_archive_page 应调用 PATCH /pages/{id} 并携带 archived=True"""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()

    with patch("requests.patch", return_value=mock_resp) as mock_patch:
        notion_archive_page("page-to-archive")

    mock_patch.assert_called_once()
    call_url = mock_patch.call_args[0][0]
    assert "page-to-archive" in call_url
    call_body = mock_patch.call_args[1]["json"]
    assert call_body == {"archived": True}


# ── notion_write_stickies ─────────────────────────────────────────────────────


def test_notion_write_stickies_calls_patch():
    """notion_write_stickies 应调用 PATCH /blocks/{id}/children"""
    stickies = [_simple_sticky("hello")]
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    with patch("requests.patch", return_value=mock_resp) as mock_patch:
        notion_write_stickies("page-123", stickies)
    mock_patch.assert_called_once()
    call_kwargs = mock_patch.call_args
    assert "page-123" in call_kwargs[0][0]


def test_notion_write_stickies_batches_large_input():
    """超过 100 个 blocks 时应分多次调用 PATCH"""
    # 60 条便签 × 1 paragraph + 59 dividers = 119 blocks → 需要 2 次 PATCH
    stickies = [_simple_sticky(f"sticky {i}", float(i)) for i in range(60)]
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    with patch("requests.patch", return_value=mock_resp) as mock_patch:
        notion_write_stickies("page-456", stickies)
    assert mock_patch.call_count == 2


# ── main ──────────────────────────────────────────────────────────────────────


def test_main_no_change_skips_notion(tmp_path):
    """hash 未变化时不调用任何 Notion API"""
    stickies = [_simple_sticky("hello")]
    current_hash = compute_hash(stickies)
    state_file = str(tmp_path / "state.json")
    save_state(
        state_file,
        {"hash": current_hash, "notion_page_id": "existing-id"},
    )

    with (
        patch("sync_stickies.read_stickies", return_value=stickies),
        patch("requests.post") as mock_post,
        patch("requests.patch") as mock_patch,
        patch("requests.get") as mock_get,
        patch("requests.delete") as mock_del,
    ):
        main(state_file=state_file)

    mock_post.assert_not_called()
    mock_patch.assert_not_called()


def test_main_with_change_calls_notion(tmp_path):
    """hash 变化时：创建新页、写入内容、归档旧页，state 更新为新页面 ID"""
    stickies = [_simple_sticky("new content")]
    state_file = str(tmp_path / "state.json")
    save_state(
        state_file,
        {"hash": "old_hash", "notion_page_id": "old-page-id"},
    )

    mock_post_resp = MagicMock()
    mock_post_resp.raise_for_status = MagicMock()
    mock_post_resp.json.return_value = {"id": "shadow-page-id"}

    mock_patch_resp = MagicMock()
    mock_patch_resp.raise_for_status = MagicMock()

    with (
        patch("sync_stickies.read_stickies", return_value=stickies),
        patch("requests.post", return_value=mock_post_resp),
        patch("requests.patch", return_value=mock_patch_resp),
    ):
        main(state_file=state_file)

    new_state = load_state(state_file)
    assert new_state["hash"] == compute_hash(stickies)
    assert new_state["notion_page_id"] == "shadow-page-id"
    assert "write_complete" not in new_state
    assert "pending_page_id" not in new_state
    assert "old_page_id" not in new_state


def test_main_api_failure_does_not_update_state(tmp_path):
    """Notion API 抛出异常（创建页面失败）时，state.json 不应被更新"""
    stickies = [_simple_sticky("some content")]
    old_hash = "old_hash"
    state_file = str(tmp_path / "state.json")
    save_state(
        state_file,
        {"hash": old_hash, "notion_page_id": "existing-id"},
    )

    with (
        patch("sync_stickies.read_stickies", return_value=stickies),
        patch("requests.post", side_effect=Exception("API unreachable")),
    ):
        try:
            main(state_file=state_file)
        except Exception:
            pass

    state_after = load_state(state_file)
    assert state_after["hash"] == old_hash


def test_main_recovers_from_interrupted_write(tmp_path):
    """state 含 pending_page_id 时，启动应归档残留影子页并重新同步"""
    stickies = [_simple_sticky("same content")]
    current_hash = compute_hash(stickies)
    state_file = str(tmp_path / "state.json")
    # 模拟上次写入中途中断：pending_page_id 存在
    save_state(
        state_file,
        {
            "hash": "old_hash",
            "notion_page_id": "active-page-id",
            "pending_page_id": "broken-shadow-id",
        },
    )

    mock_post_resp = MagicMock()
    mock_post_resp.raise_for_status = MagicMock()
    mock_post_resp.json.return_value = {"id": "new-shadow-id"}

    mock_patch_resp = MagicMock()
    mock_patch_resp.raise_for_status = MagicMock()

    with (
        patch("sync_stickies.read_stickies", return_value=stickies),
        patch("requests.post", return_value=mock_post_resp),
        patch("requests.patch", return_value=mock_patch_resp) as mock_patch,
    ):
        main(state_file=state_file)

    # 应归档 broken-shadow-id + 写入 + 归档 active-page-id
    patch_urls = [call[0][0] for call in mock_patch.call_args_list]
    assert any("broken-shadow-id" in u for u in patch_urls), "应归档 pending_page_id"
    assert any("active-page-id" in u for u in patch_urls), "应归档旧页面"

    new_state = load_state(state_file)
    assert new_state["hash"] == current_hash
    assert new_state["notion_page_id"] == "new-shadow-id"
    assert "pending_page_id" not in new_state
    assert "old_page_id" not in new_state


def test_main_cleans_up_old_page_on_startup(tmp_path):
    """state 含 old_page_id 时，启动应先归档旧页，再判断 hash"""
    stickies = [_simple_sticky("hello")]
    current_hash = compute_hash(stickies)
    state_file = str(tmp_path / "state.json")
    # 模拟上次归档旧页未完成
    save_state(
        state_file,
        {
            "hash": current_hash,
            "notion_page_id": "current-page-id",
            "old_page_id": "stale-old-id",
        },
    )

    mock_patch_resp = MagicMock()
    mock_patch_resp.raise_for_status = MagicMock()

    with (
        patch("sync_stickies.read_stickies", return_value=stickies),
        patch("requests.patch", return_value=mock_patch_resp) as mock_patch,
        patch("requests.post") as mock_post,
    ):
        main(state_file=state_file)

    # 应归档 stale-old-id（哪怕 hash 相同也要先清理）
    patch_urls = [call[0][0] for call in mock_patch.call_args_list]
    assert any("stale-old-id" in u for u in patch_urls), "应归档 old_page_id"
    # hash 相同，不应创建新页面
    mock_post.assert_not_called()

    new_state = load_state(state_file)
    assert "old_page_id" not in new_state


def test_main_archives_old_page_after_successful_sync(tmp_path):
    """成功同步后旧页面应被归档，且 state 中不含 old_page_id"""
    stickies = [_simple_sticky("updated")]
    state_file = str(tmp_path / "state.json")
    save_state(
        state_file,
        {"hash": "old_hash", "notion_page_id": "old-page-id"},
    )

    mock_post_resp = MagicMock()
    mock_post_resp.raise_for_status = MagicMock()
    mock_post_resp.json.return_value = {"id": "new-page-id"}

    mock_patch_resp = MagicMock()
    mock_patch_resp.raise_for_status = MagicMock()

    with (
        patch("sync_stickies.read_stickies", return_value=stickies),
        patch("requests.post", return_value=mock_post_resp),
        patch("requests.patch", return_value=mock_patch_resp) as mock_patch,
    ):
        main(state_file=state_file)

    patch_urls = [call[0][0] for call in mock_patch.call_args_list]
    assert any("old-page-id" in u for u in patch_urls), "旧页面应被归档"

    new_state = load_state(state_file)
    assert new_state["notion_page_id"] == "new-page-id"
    assert "old_page_id" not in new_state
