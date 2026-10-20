"""plugin/api.py 单元测试：树状任务组织与任务间链接。

运行：python3 -m pytest tests/ -q
"""
import importlib.util
import json
import os

import pytest

_API_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugin", "api.py"
)


def _load_api():
    spec = importlib.util.spec_from_file_location("autoworker_api_test", _API_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


api = _load_api()


@pytest.fixture()
def workdir(tmp_path):
    return str(tmp_path)


def _create(workdir, title, parent_id=None):
    res = api.create_task(workdir=workdir, title=title, parent_id=parent_id)
    assert res["success"], res
    return res["data"]["id"]


def test_create_child_task(workdir):
    parent = _create(workdir, "父任务")
    child = _create(workdir, "子任务", parent_id=parent)
    data = api.get_task(workdir, child)["data"]
    assert data["parent_id"] == parent
    assert data["links"] == []
    # 子任务目录仍平铺在 workdir 下
    assert os.path.dirname(data["task_dir"]) == workdir


def test_create_child_with_missing_parent(workdir):
    res = api.create_task(workdir=workdir, title="x", parent_id="T999")
    assert not res["success"]
    assert "父任务不存在" in res["error"]


def test_children_derived_from_parent_id(workdir):
    p = _create(workdir, "P")
    c1 = _create(workdir, "C1", parent_id=p)
    c2 = _create(workdir, "C2", parent_id=p)
    g = _create(workdir, "G", parent_id=c1)
    tasks = {t["id"]: t for t in api.list_tasks(workdir)["data"]}
    assert tasks[p]["children"] == [c1, c2]
    assert tasks[c1]["children"] == [g]
    assert tasks[c2]["children"] == []
    assert tasks[p]["parent_id"] is None


def test_tree_view_depth_and_progress(workdir):
    p = _create(workdir, "P")
    c1 = _create(workdir, "C1", parent_id=p)
    _create(workdir, "C2", parent_id=p)
    api.set_status(workdir, c1, "completed")
    data = {t["id"]: t for t in api.list_tasks(workdir, tree=True)["data"]}
    assert data[p]["depth"] == 0
    assert data[c1]["depth"] == 1
    assert data[p]["children_count"] == 2
    assert data[p]["child_done_count"] == 1


def test_update_parent_and_cycle_prevention(workdir):
    p = _create(workdir, "P")
    c = _create(workdir, "C", parent_id=p)
    g = _create(workdir, "G", parent_id=c)
    # 把父任务挂到自己的后代下 -> 拒绝
    res = api.update_task(workdir, p, parent_id=g)
    assert not res["success"]
    assert "循环" in res["error"]
    # 不能把自己设为父任务
    res = api.update_task(workdir, c, parent_id=c)
    assert not res["success"]
    # 移到顶层
    res = api.update_task(workdir, c, parent_id="")
    assert res["success"]
    assert api.get_task(workdir, c)["data"]["parent_id"] is None


def test_link_and_unlink(workdir):
    a = _create(workdir, "A")
    b = _create(workdir, "B")
    res = api.link_task(workdir, a, b, "blocked_by", "依赖 B")
    assert res["success"], res
    links = api.get_task(workdir, a)["data"]["links"]
    assert links == [{"task_id": b, "relation": "blocked_by", "note": "依赖 B"}]

    # 重复关联
    assert not api.link_task(workdir, a, b, "blocked_by")["success"]
    # 关联自身
    assert not api.link_task(workdir, a, a)["success"]
    # 非法 relation
    assert not api.link_task(workdir, a, b, "foo")["success"]
    # 目标不存在
    assert not api.link_task(workdir, a, "T999")["success"]

    res = api.unlink_task(workdir, a, b)
    assert res["success"], res
    assert api.get_task(workdir, a)["data"]["links"] == []
    # 再次解除
    assert not api.unlink_task(workdir, a, b)["success"]


def test_delete_parent_blocked_then_cascade(workdir):
    p = _create(workdir, "P")
    c = _create(workdir, "C", parent_id=p)
    g = _create(workdir, "G", parent_id=c)

    res = api.delete_task(workdir, p)
    assert not res["success"]
    assert "子任务" in res["error"]

    res = api.delete_task(workdir, p, cascade=True)
    assert res["success"], res
    assert set(res["deleted_ids"]) == {p, c, g}
    assert api.list_tasks(workdir)["data"] == []


def test_restore_child_with_deleted_parent_becomes_top(workdir):
    p = _create(workdir, "P")
    c = _create(workdir, "C", parent_id=p)
    api.delete_task(workdir, p, cascade=True)
    res = api.restore_task(workdir, c)
    assert res["success"], res
    assert api.get_task(workdir, c)["data"]["parent_id"] is None


def test_legacy_task_without_new_fields(workdir):
    task_dir = os.path.join(workdir, "T001_old")
    os.makedirs(os.path.join(task_dir, ".jarvis", "autoworker"))
    with open(
        os.path.join(task_dir, ".jarvis", "autoworker", "task.json"),
        "w",
        encoding="utf-8",
    ) as f:
        json.dump({"id": "T001", "title": "old", "status": "pending"}, f)

    tasks = api.list_tasks(workdir)["data"]
    assert len(tasks) == 1
    assert tasks[0]["children"] == []
    # 旧数据可正常更新/关联
    new_id = _create(workdir, "new")
    assert api.link_task(workdir, "T001", new_id)["success"]
    assert api.update_task(workdir, "T001", parent_id=new_id)["success"]


def test_task_manager_tool_actions(workdir, monkeypatch):
    """task_manager 工具层：link_task/unlink_task/parent_id/tree/cascade 参数透传。"""
    import importlib.util as iu

    tool_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "tools",
        "task_manager.py",
    )
    spec = iu.spec_from_file_location("autoworker_task_manager_test", tool_path)
    mod = iu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    tool = mod.TaskManagerTool()

    def run(**args):
        res = tool.execute(args)
        return res, json.loads(res["stdout"]) if res["stdout"] else {}

    res, payload = run(action="create_task", workdir=workdir, title="P")
    assert res["success"], res
    p = payload["data"]["id"]
    res, payload = run(
        action="create_task", workdir=workdir, title="C", parent_id=p
    )
    c = payload["data"]["id"]
    assert payload["data"]["parent_id"] == p

    res, _ = run(action="link_task", workdir=workdir, task_id=p, target_id=c)
    assert res["success"], res
    res, payload = run(action="list_tasks", workdir=workdir, tree=True)
    assert res["success"], res
    assert any(t.get("depth") == 1 for t in payload["data"])

    res, _ = run(action="delete_task", workdir=workdir, task_id=p)
    assert not res["success"]
    res, _ = run(action="delete_task", workdir=workdir, task_id=p, cascade=True)
    assert res["success"], res


