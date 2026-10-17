# AutoWorker 插件

> Jarvis 外部插件：**AutoWorker 任务管理**。
> 把一批任务（task）组织在一个后端工作目录下，通过编辑器左侧「任务」面板管理，并支持「创建 Agent 执行任务」+「Agent 调用任务管理工具」两条执行路径。

## 功能

- **前端任务面板**（编辑器左侧 sidebar_view）：设置后端工作目录、按任务分组列表、编辑任务信息（标题/描述/标签/截止时间）、右键菜单（创建 Agent 执行该任务 / 标记已完成 / 标记已放弃 / 编辑信息）、创建任务、定时轮询 + 手动刷新。
- **任务与目录结构**：后端工作目录为所有任务的根目录；每个任务分配自动递增 ID（如 `T001`）与标题，用 `ID_标题` 命名子目录（如 `T001_实现登录功能`）；目录名固定用创建时的 `ID_标题`，标题修改只更新 `task.json` 不改目录名。
- **任务状态持久化**：每个任务子目录下 `.jarvis/autoworker/task.json`，插件通过扫描 `<工作目录>/*/.jarvis/autoworker/task.json` 发现任务。状态：`pending` / `running` / `completed` / `abandoned`（无失败/阻塞）。
- **双通道执行**：
  - 右键「创建 Agent 执行该任务」：复用宿主 `window.__jarvisCreateAgentForTask`，新建 Agent 的 `working_dir` 设为任务子目录，创建后标记任务为「执行中」。
  - Agent 调用 `task_manager` 工具查看/修改任务状态、保存关键信息（notes）；执行完成可标记「已完成」。
  - 前端也可手动点击标记状态（兜底路径）。

## 安装

### 从插件市场安装（推荐）

在 Jarvis 插件管理面板的「市场」页签搜索 `autoworker`，一键安装。

### 命令行安装

```bash
# 从源码仓库安装
jarvis --install-plugin https://github.com/skyfireitdiy/jarvis-plugin-autoworker
# 或从本地目录安装
jarvis --install-plugin /path/to/autoworker
```

安装后重启 Jarvis 使插件注册生效：

```bash
jarvis --list-plugins   # 确认 autoworker 插件已注册
```

### 卸载

```bash
jarvis --uninstall-plugin autoworker
```

> 注：autoworker 已从 Jarvis 核心内置插件迁移为独立第三方插件（见 [Issue #271](https://github.com/skyfireitdiy/Jarvis/issues/271)）。旧版本内置插件用户升级后需从市场重新安装。

## 目录结构

```text
autoworker/
├── config.yaml          # 插件配置：logo、tool_load_dirs、code_agent_tools、frontend.sidebar_views、capabilities
├── plugin/
│   └── api.py           # 任务 CRUD 私有功能（运行在 gateway，供前端代理调用）
├── tools/
│   └── task_manager.py  # Agent 任务管理工具（运行在 agent 进程，importlib 复用 api.py）
└── frontend/
    ├── icon.svg         # 插件图标
    └── sidebar_view.js  # 任务面板（window.Vue 单文件自包含）
```

## 任务数据结构

每个任务子目录下的 `.jarvis/autoworker/task.json`：

```json
{
  "id": "T001",
  "title": "实现登录功能",
  "description": "…",
  "tags": ["前端", "认证"],
  "due_date": "2026-10-20",
  "status": "pending",
  "created_at": "2026-10-10T07:00:00+08:00",
  "updated_at": "2026-10-10T07:00:00+08:00",
  "notes": [{ "time": "2026-10-10T07:05:00+08:00", "content": "…" }]
}
```

## 任务管理工具（task_manager）

任意 Agent 可调用，动作（action）：

- `list_tasks`：列出任务（默认扫描当前目录，或指定 workdir）
- `get_task`：查看单个任务
- `create_task`：创建任务
- `update_task`：更新任务详情（标题/描述/标签/截止时间）
- `set_status`：标记状态（pending/running/completed/abandoned）
- `add_note`：保存关键信息（带时间戳）
- `delete_task`：软删除任务（数据保留，可恢复）
- `restore_task`：恢复已软删除任务

工具运行在 agent 进程，Agent 的当前工作目录即其 `working_dir`。当 Agent 被「创建 Agent 执行该任务」创建时，`working_dir` 即任务子目录，工具默认定位 `./.jarvis/autoworker/task.json`；也支持显式传 `workdir`（后端工作目录，即任务子目录的父目录）+ `task_id` 操作其他任务。

## 环境要求

- Jarvis（支持外部插件，含前端 sidebar_view 扩展与插件私有功能代理）

## 许可证

MIT
