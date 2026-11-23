import json, time
from pathlib import Path
import httpx, pytest
from bitrix_mcp.core import AppError, Client, Settings, markdown_to_bbcode, normalize_date

URL="https://example.bitrix24.ru/rest/1001/secret/"
def settings(tmp_path,**kw):return Settings(webhook_url=URL,audit_log=tmp_path/"audit.jsonl",**kw)
def client(tmp_path,handler,**kw):return Client(settings(tmp_path,**kw),httpx.AsyncClient(transport=httpx.MockTransport(handler)))

@pytest.mark.asyncio
async def test_identity_and_no_secret_in_audit(tmp_path):
    def h(req):return httpx.Response(200,json={"result":{"ID":"1001"}} if req.url.path.endswith("profile.json") else {"result":[]})
    c=client(tmp_path,h);await c.call("user.get",{});await c._http.aclose()
    text=(tmp_path/"audit.jsonl").read_text();assert "secret" not in text and "user.get" in text

@pytest.mark.asyncio
async def test_identity_mismatch(tmp_path):
    c=client(tmp_path,lambda r:httpx.Response(200,json={"result":{"ID":"1"}}))
    with pytest.raises(AppError,match="1001"):await c.call("profile")

@pytest.mark.asyncio
async def test_readonly_cannot_bypass_mutation_flag(tmp_path):
    c=client(tmp_path,lambda r:httpx.Response(200,json={"result":{"ID":"1001"}}),read_only=True)
    with pytest.raises(AppError) as e:await c.call("tasks.task.add",{},mutation=False)
    assert e.value.code=="READ_ONLY"

@pytest.mark.asyncio
async def test_unknown_write_not_retried(tmp_path):
    calls=0
    def h(req):
        nonlocal calls
        if req.url.path.endswith("profile.json"):return httpx.Response(200,json={"result":{"ID":"1001"}})
        calls+=1;raise httpx.ReadTimeout("x",request=req)
    c=client(tmp_path,h,read_only=False)
    with pytest.raises(AppError) as e:await c.call("tasks.task.add",{},mutation=True)
    assert e.value.code=="OUTCOME_UNKNOWN" and calls==1

@pytest.mark.asyncio
async def test_rate_retry_read(tmp_path,monkeypatch):
    calls=0
    async def no_sleep(*a):pass
    monkeypatch.setattr("asyncio.sleep",no_sleep)
    def h(req):
        nonlocal calls
        if req.url.path.endswith("profile.json"):return httpx.Response(200,json={"result":{"ID":"1001"}})
        calls+=1;return httpx.Response(429,json={"error":"QUERY_LIMIT_EXCEEDED"}) if calls==1 else httpx.Response(200,json={"result":[]})
    c=client(tmp_path,h);await c.call("user.get");assert calls==2

@pytest.mark.asyncio
async def test_batch_retries_only_rejected(tmp_path,monkeypatch):
    async def no_sleep(*a): pass
    monkeypatch.setattr("bitrix_mcp.core.asyncio.sleep",no_sleep)
    calls=[]
    def h(req):
        if req.url.path.endswith("profile.json"):return httpx.Response(200,json={"result":{"ID":"1001"}})
        body=req.content.decode();calls.append(body)
        if len(calls)==1:return httpx.Response(200,json={"result":{"result":{"a":1},"result_error":{"b":{"error":"QUERY_LIMIT_EXCEEDED"}}}})
        return httpx.Response(200,json={"result":{"result":{"b":2},"result_error":{}}})
    c=client(tmp_path,h); c._last_request=-99
    r=await c.batch({"a":("user.get",{}),"b":("user.get",{})})
    assert r["successes"]=={"a":1,"b":2} and "a=" not in calls[1] and len(calls)==2

def test_dates_and_config(tmp_path):
    assert normalize_date("2026-01-02T10:00:00+00:00").endswith("+03:00")
    with pytest.raises(AppError):Settings(webhook_url="https://evil/x")
    assert "secret" not in repr(settings(tmp_path))

def test_markdown_fenced_code_is_opaque():
    result=markdown_to_bbcode("```py\n**literal** [x](https://example.test)\n```\n**bold**")
    assert "[code]**literal** [x](https://example.test)\n[/code]" in result
    assert result.endswith("[b]bold[/b]")

@pytest.mark.asyncio
async def test_redirect_is_never_accepted(tmp_path):
    def h(req):
        if req.url.path.endswith("profile.json"):return httpx.Response(200,json={"result":{"ID":"1001"}})
        return httpx.Response(302,headers={"location":"https://example.test/secret"})
    c=client(tmp_path,h)
    with pytest.raises(AppError) as e:await c.call("user.get")
    assert e.value.code=="INVALID_RESPONSE"

@pytest.mark.asyncio
async def test_successful_mutation_survives_audit_failure(tmp_path,monkeypatch):
    def h(req):return httpx.Response(200,json={"result":{"ID":"1001"}} if req.url.path.endswith("profile.json") else {"result":{"task":{"id":7}}})
    c=client(tmp_path,h,read_only=False)
    real=c.audit
    calls=0
    async def broken(event,**metadata):
        nonlocal calls
        calls+=1
        if metadata.get("success"):raise AppError("AUDIT_FAILED","x")
        return await real(event,**metadata)
    monkeypatch.setattr(c,"audit",broken)
    result=await c.call("tasks.task.add",{},mutation=True)
    assert result["result"]["task"]["id"]==7 and result["_local_warning"]["code"]=="AUDIT_FAILED"

@pytest.mark.asyncio
async def test_readonly_denial_is_audited(tmp_path):
    c=client(tmp_path,lambda r:httpx.Response(500),read_only=True)
    with pytest.raises(AppError):await c.call("tasks.task.add",{},mutation=False)
    record=json.loads((tmp_path/"audit.jsonl").read_text())
    assert record["code"]=="READ_ONLY" and record["success"] is False
