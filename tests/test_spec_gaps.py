import json
import httpx, pytest
from bitrix_mcp.core import AppError, Client, Settings
from bitrix_mcp.server import build_server, create_http_app
from bitrix_mcp.tasks import TaskTools
from bitrix_mcp.extras import ExtraTools

URL = "https://example.bitrix24.ru/rest/1001/secret/"


class Fake:
    def __init__(self, tmp, results=None):
        self.results = list(results or []); self.calls = []
        self.settings = Settings(webhook_url=URL, audit_log=tmp / "a", read_only=False)
    async def call(self, m, p=None, mutation=False):
        self.calls.append((m, p, mutation))
        return self.results.pop(0) if self.results else {"result": True}
    async def close(self): pass


async def test_create_dry_run_shows_fields_with_deadline_flag_and_bbcode(tmp_path):
    f = Fake(tmp_path, [{"result": {"tasks": []}}])
    r = await TaskTools(f).task_create("T", description="[b]x[/b] **y**", allow_change_deadline=True,
                                       task_control=True, description_format="bbcode", dry_run=True)
    assert r["fields"]["ALLOW_CHANGE_DEADLINE"] == "Y" and r["fields"]["TASK_CONTROL"] == "Y"
    assert r["fields"]["DESCRIPTION"] == "[b]x[/b] **y**"
    assert all(c[0] != "tasks.task.add" for c in f.calls)


async def test_create_markdown_is_converted(tmp_path):
    f = Fake(tmp_path, [{"result": {"tasks": []}}])
    r = await TaskTools(f).task_create("T", description="**y**", dry_run=True)
    assert r["fields"]["DESCRIPTION"] == "[b]y[/b]" and r["fields"]["ALLOW_CHANGE_DEADLINE"] == "N"


async def test_update_with_only_format_is_rejected(tmp_path):
    with pytest.raises(AppError) as e:
        await TaskTools(Fake(tmp_path)).task_update(1, description_format="bbcode")
    assert e.value.code == "VALIDATION"


async def test_task_get_dates_moscow_and_checklist(tmp_path):
    task = {"result": {"task": {"id": 5, "title": "x", "status": 2, "deadline": "2026-11-20T12:00:00+00:00", "action": {"edit": True}}}}
    checklist = {"result": [{"ID": "6", "PARENT_ID": 0, "TITLE": "BX_CHECKLIST_1"}, {"ID": "7", "PARENT_ID": "6", "TITLE": "п1", "IS_COMPLETE": "N", "SORT_INDEX": "0"}]}
    r = await TaskTools(Fake(tmp_path, [task, checklist])).task_get(5, with_checklist=True)
    assert r["deadline"] == "2026-11-20T15:00:00+03:00"
    assert r["checklist"] == [{"id": 7, "title": "п1", "done": False, "sort": 0, "group_id": 6}]


async def test_chat_send_file_strips_signed_urls(tmp_path):
    env = {"result": {"MESSAGE_ID": 11, "FILES": {"5249": {"urlDownload": "https://example.bitrix24.ru/x?token=SIGNED"}}}}
    f = Fake(tmp_path, [env]); r = await ExtraTools(f, TaskTools(f)).chat_send_file("sg12", 5249)
    assert r == {"sent": True, "message_id": 11, "file_ids": [5249], "task_url": None}
    assert "SIGNED" not in json.dumps(r)
    with pytest.raises(AppError):
        await ExtraTools(f, TaskTools(f)).chat_send_file("chat12&x=1", 1)


async def test_chat_history_compact_without_urls(tmp_path):
    env = {"result": {"messages": [{"id": 1, "author_id": 3, "date": "2026-11-18T10:00:00+00:00", "text": "hi", "params": {"FILE_ID": ["9"]}}],
                      "users": [{"id": 3, "name": "Полина"}], "files": [{"id": 9, "name": "a.png", "size": 4, "urlDownload": "https://x/SIGNED"}]}}
    f = Fake(tmp_path, [env]); r = await ExtraTools(f, TaskTools(f)).chat_history("chat1", 5)
    assert r["messages"][0]["author"] == "Полина" and r["messages"][0]["date"].endswith("+03:00")
    assert r["messages"][0]["files"] == [{"id": 9, "name": "a.png", "size": 4}] and "SIGNED" not in json.dumps(r)


async def test_every_tool_call_is_audited_without_params(tmp_path):
    def h(req):
        if req.url.path.endswith("profile.json"): return httpx.Response(200, json={"result": {"ID": "1001"}})
        return httpx.Response(200, json={"result": {"tasks": []}})
    s = Settings(webhook_url=URL, audit_log=tmp_path / "audit.jsonl", read_only=False)
    c = Client(s, httpx.AsyncClient(transport=httpx.MockTransport(h)))
    m = build_server(s, c)
    await m.call_tool("chat_send", {"dialog_id": "chat1", "message": "секретный текст", "dry_run": True})
    await m.call_tool("task_create", {"title": "", "dry_run": True})
    await c._http.aclose()
    lines = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines()]
    tools = [x for x in lines if x["event"] == "tool.call"]
    assert tools[0] == {**tools[0], "tool": "chat_send", "success": True, "dry_run": True}
    assert tools[1]["tool"] == "task_create" and tools[1]["code"] == "VALIDATION"
    text = (tmp_path / "audit.jsonl").read_text()
    assert "секретный" not in text and "secret" not in text


