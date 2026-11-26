from __future__ import annotations

import argparse, hmac, json, logging, os
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any, Literal

import uvicorn
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from .core import AppError, Client, Settings
from .extras import ExtraTools
from .tasks import TaskTools

logging.getLogger("httpx").setLevel(logging.WARNING); logging.getLogger("httpcore").setLevel(logging.WARNING)

class ChecklistItem(BaseModel):
    title:str
    files:list[int|str]=Field(default_factory=list,description="ID файлов на Диске или локальные пути внутри MCP_ALLOWED_ROOTS")

def compact(value:Any)->str:return json.dumps(value,ensure_ascii=False,separators=(",",":"),default=str)

def build_server(settings:Settings,client:Client|None=None)->FastMCP:
    owned=client is None; api=client or Client(settings); tasks=TaskTools(api); extra=ExtraTools(api,tasks)
    @asynccontextmanager
    async def lifespan(_:FastMCP):
        try:yield {"client":api}
        finally:
            if owned:await api.close()
    mcp=FastMCP("Bitrix24",lifespan=lifespan,host=settings.host,port=settings.port,json_response=True,stateless_http=True)
    def tool(fn):return mcp.tool()(fn)
    async def log(name:str,**meta:Any):
        # The tool log is best effort: REST mutations already audit on their own.
        try:await api.audit("tool.call",tool=name,**meta)
        except Exception:pass
    async def safe(name:str,awaitable):
        try:result=await awaitable
        except AppError as e:
            await log(name,success=False,code=e.code);return compact({"ok":False,"error":e.as_dict()})
        except Exception as e:
            # Type only: exception text from httpx can contain the webhook URL.
            logging.getLogger("bitrix_mcp").error("tool %s failed: %s",name,type(e).__name__)
            await log(name,success=False,code="INTERNAL_ERROR");return compact({"ok":False,"error":{"code":"INTERNAL_ERROR","message":"Внутренняя ошибка без раскрытия деталей."}})
        await log(name,success=True,dry_run=bool(isinstance(result,dict) and result.get("dry_run")));return compact(result)
    @tool
    async def whoami()->str:return await safe("whoami",tasks.whoami())
    @tool
    async def user_search(query:str,active:bool|None=True,limit:int=50,cursor:int=0)->str:return await safe("user_search",tasks.user_search(query,active,limit,cursor))
    @tool
    async def tag_list(scope:Literal["available","tasks"]="available",scan_limit:int=500)->str:return await safe("tag_list",tasks.tag_list(scope,scan_limit))
    @tool
    async def task_list(status:str|None=None,role:Literal["all","responsible","creator","auditor","accomplice"]="all",user:int|str|None=None,creator:int|str|None=None,tag:str|None=None,deadline_from:str|None=None,deadline_to:str|None=None,search:str|None=None,limit:int=50,cursor:int=0)->str:return await safe("task_list",tasks.task_list(status,role,user,creator,tag,deadline_from,deadline_to,search,limit,cursor))
    @tool
    async def task_get(task_id:int)->str:return await safe("task_get",tasks.task_get(task_id,with_checklist=True))
    @tool
    async def task_create(title:str,description:str="",creator:int|str|None=None,responsible:int|str|None=None,auditors:list[int|str]|None=None,accomplices:list[int|str]|None=None,deadline:str|None=None,tags:list[str]|None=None,task_control:bool=False,match_work_time:bool=False,parent_id:int|None=None,files:list[int]|None=None,allow_change_deadline:bool=False,description_format:Literal["markdown","bbcode"]="markdown",dry_run:bool=False)->str:return await safe("task_create",tasks.task_create(title,description,creator,responsible,auditors,accomplices,deadline,tags,task_control,match_work_time,parent_id,files,dry_run,allow_change_deadline=allow_change_deadline,description_format=description_format))
    @tool
    async def task_update(task_id:int,title:str|None=None,description:str|None=None,creator:int|str|None=None,responsible:int|str|None=None,auditors:list[int|str]|None=None,accomplices:list[int|str]|None=None,deadline:str|None=None,tags:list[str]|None=None,task_control:bool|None=None,match_work_time:bool|None=None,parent_id:int|None=None,files:list[int]|None=None,allow_change_deadline:bool|None=None,description_format:Literal["markdown","bbcode"]="markdown",clear_description:bool=False,clear_deadline:bool=False,clear_auditors:bool=False,clear_accomplices:bool=False,clear_tags:bool=False,clear_files:bool=False,clear_parent:bool=False,dry_run:bool=False)->str:return await safe("task_update",tasks.task_update(task_id,dry_run,title=title,description=description,creator=creator,responsible=responsible,auditors=auditors,accomplices=accomplices,deadline=deadline,tags=tags,task_control=task_control,match_work_time=match_work_time,parent_id=parent_id,files=files,allow_change_deadline=allow_change_deadline,description_format=description_format,clear_description=clear_description,clear_deadline=clear_deadline,clear_auditors=clear_auditors,clear_accomplices=clear_accomplices,clear_tags=clear_tags,clear_files=clear_files,clear_parent=clear_parent))
    async def action(tool_name:str,task_id:int,name:str,dry_run:bool,**params:Any):return await safe(tool_name,tasks._action(task_id,name,dry_run,**params))
    @tool
    async def task_start(task_id:int,dry_run:bool=False)->str:return await action("task_start",task_id,"start",dry_run)
    @tool
    async def task_complete(task_id:int,dry_run:bool=False)->str:return await action("task_complete",task_id,"complete",dry_run)
    @tool
    async def task_defer(task_id:int,dry_run:bool=False)->str:return await action("task_defer",task_id,"defer",dry_run)
    @tool
    async def task_delegate(task_id:int,user:int|str,dry_run:bool=False)->str:
        async def run():
            user_id=await tasks.resolve_user(user)
            return await tasks._action(task_id,"delegate",dry_run,userId=user_id)
        return await safe("task_delegate",run())
    @tool
    async def task_renew(task_id:int,dry_run:bool=False)->str:return await action("task_renew",task_id,"renew",dry_run)
    @tool
    async def task_delete(task_id:int,dry_run:bool=False)->str:return await action("task_delete",task_id,"delete",dry_run)
    @tool
    async def checklist_get(task_id:int)->str:return await safe("checklist_get",extra.checklist_get(task_id))
    @tool
    async def checklist_add(task_id:int,titles:list[str|ChecklistItem],dry_run:bool=False)->str:
        """Добавить пункты. Пункт — строка или {"title": ..., "files": [...]}: файлы (ID на Диске или локальные пути) загружаются и крепятся к пункту в том же вызове."""
        return await safe("checklist_add",extra.checklist_add(task_id,[x.model_dump() if isinstance(x,BaseModel) else x for x in titles],dry_run))
    @tool
    async def checklist_update(task_id:int,item_id:int,title:str,dry_run:bool=False)->str:return await safe("checklist_update",extra.checklist_update(task_id,item_id,title,dry_run))
    @tool
    async def checklist_toggle(task_id:int,item_id:int,complete:bool,dry_run:bool=False)->str:return await safe("checklist_toggle",extra.checklist_toggle(task_id,item_id,complete,dry_run))
    @tool
    async def checklist_delete(task_id:int,item_id:int,dry_run:bool=False)->str:return await safe("checklist_delete",extra.checklist_delete(task_id,item_id,dry_run))
    @tool
    async def checklist_reorder(task_id:int,item_ids:list[int],dry_run:bool=False)->str:return await safe("checklist_reorder",extra.checklist_reorder(task_id,item_ids,dry_run))
    @tool
    async def checklist_attach(task_id:int,item_id:int,files:list[int|str],dry_run:bool=False)->str:
        """Прикрепить файлы к существующему пункту. files: ID файлов на Диске или локальные пути."""
        return await safe("checklist_attach",extra.checklist_attach(task_id,item_id,files,dry_run))
    @tool
    async def comment_list(task_id:int,limit:int=50,cursor:int=0,include_system:bool=False)->str:return await safe("comment_list",extra.comment_list(task_id,limit,cursor,include_system))
    @tool
    async def comment_add(task_id:int,text:str,files:list[int]|None=None,dry_run:bool=False)->str:return await safe("comment_add",extra.comment_add(task_id,text,files,dry_run))
    @tool
    async def file_upload(name:str,path:str|None=None,content_base64:str|None=None,folder_id:int|None=None,dry_run:bool=False)->str:return await safe("file_upload",extra.file_upload(name,path,content_base64,folder_id,dry_run))
    @tool
    async def task_attach_files(task_id:int,file_ids:list[int],dry_run:bool=False)->str:return await safe("task_attach_files",extra.task_attach_files(task_id,file_ids,dry_run))
    @tool
    async def file_download(file_id:int,output_path:str|None=None,return_base64:bool=False,source:Literal["disk","attached"]="disk",dry_run:bool=False)->str:return await safe("file_download",extra.file_download(file_id,output_path,return_base64,dry_run,source))
    @tool
    async def chat_find(title:str,limit:int=50,offset:int=0)->str:return await safe("chat_find",extra.chat_find(title,limit,offset))
    @tool
    async def chat_send(dialog_id:str,message:str,dry_run:bool=False)->str:
        """Отправить сообщение в чат. dialog_id: chat123, sg123, ID пользователя или точное название чата.
        @Имя или @Имя Фамилия превращаются в упоминание [USER=id]: сначала среди участников чата, потом по порталу."""
        return await safe("chat_send",extra.chat_send(dialog_id,message,dry_run))
    @tool
    async def chat_history(dialog_id:str,last_n:int=20)->str:return await safe("chat_history",extra.chat_history(dialog_id,last_n))
    @tool
    async def chat_send_file(dialog_id:str,file_id:int,message:str="",dry_run:bool=False)->str:return await safe("chat_send_file",extra.chat_send_file(dialog_id,file_id,message,dry_run))
    return mcp