def test_completed_at_written_and_cleared(workdir):
    """set_status 切到 completed 写入 completed_at，切出时清空。"""
    tid = _create(workdir, "任务")
    # 初始无 completed_at
    assert "completed_at" not in api.get_task(workdir, tid)["data"]
    # 切到 completed -> 写入
    res = api.set_status(workdir, tid, "completed")
    assert res["success"], res
    data = api.get_task(workdir, tid)["data"]
    assert data["completed_at"]
    # 切出 completed -> 清空
    res = api.set_status(workdir, tid, "pending")
    assert res["success"], res
    assert "completed_at" not in api.get_task(workdir, tid)["data"]
    # 再次切回 completed -> 重新写入
    res = api.set_status(workdir, tid, "completed")
    assert res["success"], res
    assert api.get_task(workdir, tid)["data"]["completed_at"]


def test_list_tasks_sort_by_due_date(workdir):
    """list_tasks sort_by=due_date 按期望完成时间升序，无 due_date 排最后。"""
    a = api.create_task(workdir=workdir, title="A", due_date="2026-10-30")["data"]["id"]
    b = api.create_task(workdir=workdir, title="B", due_date="2026-10-20")["data"]["id"]
    c = api.create_task(workdir=workdir, title="C")["data"]["id"]
    d = api.create_task(workdir=workdir, title="D", due_date="2026-10-25")["data"]["id"]

    ids = [t["id"] for t in api.list_tasks(workdir, sort_by="due_date")["data"]]
    # B(10-20) < D(10-25) < A(10-30) < C(无 due_date 排最后)
    assert ids == [b, d, a, c]
    # 默认（不传 sort_by）仍按 id 升序，向后兼容
    ids_default = [t["id"] for t in api.list_tasks(workdir)["data"]]
    assert ids_default == [a, b, c, d]
