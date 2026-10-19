"""
AutoWorker 插件私有功能层（运行在 gateway，供前端代理调用；不暴露给 Agent）。

提供任务 CRUD 纯函数：
- create_task / update_task / set_status / add_note：写操作
- link_task / unlink_task：任务间关联（links）增删
- list_tasks / get_task：读操作

任务持久化到后端工作目录下每个任务子目录的 `.jarvis/autoworker/task.json`：
    <workdir>/<ID>_<标题>/.jarvis/autoworker/task.json
- 目录名固定用创建时的 `ID_标题`，标题修改只更新 task.json，不改目录名（避免引用路径漂移）。
- ID 自动递增（T001、T002…）。
- 状态：pending / running / completed / abandoned（无失败/阻塞）。

每个函数返回 dict：{"success": bool, "data": .../ "message": .../ "error": ...}。
本模块不声明在 tool_load_dirs，因此不会被 ToolRegistry 加载，Agent 不可见；
gateway 通过插件功能代理端点动态加载本模块并调用。
"""
import json
import os
import re
from datetime import datetime
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

# 合法任务状态（无失败/阻塞）
VALID_STATUS = {"pending", "running", "completed", "abandoned"}
# 状态展示名
STATUS_LABELS = {
    "pending": "待执行",
    "running": "执行中",
    "completed": "已完成",
    "abandoned": "已放弃",
}
# 任务文件相对任务子目录的路径
_TASK_REL_PATH = os.path.join(".jarvis", "autoworker", "task.json")
# 合法的任务关联类型
VALID_RELATIONS = {"related", "blocked_by", "blocks", "duplicate"}
# 关联类型展示名
RELATION_LABELS = {
    "related": "相关",
    "blocked_by": "被阻塞",
    "blocks": "阻塞",
    "duplicate": "重复",
}


