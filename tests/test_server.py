import json, pytest
from bitrix_mcp.core import Settings
from bitrix_mcp.server import build_server, create_http_app
from bitrix_mcp.tasks import TaskTools

class Fake:
    def __init__(self,tmp):self.settings=Settings(webhook_url="https://example.bitrix24.ru/rest/1001/secret/",audit_log=tmp/"a")
    async def call(self,m,p=None,mutation=False):return {"result":{"ID":"1001","NAME":"Test"}}
    async def close(self):pass

@pytest.mark.asyncio
async def test_typed_tool_schemas_and_call(tmp_path):
    m=build_server(Fake(tmp_path).settings,Fake(tmp_path));tools=await m.list_tools();names={x.name for x in tools}
    assert {"whoami","task_create","file_download","chat_send_file"}<=names
    create=next(x for x in tools if x.name=="task_create");assert create.inputSchema["properties"]["title"]["type"]=="string" and "title" in create.inputSchema["required"]
    result=await m.call_tool("whoami",{});assert "1001" in str(result)

@pytest.mark.asyncio
async def test_all_mutations_have_dry_run(tmp_path):
    tools=await build_server(Fake(tmp_path).settings,Fake(tmp_path)).list_tools()
    names={"task_create","task_update","task_start","task_complete","task_defer","task_delegate","task_renew","task_delete","checklist_add","checklist_update","checklist_toggle","checklist_delete","checklist_reorder","comment_add","file_upload","task_attach_files","file_download","chat_send","chat_send_file"}
    for t in tools:
        if t.name in names:assert "dry_run" in t.inputSchema["properties"],t.name

@pytest.mark.asyncio
async def test_bearer_and_host_asgi(tmp_path):
    import httpx
    s=Settings(webhook_url="https://example.bitrix24.ru/rest/1001/secret/",audit_log=tmp_path/"a",bearer_token="x"*24)
    app=create_http_app(s,Fake(tmp_path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://localhost") as c:
        r=await c.post("/mcp",headers={"host":"localhost"});assert r.status_code==401
        r=await c.post("/mcp",headers={"host":"evil","authorization":"Bearer "+"x"*24});assert r.status_code==421

@pytest.mark.asyncio
async def test_delegate_resolution_error_is_safe_json(tmp_path):
    class Ambiguous(Fake):
        async def call(self,m,p=None,mutation=False):
            if m=="user.get":return {"result":[{"ID":"1","NAME":"Олег"},{"ID":"2","NAME":"Олег"}]}
            return await super().call(m,p,mutation)
    m=build_server(Ambiguous(tmp_path).settings,Ambiguous(tmp_path))
    result=await m.call_tool("task_delegate",{"task_id":1,"user":"Олег"})
    assert "USER_AMBIGUOUS" in str(result)
