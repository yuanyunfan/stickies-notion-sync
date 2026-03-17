# Spec: Rich Text Formatting Sync (Bold + Color)

**Date:** 2026-03-17  
**Status:** Approved  
**Project:** stickies-notion-sync

---

## 1. Goal

Extend the Mac Stickies → Notion sync to preserve **bold** and **text color** formatting, in addition to plain text content.

---

## 2. Background

The current implementation uses `textutil -convert txt` which strips all formatting. The RTF files produced by Mac Stickies contain:
- Bold text (via `\f0\b` RTF commands and font name `PingFang SC Semibold`)
- Colored text (via `\cf<N>` RTF commands referencing a color table)
- Both paragraph-level and inline (span-level) formatting

`textutil -convert html` already outputs a well-structured HTML document with CSS classes encoding bold and color, making it the natural parsing source.

---

## 3. Approach

Use `textutil -convert html` as the RTF parsing step. Parse the resulting HTML with Python's stdlib `html.parser` to produce a list of **runs** per paragraph. Map hex colors to Notion's 10 named text colors. Emit Notion `rich_text` arrays with `annotations`.

Zero new dependencies. All parsing done with macOS built-ins and Python stdlib.

---

## 4. Data Model

### 4.1 `Run` (TypedDict)

```python
class Run(TypedDict):
    text: str
    bold: bool
    color: str  # Notion color name: "default" | "red" | "orange" | "yellow"
                #                    | "green" | "blue" | "purple" | "pink"
                #                    | "gray" | "brown"
```

### 4.2 `read_stickies` return type change

**Before:** `List[Tuple[str, float]]`  
**After:** `List[Tuple[List[List[Run]], float]]`

Where the inner `List[List[Run]]` is a list of paragraphs, each paragraph being a list of runs.

Wait — for simplicity and minimal interface change, we flatten: each sticky is a list of paragraphs, and each paragraph is a list of runs.

Revised: `List[Tuple[List[List[Run]], float]]`
- Outer list: one sticky per item
- `List[List[Run]]`: list of paragraphs in the sticky
- `List[Run]`: list of runs in one paragraph

---

## 5. New / Modified Functions

### 5.1 `hex_to_notion_color(hex_color: str) -> str`

Converts a CSS hex color (e.g. `#ff0000`) to a Notion color name using HSV color space:

| Condition | Notion color |
|-----------|-------------|
| Saturation < 0.2 (gray/black/white) | `"default"` |
| Hue 0°–20° or 340°–360° | `"red"` |
| Hue 20°–45° | `"orange"` |
| Hue 45°–70° | `"yellow"` |
| Hue 70°–165° | `"green"` |
| Hue 165°–260° | `"blue"` |
| Hue 260°–290° | `"purple"` |
| Hue 290°–340° | `"pink"` |
| Parse error / empty | `"default"` |

Uses `colorsys.rgb_to_hsv` from Python stdlib.

### 5.2 `parse_html_to_paragraphs(html: str) -> List[List[Run]]`

Internal helper (not exported). Steps:
1. Parse `<style>` block with regex to build `class_name → {bold: bool, color_hex: str | None}` mapping.
   - Class has bold if `font-family` contains `Semibold` or `Bold`.
   - Class has color if `color: #RRGGBB` present.
2. Walk `<body>` using `html.parser.HTMLParser`.
3. For each `<p>`:
   - Determine paragraph-level default bold and color from its CSS class.
   - Walk child nodes: `<b>` sets bold=True, `<span>` may override color, text nodes emit a run.
   - Emit `Run(text=..., bold=..., color=...)` for each non-empty text segment.
   - If paragraph produces at least one non-whitespace run, include it.
   - Empty `<p>` (spacer) → emit a single `Run(text="", bold=False, color="default")` as a blank paragraph marker.
4. Return list of paragraphs (each paragraph = list of runs).

### 5.3 `read_stickies` (modified)

- Replace `textutil -convert txt` with `textutil -convert html`.
- Call `parse_html_to_paragraphs(html)` instead of `proc.stdout.strip()`.
- Return `List[Tuple[List[List[Run]], float]]`.

### 5.4 `compute_hash` (modified)

Hash over the full run structure (not just plain text) so that formatting changes trigger re-sync:

```python
def compute_hash(stickies: List[Tuple[List[List[Run]], float]]) -> str:
    serialized = json.dumps(
        [[para for para in paragraphs] for paragraphs, _ in stickies],
        ensure_ascii=False, sort_keys=True
    )
    return hashlib.md5(serialized.encode("utf-8")).hexdigest()
```

### 5.5 `stickies_to_blocks` (modified)

Each paragraph (= `List[Run]`) becomes one Notion `paragraph` block. Each run becomes one element in `rich_text`:

```python
{
    "type": "paragraph",
    "paragraph": {
        "rich_text": [
            {
                "type": "text",
                "text": {"content": run["text"]},
                "annotations": {
                    "bold": run["bold"],
                    "color": run["color"]
                }
            }
            for run in paragraph
        ]
    }
}
```

Empty paragraphs (blank lines) become `{"type": "paragraph", "paragraph": {"rich_text": []}}`.

Dividers between stickies are preserved as before.

---

## 6. Color Mapping Examples (from real stickies data)

| RTF hex | Notion color |
|---------|-------------|
| `#ff0000` | `red` |
| `#ff0009` | `red` |
| `#ff0021` | `red` |
| `#0000ff` | `blue` |
| `#0d00ff` | `blue` |
| `#000000` | `default` |

---

## 7. Test Plan

### Existing tests (update mocks)
All 22 existing tests that use `read_stickies` return values or `stickies_to_blocks` inputs must be updated to use the new `List[List[Run]]` data shape.

### New tests
| Test | Description |
|------|-------------|
| `test_hex_to_notion_color_red` | `#ff0000` → `"red"` |
| `test_hex_to_notion_color_blue` | `#0000ff` → `"blue"` |
| `test_hex_to_notion_color_low_sat` | `#888888` → `"default"` |
| `test_hex_to_notion_color_black` | `#000000` → `"default"` |
| `test_parse_html_bold` | `<b>` tag produces `bold=True` run |
| `test_parse_html_paragraph_color` | paragraph-level color class applied to runs |
| `test_parse_html_inline_color` | span-level color overrides paragraph color |
| `test_parse_html_mixed` | bold + color on same run |
| `test_parse_html_empty_paragraph` | blank `<p>` → empty paragraph placeholder |
| `test_stickies_to_blocks_annotations` | rich_text has bold/color annotations |

Total target: ~32+ tests passing.

---

## 8. Out of Scope

- Italic, underline, strikethrough (not used in Mac Stickies)
- Background colors (Notion supports them, but Stickies only sets text color)
- Font size differences (Notion doesn't support arbitrary font sizes)
- Sticky background color (the yellow/pink/blue sticky color)
- Bidirectional color sync (Notion → Stickies)

---

## 9. Files Changed

| File | Change |
|------|--------|
| `sync_stickies.py` | Add `hex_to_notion_color`, `parse_html_to_paragraphs`; modify `read_stickies`, `compute_hash`, `stickies_to_blocks` |
| `tests/test_sync.py` | Update all existing mocks + add ~10 new tests |

No new files, no new dependencies.