def _now() -> str:
    """当前本地时间，ISO 8601 格式（含时区，秒精度）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _clean_dir_name(title: str) -> str:
    """清理标题中的非法字符，用于目录命名（不用于 task.json 的 title 字段）。"""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n]+', "_", str(title or "").strip())
    return cleaned or "未命名"


def _task_file_path(task_dir: str) -> str:
    """任务子目录 -> task.json 绝对路径。"""
    return os.path.join(task_dir, _TASK_REL_PATH)


def _load_task(task_dir: str) -> Optional[Dict[str, Any]]:
    """读取任务子目录下的 task.json；不存在或解析失败返回 None。
    返回的任务 dict 附带内部字段 task_dir（任务子目录绝对路径）。
    """
    tf = _task_file_path(task_dir)
    if not os.path.isfile(tf):
        return None
    try:
        with open(tf, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        data["task_dir"] = task_dir
        return data
    except Exception:
        return None


def _scan_tasks(workdir: str, include_deleted: bool = False) -> List[Dict[str, Any]]:
    """扫描 <workdir>/*/.jarvis/autoworker/task.json，返回任务列表（按 id 数值升序）。

    include_deleted=False（默认）时过滤掉已软删除（deleted=True）的任务。
    """
    tasks: List[Dict[str, Any]] = []
    if not os.path.isdir(workdir):
        return tasks
    try:
        entries = sorted(os.listdir(workdir))
    except Exception:
        return tasks
    for name in entries:
        task_dir = os.path.join(workdir, name)
        if not os.path.isdir(task_dir):
            continue
        task = _load_task(task_dir)
        if not task:
            continue
        if not include_deleted and task.get("deleted"):
            continue
        tasks.append(task)

    def _sort_key(t: Dict[str, Any]) -> int:
        m = re.match(r"^T(\d+)$", str(t.get("id", "")))
        return int(m.group(1)) if m else 0

    tasks.sort(key=_sort_key)
    return tasks


def _next_task_id(workdir: str) -> str:
    """生成下一个递增任务 ID（T001、T002…）。"""
    max_num = 0
    for t in _scan_tasks(workdir):
        m = re.match(r"^T(\d+)$", str(t.get("id", "")))
        if m:
            max_num = max(max_num, int(m.group(1)))
    return f"T{max_num + 1:03d}"


def _find_task(
    workdir: str, task_id: str, include_deleted: bool = True
) -> Optional[Dict[str, Any]]:
    """按任务 ID 查找任务；未找到返回 None。

    include_deleted=True（默认）时也查找已软删除的任务，供 delete/restore 定位。
    """
    for t in _scan_tasks(workdir, include_deleted=include_deleted):
        if str(t.get("id", "")) == task_id:
            return t
    return None


def _require_workdir(workdir: Any) -> tuple:
    """校验并规范化后端工作目录。返回 (workdir, error)；workdir 为空/不存在时 error 非空。"""
    workdir = (workdir or "").strip()
    if not workdir:
        return "", "未指定后端工作目录"
    workdir = os.path.abspath(os.path.expanduser(workdir))
    if not os.path.isdir(workdir):
        return "", f"后端工作目录不存在: {workdir}"
    return workdir, ""


def _require_task_id(task_id: Any) -> tuple:
    """校验任务 ID。返回 (task_id, error)。"""
    task_id = str(task_id or "").strip()
    if not task_id:
        return "", "未指定任务 ID"
    return task_id, ""


def _write_task(task: Dict[str, Any]) -> Dict[str, Any]:
    """把任务 dict 写回其 task.json（剔除内部字段 task_dir）。"""
    task_dir = task.get("task_dir")
    if not task_dir:
        return {"success": False, "error": "任务目录缺失"}
    tf = _task_file_path(task_dir)
    try:
        os.makedirs(os.path.dirname(tf), exist_ok=True)
        data = {k: v for k, v in task.items() if k != "task_dir"}
        with open(tf, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return {"success": True, "data": data}
    except Exception as e:
        return {"success": False, "error": f"写入任务文件失败: {e}"}


def _normalize_tags(tags: Any) -> List[str]:
    """规范化标签：接受 list 或逗号分隔字符串，返回去重后的字符串列表。"""
    if tags is None:
        return []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    elif isinstance(tags, (list, tuple)):
        tags = [str(t).strip() for t in tags if str(t).strip()]
    else:
        tags = []
    seen = set()
    result = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result


def _normalize_due_date(due_date: Any) -> str:
    """规范化截止时间：接受空/None（返回空串）或字符串（裁剪）。"""
    if due_date is None:
        return ""
    return str(due_date).strip()


def _normalize_parent_id(parent_id: Any) -> Optional[str]:
    """规范化父任务 ID：空/None/"null"/"0" 均视为顶层任务（返回 None）。"""
    if parent_id is None:
        return None
    pid = str(parent_id).strip()
    if not pid or pid.lower() in ("null", "none", "0"):
        return None
    return pid


def _normalize_links(links: Any) -> List[Dict[str, str]]:
    """规范化关联列表：丢弃非法项、去重（同 task_id + relation）、校验 relation。

    返回列表；非法 relation 的项会被丢弃（调用方如需报错可自行预校验）。
    """
    if not isinstance(links, (list, tuple)):
        return []
    result: List[Dict[str, str]] = []
    seen = set()
    for item in links:
        if not isinstance(item, dict):
            continue
        tid = str(item.get("task_id") or "").strip()
        relation = str(item.get("relation") or "related").strip()
        if not tid or relation not in VALID_RELATIONS:
            continue
        key = (tid, relation)
        if key in seen:
            continue
        seen.add(key)
        entry = {"task_id": tid, "relation": relation}
        note = str(item.get("note") or "").strip()
        if note:
            entry["note"] = note
        result.append(entry)
    return result


def _build_children_map(tasks: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """由 parent_id 反向推导 children 映射（parent_id 为唯一事实来源）。

    仅收录存在于 tasks 列表中的父任务；父任务不存在（悬空 parent_id）的项忽略。
    """
    ids = {str(t.get("id", "")) for t in tasks}
    children: Dict[str, List[str]] = {}
    for t in tasks:
        pid = _normalize_parent_id(t.get("parent_id"))
        if pid and pid in ids:
            children.setdefault(pid, []).append(str(t.get("id", "")))
    return children


def _attach_children(task: Dict[str, Any], children_map: Dict[str, List[str]]) -> None:
    """把推导出的 children 写入任务 dict（仅展示用，不落盘）。"""
    task["children"] = list(children_map.get(str(task.get("id", "")), []))


def _descendant_ids(
    workdir: str, task_id: str, include_deleted: bool = False
) -> List[str]:
    """返回 task_id 的全部后代任务 ID（广度优先，不含自身）。"""
    all_tasks = _scan_tasks(workdir, include_deleted=include_deleted)
    children_map = _build_children_map(all_tasks)
    result: List[str] = []
    queue = list(children_map.get(task_id, []))
    seen = set()
    while queue:
        cur = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        result.append(cur)
        queue.extend(children_map.get(cur, []))
    return result


def _tree_depth(
    task_id: str,
    tasks_by_id: Dict[str, Dict[str, Any]],
    cache: Dict[str, int],
) -> int:
    """计算任务在树中的深度（顶层为 0）。

    tasks_by_id 为 id -> 任务 dict（已带 children）；悬空 parent_id 视为顶层，
    并用 cache 防御异常数据造成的环。
    """
    if task_id in cache:
        return cache[task_id]
    cache[task_id] = 0  # 防御环
    task = tasks_by_id.get(task_id) or {}
    pid = _normalize_parent_id(task.get("parent_id"))
    depth = 0
    if pid and pid in tasks_by_id:
        depth = _tree_depth(pid, tasks_by_id, cache) + 1
    cache[task_id] = depth
    return depth


def _descendant_children(workdir: str, task_id: str) -> List[str]:
    """返回 task_id 的直接子任务 ID 列表（由 parent_id 推导）。"""
    all_tasks = _scan_tasks(workdir)
    return list(_build_children_map(all_tasks).get(task_id, []))


def _validate_parent(
    workdir: str, task_id: str, parent_id: Optional[str]
) -> str:
    """校验 parent_id 合法性；返回错误信息（空串表示合法）。

    - 父任务必须存在且未删除；
    - 不能把自己设为父任务；
    - 不能把后代任务设为父任务（避免成环）。
    """
    if not parent_id:
        return ""
    if parent_id == task_id:
        return "父任务不能是任务自身"
    parent = _find_task(workdir, parent_id, include_deleted=False)
    if not parent:
        return f"父任务不存在: {parent_id}"
    if parent_id in _descendant_ids(workdir, task_id):
        return f"父任务 {parent_id} 是当前任务的后代，会形成循环"
    return ""


def create_task(
    workdir: Optional[str] = None,
    title: Optional[str] = None,
    description: str = "",
    tags: Any = None,
    due_date: Any = None,
    parent_id: Any = None,
) -> Dict[str, Any]:
    """创建任务：生成递增 ID，创建 <workdir>/<ID>_<标题> 子目录与 task.json。
    目录名固定用创建时的 ID_标题；返回 data 含 id/task_dir 等。

    parent_id: 父任务 ID；为空则创建为顶层任务。子任务目录仍平铺在 workdir 下
    （ID 全局递增），不改变目录层级，避免路径漂移。
    """
    workdir, err = _require_workdir(workdir)
    if err:
        return {"success": False, "error": err}
    title = (title or "").strip()
    if not title:
        return {"success": False, "error": "请提供任务标题"}
    parent_id = _normalize_parent_id(parent_id)
    if parent_id:
        err = _validate_parent(workdir, "", parent_id)
        if err:
            return {"success": False, "error": err}
    task_id = _next_task_id(workdir)
    dir_name = f"{task_id}_{_clean_dir_name(title)}"
    task_dir = os.path.join(workdir, dir_name)
    if os.path.exists(task_dir):
        return {"success": False, "error": f"任务目录已存在: {task_dir}"}
    now = _now()
    task: Dict[str, Any] = {
        "id": task_id,
        "title": title,
        "description": str(description or ""),
        "tags": _normalize_tags(tags),
        "due_date": _normalize_due_date(due_date),
        "parent_id": parent_id,
        "links": [],
        "status": "pending",
        "created_at": now,
        "updated_at": now,
        "notes": [],
        "task_dir": task_dir,
    }
    result = _write_task(task)
    if not result["success"]:
        return result
    # task_dir 是内部字段（不落盘），但对外返回时补上：前端需据此设置 Agent 工作目录
    data = dict(result["data"])
    data["task_dir"] = task_dir
    data["children"] = []
    return {
        "success": True,
        "data": data,
        "message": f"任务 {task_id} 已创建",
    }


def update_task(
    workdir: Optional[str] = None,
    task_id: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    tags: Any = None,
    due_date: Any = None,
    parent_id: Any = None,
) -> Dict[str, Any]:
    """更新任务详情（标题/描述/标签/截止时间/父任务）。
    标题修改只更新 task.json，不改目录名（避免引用路径漂移）。
    parent_id 传空（None 表示不修改；传 "" / "null" / "0" 表示移到顶层）。
    """
    workdir, err = _require_workdir(workdir)
    if err:
        return {"success": False, "error": err}
    task_id, err = _require_task_id(task_id)
    if err:
        return {"success": False, "error": err}
    task = _find_task(workdir, task_id)
    if not task:
        return {"success": False, "error": f"任务不存在: {task_id}"}
    if title is not None:
        title = str(title).strip()
        if not title:
            return {"success": False, "error": "任务标题不能为空"}
        task["title"] = title
    if description is not None:
        task["description"] = str(description)
    if tags is not None:
        task["tags"] = _normalize_tags(tags)
    if due_date is not None:
        task["due_date"] = _normalize_due_date(due_date)
    if parent_id is not None:
        new_parent = _normalize_parent_id(parent_id)
        err = _validate_parent(workdir, task_id, new_parent)
        if err:
            return {"success": False, "error": err}
        task["parent_id"] = new_parent
    task["updated_at"] = _now()
    result = _write_task(task)
    if not result["success"]:
        return result
    data = dict(result["data"])
    data["task_dir"] = task["task_dir"]
    data["children"] = _descendant_children(workdir, task_id)
    return {
        "success": True,
        "data": data,
        "message": f"任务 {task_id} 已更新",
    }


def set_status(
    workdir: Optional[str] = None,
    task_id: Optional[str] = None,
    status: Optional[str] = None,
) -> Dict[str, Any]:
    """标记任务状态（pending/running/completed/abandoned）。"""
    workdir, err = _require_workdir(workdir)
    if err:
        return {"success": False, "error": err}
    task_id, err = _require_task_id(task_id)
    if err:
        return {"success": False, "error": err}
    status = str(status or "").strip()
    if status not in VALID_STATUS:
        return {
            "success": False,
            "error": f"无效状态: {status}（可选: {', '.join(sorted(VALID_STATUS))}）",
        }
    task = _find_task(workdir, task_id)
    if not task:
        return {"success": False, "error": f"任务不存在: {task_id}"}
    task["status"] = status
    task["updated_at"] = _now()
    result = _write_task(task)
    if not result["success"]:
        return result
    data = dict(result["data"])
    data["task_dir"] = task["task_dir"]
    return {
        "success": True,
        "data": data,
        "message": f"任务 {task_id} 状态已更新为 {STATUS_LABELS.get(status, status)}",
    }


def delete_task(
    workdir: Optional[str] = None,
    task_id: Optional[str] = None,
    cascade: bool = False,
) -> Dict[str, Any]:
    """软删除任务：只写 deleted 标记，不删除任务文件/目录。

    删除后任务默认从列表消失（list_tasks 过滤），数据仍保留在磁盘。
    - 任务有未删除的子任务时：默认阻止删除并返回错误；
      cascade=True 时连同全部后代任务一起软删除。
    """
    workdir, err = _require_workdir(workdir)
    if err:
        return {"success": False, "error": err}
    task_id, err = _require_task_id(task_id)
    if err:
        return {"success": False, "error": err}
    task = _find_task(workdir, task_id, include_deleted=True)
    if not task:
        return {"success": False, "error": f"任务不存在: {task_id}"}
    if task.get("deleted"):
        return {"success": False, "error": f"任务 {task_id} 已删除"}
    descendants = _descendant_ids(workdir, task_id)
    if descendants and not cascade:
        return {
            "success": False,
            "error": (
                f"任务 {task_id} 有 {len(descendants)} 个子任务"
                f"（{', '.join(descendants)}），请先删除子任务或使用 cascade 级联删除"
            ),
        }
    now = _now()
    deleted_ids: List[str] = []
    for tid in [task_id] + descendants:
        target = task if tid == task_id else _find_task(workdir, tid, include_deleted=True)
        if not target or target.get("deleted"):
            continue
        target["deleted"] = True
        target["deleted_at"] = now
        target["updated_at"] = now
        res = _write_task(target)
        if not res["success"]:
            return res
        deleted_ids.append(tid)
    data = {k: v for k, v in task.items() if k != "task_dir"}
    data["task_dir"] = task["task_dir"]
    return {
        "success": True,
        "data": data,
        "deleted_ids": deleted_ids,
        "message": (
            f"任务 {task_id} 已删除（数据保留，可恢复）"
            if len(deleted_ids) <= 1
            else f"任务 {task_id} 及其 {len(deleted_ids) - 1} 个子任务已删除（数据保留，可恢复）"
        ),
    }


def restore_task(
    workdir: Optional[str] = None, task_id: Optional[str] = None
) -> Dict[str, Any]:
    """恢复已软删除的任务：清除 deleted 标记。"""
    workdir, err = _require_workdir(workdir)
    if err:
        return {"success": False, "error": err}
    task_id, err = _require_task_id(task_id)
    if err:
        return {"success": False, "error": err}
    task = _find_task(workdir, task_id, include_deleted=True)
    if not task:
        return {"success": False, "error": f"任务不存在: {task_id}"}
    if not task.get("deleted"):
        return {"success": False, "error": f"任务 {task_id} 未删除，无需恢复"}
    # 父任务已被删除（级联删除后单独恢复子任务）：自动提升为顶层任务，
    # 避免恢复后成为不可见的孤儿任务。
    pid = _normalize_parent_id(task.get("parent_id"))
    if pid and not _find_task(workdir, pid, include_deleted=False):
        task["parent_id"] = None
    task.pop("deleted", None)
    task.pop("deleted_at", None)
    task["updated_at"] = _now()
    result = _write_task(task)
    if not result["success"]:
        return result
    data = dict(result["data"])
    data["task_dir"] = task["task_dir"]
    return {
        "success": True,
        "data": data,
        "message": f"任务 {task_id} 已恢复",
    }


def add_note(
    workdir: Optional[str] = None,
    task_id: Optional[str] = None,
    content: Optional[str] = None,
) -> Dict[str, Any]:
    """保存关键信息（notes），带时间戳，供后续生成任务报告。"""
    workdir, err = _require_workdir(workdir)
    if err:
        return {"success": False, "error": err}
    task_id, err = _require_task_id(task_id)
    if err:
        return {"success": False, "error": err}
    content = (content or "").strip()
    if not content:
        return {"success": False, "error": "请提供 note 内容"}
    task = _find_task(workdir, task_id)
    if not task:
        return {"success": False, "error": f"任务不存在: {task_id}"}
    notes = task.get("notes") or []
    if not isinstance(notes, list):
        notes = []
    notes.append({"time": _now(), "content": content})
    task["notes"] = notes
    task["updated_at"] = _now()
    result = _write_task(task)
    if not result["success"]:
        return result
    data = dict(result["data"])
    data["task_dir"] = task["task_dir"]
    return {
        "success": True,
        "data": data,
        "message": f"已为任务 {task_id} 保存关键信息",
    }


def link_task(
    workdir: Optional[str] = None,
    task_id: Optional[str] = None,
    target_id: Optional[str] = None,
    relation: Optional[str] = None,
    note: str = "",
) -> Dict[str, Any]:
    """为任务添加一条关联（links）：{task_id, relation, note?}。

    - relation 默认 related，可选 related/blocked_by/blocks/duplicate；
    - 不能关联自身；目标任务必须存在且未删除；
    - 同一 (target_id, relation) 已存在时不重复添加（幂等）。
    """
    workdir, err = _require_workdir(workdir)
    if err:
        return {"success": False, "error": err}
    task_id, err = _require_task_id(task_id)
    if err:
        return {"success": False, "error": err}
    target_id = str(target_id or "").strip()
    if not target_id:
        return {"success": False, "error": "请提供关联任务 ID（target_id）"}
    if target_id == task_id:
        return {"success": False, "error": "不能关联任务自身"}
    relation = str(relation or "related").strip() or "related"
    if relation not in VALID_RELATIONS:
        return {
            "success": False,
            "error": (
                f"无效关联类型: {relation}"
                f"（可选: {', '.join(sorted(VALID_RELATIONS))}）"
            ),
        }
    task = _find_task(workdir, task_id)
    if not task:
        return {"success": False, "error": f"任务不存在: {task_id}"}
    if not _find_task(workdir, target_id):
        return {"success": False, "error": f"关联任务不存在: {target_id}"}
    links = _normalize_links(task.get("links"))
    if any(lk["task_id"] == target_id and lk["relation"] == relation for lk in links):
        return {
            "success": False,
            "error": f"任务 {task_id} 已存在到 {target_id} 的 {relation} 关联",
        }
    entry = {"task_id": target_id, "relation": relation}
    note = str(note or "").strip()
    if note:
        entry["note"] = note
    links.append(entry)
    task["links"] = links
    task["updated_at"] = _now()
    result = _write_task(task)
    if not result["success"]:
        return result
    data = {k: v for k, v in task.items() if k != "task_dir"}
    data["task_dir"] = task["task_dir"]
    return {
        "success": True,
        "data": data,
        "message": (
            f"任务 {task_id} 已关联 {target_id}"
            f"（{RELATION_LABELS.get(relation, relation)}）"
        ),
    }


def unlink_task(
    workdir: Optional[str] = None,
    task_id: Optional[str] = None,
    target_id: Optional[str] = None,
    relation: Optional[str] = None,
) -> Dict[str, Any]:
    """解除任务关联：按 target_id（可选再按 relation）移除 links 项。"""
    workdir, err = _require_workdir(workdir)
    if err:
        return {"success": False, "error": err}
    task_id, err = _require_task_id(task_id)
    if err:
        return {"success": False, "error": err}
    target_id = str(target_id or "").strip()
    if not target_id:
        return {"success": False, "error": "请提供关联任务 ID（target_id）"}
    relation = str(relation or "").strip()
    if relation and relation not in VALID_RELATIONS:
        return {"success": False, "error": f"无效关联类型: {relation}"}
    task = _find_task(workdir, task_id)
    if not task:
        return {"success": False, "error": f"任务不存在: {task_id}"}
    links = _normalize_links(task.get("links"))
    kept = [
        lk
        for lk in links
        if not (
            lk["task_id"] == target_id
            and (not relation or lk["relation"] == relation)
        )
    ]
    if len(kept) == len(links):
        return {
            "success": False,
            "error": f"任务 {task_id} 不存在到 {target_id} 的关联",
        }
    task["links"] = kept
    task["updated_at"] = _now()
    result = _write_task(task)
    if not result["success"]:
        return result
    data = {k: v for k, v in task.items() if k != "task_dir"}
    data["task_dir"] = task["task_dir"]
    return {
        "success": True,
        "data": data,
        "message": f"任务 {task_id} 已解除与 {target_id} 的关联",
    }


def _task_matches(
    task: Dict[str, Any],
    query: str,
    tags: Optional[List[str]],
    status: Optional[str],
) -> bool:
    """判断任务是否命中过滤条件（各条件为「与」关系，均为空则命中）。"""
    if status and str(task.get("status", "")) != status:
        return False
    if tags:
        task_tags = {str(t) for t in (task.get("tags") or [])}
        if not all(str(t) in task_tags for t in tags):
            return False
    if query:
        q = query.lower()
        haystack = [str(task.get("title", "")), str(task.get("description", ""))]
        for note in task.get("notes") or []:
            if isinstance(note, dict):
                haystack.append(str(note.get("content", "")))
        if not any(q in h.lower() for h in haystack):
            return False
    return True


def _task_summary(task: Dict[str, Any], include_notes: bool) -> Dict[str, Any]:
    """生成列表用的任务摘要。

    默认剥离 notes 全文（只保留 note_count），避免任务多/备注长时列表体积过大；
    include_notes=True 时保留完整 notes。
    """
    item = dict(task)
    notes = item.get("notes") or []
    if not isinstance(notes, list):
        notes = []
    item["note_count"] = len(notes)
    if not include_notes:
        item.pop("notes", None)
    return item


def list_tasks(
    workdir: Optional[str] = None,
    query: Optional[str] = None,
    tags: Optional[List[str]] = None,
    status: Optional[str] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    include_notes: bool = True,
    include_deleted: bool = False,
    tree: bool = False,
) -> Dict[str, Any]:
    """列出后端工作目录下的任务（按 id 升序），支持过滤与分页。

    - query: 关键词，大小写不敏感，匹配 title/description/notes 内容（任一命中即可）。
    - tags: 标签列表，需全部命中任务的 tags。
    - status: 精确匹配任务状态（pending/running/completed/abandoned）。
    - limit/offset: 分页；limit 为 None 时不分页（返回全部，向后兼容）。
    - include_notes: 列表项是否保留 notes 全文，默认 True（保持前端面板旧行为）；
      Agent 侧工具默认置 False，只返回 note_count 以节省上下文。
    - include_deleted: 是否包含已软删除任务，默认 False（列表默认隐藏已删除）。
    - tree: 是否返回树形结构（data 中每项带 depth/children_count/child_done_count），
      默认 False 保持扁平列表。每个任务始终带 parent_id 与 children（由 parent_id 推导）。

    返回 data 为任务列表，并附带 total/offset/limit/has_more。
    """
    workdir, err = _require_workdir(workdir)
    if err:
        return {"success": False, "error": err}

    query = (query or "").strip()
    status = (status or "").strip() or None
    if status is not None and status not in VALID_STATUS:
        return {"success": False, "error": f"无效状态: {status}"}
    tag_list = [str(t).strip() for t in (tags or []) if str(t).strip()]

    all_tasks = _scan_tasks(workdir, include_deleted=include_deleted)
    children_map = _build_children_map(all_tasks)
    status_by_id = {str(t.get("id", "")): t.get("status") for t in all_tasks}
    tasks_by_id = {str(t.get("id", "")): t for t in all_tasks}
    for t in all_tasks:
        _attach_children(t, children_map)

    matched = [
        t for t in all_tasks if _task_matches(t, query, tag_list, status)
    ]
    total = len(matched)

    # 分页：limit 为 None 时不分页（保持旧行为）
    if limit is None:
        page_items = matched
        eff_offset = 0
        eff_limit = total
    else:
        try:
            eff_limit = int(limit)
            eff_offset = int(offset or 0)
        except (TypeError, ValueError):
            return {"success": False, "error": "limit/offset 必须为整数"}
        if eff_limit < 0 or eff_offset < 0:
            return {"success": False, "error": "limit/offset 不能为负数"}
        page_items = matched[eff_offset : eff_offset + eff_limit]

    data = [_task_summary(t, include_notes) for t in page_items]
    if tree:
        # 树形视图：按 parent_id 计算缩进深度，并给出子任务完成进度
        depth_by_id: Dict[str, int] = {}
        for item in data:
            item["children_count"] = len(item.get("children") or [])
            done = sum(
                1
                for cid in item.get("children") or []
                if status_by_id.get(cid) == "completed"
            )
            item["child_done_count"] = done
            item["depth"] = _tree_depth(
                str(item.get("id", "")), tasks_by_id, depth_by_id
            )
    has_more = (eff_offset + len(page_items)) < total
    return {
        "success": True,
        "data": data,
        "workdir": workdir,
        "count": len(data),
        "total": total,
        "offset": eff_offset,
        "limit": eff_limit,
        "has_more": has_more,
    }


def get_task(
    workdir: Optional[str] = None, task_id: Optional[str] = None
) -> Dict[str, Any]:
    """查看单个任务详情。"""
    workdir, err = _require_workdir(workdir)
    if err:
        return {"success": False, "error": err}
    task_id, err = _require_task_id(task_id)
    if err:
        return {"success": False, "error": err}
    task = _find_task(workdir, task_id)
    if not task:
        return {"success": False, "error": f"任务不存在: {task_id}"}
    all_tasks = _scan_tasks(workdir)
    children_map = _build_children_map(all_tasks)
    _attach_children(task, children_map)
    children = task["children"]
    child_tasks = {str(t.get("id", "")): t for t in all_tasks}
    task["children_count"] = len(children)
    task["child_done_count"] = sum(
        1
        for c in children
        if str((child_tasks.get(c) or {}).get("status")) == "completed"
    )
    return {"success": True, "data": task}


# 可被 gateway 代理调用的函数白名单（防止任意函数被调用）
PUBLIC_FUNCTIONS: List[str] = [
    "create_task",
    "update_task",
    "set_status",
    "add_note",
    "link_task",
    "unlink_task",
    "list_tasks",
    "get_task",
    "delete_task",
    "restore_task",
]