class BearerMiddleware:
    def __init__(self,app,token:str|None,allowed_hosts:set[str]):self.app,self.token,self.allowed_hosts=app,token,allowed_hosts
    async def __call__(self,scope,receive,send):
        if scope["type"]=="http":
            headers={k.lower():v for k,v in scope.get("headers",[])}
            # Malformed (non-UTF-8 / non-ASCII) headers must be rejected, not crash with a 500.
            raw_host=headers.get(b"host",b"").decode("latin-1")
            host=raw_host[1:raw_host.find("]")] if raw_host.startswith("[") else raw_host.rsplit(":",1)[0] if raw_host.count(":")==1 else raw_host
            if host not in self.allowed_hosts:return await JSONResponse({"error":"host_not_allowed"},status_code=421)(scope,receive,send)
            if self.token:
                supplied=headers.get(b"authorization",b""); expected=("Bearer "+self.token).encode()
                if not hmac.compare_digest(supplied,expected):return await JSONResponse({"error":"unauthorized"},status_code=401)(scope,receive,send)
        await self.app(scope,receive,send)

def create_http_app(settings:Settings,client:Client|None=None):
    app=build_server(settings,client).streamable_http_app(); hosts={"127.0.0.1","localhost","::1",settings.host}
    return BearerMiddleware(app,settings.bearer_token,hosts)

def load_env_file(path:str)->None:
    # Keeps the webhook out of client configs: they pass only the path to .env.
    for line in Path(path).expanduser().read_text(encoding="utf-8").splitlines():
        key,sep,value=line.strip().partition("=")
        if sep and key and not key.startswith("#"):os.environ.setdefault(key.strip(),value.strip().strip('"').strip("'"))

def main():
    parser=argparse.ArgumentParser();parser.add_argument("--transport",choices=["http","stdio"],default="http");parser.add_argument("--env-file");args=parser.parse_args()
    if args.env_file:load_env_file(args.env_file)
    settings=Settings.from_env();mcp=build_server(settings)
    if args.transport=="stdio":mcp.run(transport="stdio")
    else:uvicorn.run(create_http_app(settings),host=settings.host,port=settings.port,log_level="info")
