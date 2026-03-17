# Mac Stickies → Notion 同步系统 设计文档

**日期：** 2026-03-17  
**状态：** 已用户确认

---

## 1. 需求概述

每 10 分钟自动检查 Mac Stickies 内容是否有变化，有变化则将所有便签内容同步到 Notion 的 "Mac stickies" 页面。

**约束：**
- 单向同步：Mac Stickies → Notion（Notion 端修改会被覆盖）
- 运行方式：macOS LaunchAgent，开机自启，静默后台执行
- Notion 页面结构：所有便签平铺在同一页面，用分割线隔开

---

## 2. 系统架构

### 文件结构

```
~/.local/share/stickies-sync/
├── sync_stickies.py    # 主同步脚本
├── state.json          # 状态持久化 {hash, notion_page_id}
├── sync.log            # 标准输出日志
└── sync_error.log      # 错误日志

~/Library/LaunchAgents/
└── com.user.stickies-sync.plist  # LaunchAgent 定时配置
```

### 数据源

Mac Stickies 数据路径：
`~/Library/Containers/com.apple.Stickies/Data/Library/Stickies/`

每条便签是一个 `.rtfd` bundle，内含 `TXT.rtf` 文件（Cocoa RTF 格式）。

---

## 3. 数据流

每次脚本运行执行以下步骤：

1. **读取便签**：遍历 Stickies 目录，找到所有 `.rtfd` bundle
2. **解析 RTF**：用 macOS 内置 `textutil -convert txt -stdout` 转换每个 `TXT.rtf` 为纯文本
3. **排序**：按文件修改时间倒序（最近修改的在前）
4. **计算 hash**：拼接所有便签文本，计算 MD5
5. **对比状态**：读取 `state.json`，比较 hash
   - 相同 → 静默退出，无任何操作
   - 不同 → 触发 Notion 同步
6. **Notion 同步**：
   - 若 `state.json` 中无 `notion_page_id`：在 workspace 根目录创建 "Mac stickies" 页面
   - 清空页面现有所有 blocks
   - 按顺序写入新内容（见 Notion 页面结构）
7. **更新状态**：将新 hash 和 page_id 写回 `state.json`

---

## 4. Notion 页面结构

页面标题：`Mac stickies`

页面内容（Block 结构）：

```
[paragraph] 便签1 第1行内容
[paragraph] 便签1 第2行内容
...
[divider]
[paragraph] 便签2 第1行内容
...
[divider]
[paragraph] 便签3 内容
```

- 每条便签的每一行对应一个 paragraph block
- 空行跳过
- 便签之间插入 divider block
- 最后一条便签后不加 divider

---

## 5. LaunchAgent 配置

- **plist 路径：** `~/Library/LaunchAgents/com.user.stickies-sync.plist`
- **Label：** `com.user.stickies-sync`
- **触发方式：** `StartInterval: 600`（每 600 秒 = 10 分钟）
- **执行程序：** `/opt/miniconda3/bin/python3 ~/.local/share/stickies-sync/sync_stickies.py`
- **日志：**
  - stdout → `~/.local/share/stickies-sync/sync.log`
  - stderr → `~/.local/share/stickies-sync/sync_error.log`
- **RunAtLoad: true**（加载时立即执行一次）

---

## 6. 依赖

| 依赖 | 说明 | 安装方式 |
|------|------|---------|
| `textutil` | RTF → 纯文本转换 | macOS 内置，无需安装 |
| `requests` | Notion REST API 调用 | pip install requests（环境已有） |

---

## 7. 错误处理策略

| 场景 | 处理方式 |
|------|---------|
| Notion API 调用失败 | 记录错误日志，不更新 state.json（下次重试） |
| 单个 RTF 文件解析失败 | 跳过该便签，记录警告，继续处理其他便签 |
| Stickies 目录不存在 | 记录警告，脚本正常退出（返回码 0） |
| 网络超时 | requests timeout=10s，超时后记录日志并退出 |
| state.json 损坏 | 捕获 JSONDecodeError，重置状态（重新创建页面） |

---

## 8. Notion API 认证

Token 从 opencode 配置文件中读取（`~/.config/opencode/opencode.jsonc` → `mcp.notion.environment.OPENAPI_MCP_HEADERS`）。

脚本中硬编码 token 值（个人工具，非生产环境，文件不入版本控制）。

---

## 9. 实现范围

**包含：**
- sync_stickies.py 主脚本
- com.user.stickies-sync.plist LaunchAgent 配置
- 首次安装说明（加载 LaunchAgent 的命令）

**不包含：**
- 双向同步
- Notion → Stickies 写回
- GUI 或菜单栏图标
- 错误通知（系统通知）