async def test_malformed_headers_do_not_crash(tmp_path):
    s = Settings(webhook_url=URL, audit_log=tmp_path / "a", bearer_token="x" * 24)
    app = create_http_app(s, Fake(tmp_path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as c:
        r = await c.post("/mcp", headers={"host": "localhost:8000", "authorization": "Bearer ключ".encode()})
        assert r.status_code == 401
        r = await c.post("/mcp", headers={"host": b"\xff\xfe"})
        assert r.status_code == 421


async def test_rejected_webhook_is_reported_as_such(tmp_path):
    s = Settings(webhook_url=URL, audit_log=tmp_path / "a")
    c = Client(s, httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"error": "invalid_token"}))))
    with pytest.raises(AppError) as e:
        await c.call("tasks.task.get", {"taskId": 1})
    assert e.value.code == "WEBHOOK_INVALID"


async def test_live_shapes_tags_people_remove_permission(tmp_path):
    # Shapes taken from a live portal (read-only).
    task = {"result": {"task": {"id": 5, "title": "x", "status": "6", "tags": {"240": {"id": 240, "title": "M"}},
            "auditors": ["1"], "auditorsData": {"1": {"id": "1", "name": "Пётр Смирнов"}}, "allowChangeDeadline": "Y",
            "action": {"edit": False, "remove": False, "complete": True}}}}
    r = await TaskTools(Fake(tmp_path, [task])).task_get(5)
    assert r["tags"] == ["M"] and r["auditors"] == [{"id": 1, "name": "Пётр Смирнов"}]
    assert r["allowChangeDeadline"] is True and r["permissions"]["canDelete"] is False


async def test_comments_use_task_chat_and_hide_system(tmp_path):
    task = {"result": {"task": {"id": 5, "chatId": "76746"}}}
    hist = {"result": {"messages": [{"id": 3, "author_id": 0, "text": "отложил"}, {"id": 2, "author_id": 1001, "text": "Гарантия"}], "users": [], "files": []}}
    f = Fake(tmp_path, [task, hist]); r = await ExtraTools(f, TaskTools(f)).comment_list(5)
    assert r["source"] == "task_chat" and [c["text"] for c in r["comments"]] == ["Гарантия"]
    assert f.calls[1][0] == "im.dialog.messages.get" and f.calls[1][1]["DIALOG_ID"] == "chat76746"


async def test_comment_add_goes_to_task_chat_with_files(tmp_path):
    f = Fake(tmp_path, [{"result": {"task": {"id": 5, "chatId": "76746"}}}])
    r = await ExtraTools(f, TaskTools(f)).comment_add(5, "Готово", files=[9])
    assert [c[0] for c in f.calls[1:]] == ["tasks.task.chat.message.send", "im.disk.file.commit"]
    assert f.calls[1][1] == {"fields": {"taskId": 5, "text": "Готово"}} and f.calls[2][1]["DIALOG_ID"] == "chat76746"
    assert r["sent"] is True and r["files_sent"] == [9]


async def test_rest_v3_uses_api_segment_and_json(tmp_path):
    seen = []
    def h(req):
        if req.url.path.endswith("profile.json"): return httpx.Response(200, json={"result": {"ID": "1001"}})
        seen.append(req); return httpx.Response(200, json={"result": True})
    s = Settings(webhook_url=URL, audit_log=tmp_path / "a", read_only=False)
    c = Client(s, httpx.AsyncClient(transport=httpx.MockTransport(h)))
    await c.call("tasks.task.chat.message.send", {"fields": {"taskId": 5, "text": "Привет"}}, mutation=True)
    assert seen[0].url.path == "/rest/api/1001/secret/tasks.task.chat.message.send"
    assert json.loads(seen[0].content) == {"fields": {"taskId": 5, "text": "Привет"}}


async def test_batch_empty_error_list_means_success(tmp_path):
    def h(req):
        if req.url.path.endswith("profile.json"): return httpx.Response(200, json={"result": {"ID": "1001"}})
        return httpx.Response(200, json={"result": {"result": {"add0": 1, "add1": 2}, "result_error": [], "result_total": [], "result_next": [], "result_time": []}})
    s = Settings(webhook_url=URL, audit_log=tmp_path / "a", read_only=False)
    c = Client(s, httpx.AsyncClient(transport=httpx.MockTransport(h)))
    r = await c.batch({"add0": ("task.checklistitem.add", {"TASKID": 1}), "add1": ("task.checklistitem.add", {"TASKID": 1})}, mutation=True)
    assert r == {"successes": {"add0": 1, "add1": 2}, "errors": {}}


async def test_checklist_root_is_group_not_item(tmp_path):
    rows = {"result": [{"ID": "1", "PARENT_ID": 0, "TITLE": "BX_CHECKLIST_1"}, {"ID": "3", "PARENT_ID": "1", "TITLE": "b", "SORT_INDEX": "2"}, {"ID": "2", "PARENT_ID": "1", "TITLE": "a", "SORT_INDEX": "1"}]}
    f = Fake(tmp_path, [rows]); r = await ExtraTools(f, TaskTools(f)).checklist_get(5)
    assert r["groups"] == [{"id": 1, "title": "BX_CHECKLIST_1"}] and [i["title"] for i in r["items"]] == ["a", "b"]
    f = Fake(tmp_path, [rows])
    with pytest.raises(AppError):
        await ExtraTools(f, TaskTools(f)).checklist_reorder(5, [1, 2, 3])


async def test_deleted_task_is_not_found(tmp_path):
    for env in ({"result": {"task": False}}, {"result": []}, {"result": {"task": {}}}):
        with pytest.raises(AppError) as e:
            await TaskTools(Fake(tmp_path, [env])).task_get(91112)
        assert e.value.code == "TASK_NOT_FOUND"
