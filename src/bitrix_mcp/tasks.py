from __future__ import annotations

from typing import Any, Literal, Mapping

from .core import AppError, Client, markdown_to_bbcode, normalize_date

def _items(env: Mapping[str, Any]) -> list[dict[str, Any]]:
    value = env.get("result", env)
    if isinstance(value, Mapping):
        value = value.get("tasks", value.get("users", value.get("items", value)))
    if isinstance(value, list):
        return [dict(x) for x in value if isinstance(x, Mapping)]
    return []

def _date(value: Any) -> str | None:
    try: return normalize_date(value) if value else None
    except AppError: return None

def _tags(value: Any) -> list[str]:
    # tasks.task.* returns tags as {"240": {"id": 240, "title": "M"}}; older forms are plain lists.
    if isinstance(value, Mapping): value = list(value.values())
    out = []
    for tag in value or []:
        title = tag.get("title") or tag.get("NAME") if isinstance(tag, Mapping) else tag
        if title: out.append(str(title))
    return out

def checklist_view(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    # The new task card adds a root item per checklist (PARENT_ID 0, e.g. BX_CHECKLIST_1); real items are its children.
    groups = [{"id": int(r["ID"]), "title": r.get("TITLE")} for r in rows if r.get("ID") and str(r.get("PARENT_ID") or 0) == "0"]
    items = [{"id": int(r["ID"]), "title": r.get("TITLE"), "done": _bool(r.get("IS_COMPLETE")), "sort": int(r.get("SORT_INDEX") or 0), "group_id": int(r["PARENT_ID"])}
             for r in rows if r.get("ID") and str(r.get("PARENT_ID") or 0) != "0"]
    return {"groups": groups, "items": sorted(items, key=lambda x: (x["group_id"], x["sort"]))}

def checklist_progress(items: list[Any]) -> str | None:
    # "10/13": done items out of all, None when the task has no checklist.
    if not items: return None
    return f"{sum(1 for x in items if isinstance(x, Mapping) and x.get('done'))}/{len(items)}"

def _bool(value: Any) -> bool | None:
    if value in (True, 1, "1", "Y", "y", "true", "TRUE"): return True
    if value in (False, 0, "0", "N", "n", "false", "FALSE"): return False
    return None

def permissions(raw: Any) -> dict[str, bool | None]:
    aliases = {"edit":"canEdit", "complete":"canComplete", "delete":"canDelete", "remove":"canDelete", "delegate":"canDelegate", "start":"canStart", "defer":"canDefer", "renew":"canRenew"}
    out = {v: None for v in aliases.values()}
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            compact = str(key).replace("_", "").lower().removeprefix("can")
            for source, target in aliases.items():
                if compact == source: out[target] = _bool(value)
    elif isinstance(raw, list):
        for name in raw:
            compact = str(name).replace("_", "").lower().removeprefix("can")
            for source, target in aliases.items():
                if compact == source: out[target] = True
    return out

class TaskTools:
    def __init__(self, client: Client): self.client = client

    @property
    def user_id(self) -> int: return self.client.settings.user_id

    def task_url(self, task_id: int | str) -> str:
        return f"{self.client.settings.portal}/company/personal/user/{self.user_id}/tasks/task/view/{int(task_id)}/"

    async def whoami(self) -> dict[str, Any]:
        env = await self.client.call("profile")
        row = env.get("result", {})
        if str(row.get("ID")) != str(self.user_id): raise AppError("IDENTITY_MISMATCH", f"Вебхук должен принадлежать пользователю {self.user_id}.")
        return {"id":self.user_id, "name":" ".join(filter(None,[row.get("NAME"),row.get("LAST_NAME")])), "admin":_bool(row.get("ADMIN"))}

    async def user_search(self, query: str, active: bool | None=True, limit: int=50, cursor: int=0) -> dict[str, Any]:
        if not query.strip(): raise AppError("VALIDATION", "Укажите имя или ID пользователя.")
        found=[]; start=max(0,cursor); pages=0; next_value=None
        while len(found)<limit and pages<20:
            params={"FILTER":{"ACTIVE":"Y" if active else "N"} if active is not None else {},"start":start}
            if query.isdigit(): params["ID"]=int(query)
            else: params["FILTER"]["FIND"]=query
            env=await self.client.call("user.get",params); rows=_items(env)
            for u in rows:
                name=" ".join(filter(None,[u.get("NAME"),u.get("LAST_NAME")])).strip()
                if query.isdigit() or query.casefold() in (name+" "+str(u.get("WORK_POSITION", ""))).casefold():
                    found.append({"id":int(u["ID"]),"name":name,"position":u.get("WORK_POSITION"),"active":_bool(u.get("ACTIVE"))})
            next_value=env.get("next"); pages+=1
            if next_value is None: break
            start=int(next_value)
        return {"users":found[:limit],"next":next_value,"partial":next_value is not None}

    async def resolve_user(self, value: int | str) -> int:
        if isinstance(value,int) or str(value).isdigit(): return int(value)
        result=await self.user_search(str(value),active=True,limit=20)
        exact=[u for u in result["users"] if u["name"].casefold()==str(value).strip().casefold()]
        candidates=exact or result["users"]
        if len(candidates)!=1:
            raise AppError("USER_AMBIGUOUS" if candidates else "USER_NOT_FOUND", "Пользователь не найден однозначно; уточните ID.", {"candidates":candidates})
        return candidates[0]["id"]

    async def tag_list(self, scope: Literal["available","tasks"]="available", scan_limit: int=500) -> dict[str, Any]:
        tags=set(); start=0; scanned=0; nxt=None
        while scanned<scan_limit:
            env=await self.client.call("tasks.task.list",{"select":["ID","TAGS"],"start":start})
            rows=_items(env); scanned+=len(rows)
            for row in rows:
                tags.update(_tags(row.get("tags") or row.get("TAGS")))
            nxt=env.get("next")
            if nxt is None: break
            start=int(nxt)
        return {"tags":sorted(tags,key=str.casefold),"scope":scope,"partial":nxt is not None,"scanned":scanned}

    async def task_list(self, status: str|None=None, role: str="all", user: int|str|None=None, creator: int|str|None=None, tag: str|None=None, deadline_from: str|None=None, deadline_to: str|None=None, search: str|None=None, limit: int=50, cursor: int=0) -> dict[str,Any]:
        if not 1 <= limit <= 500 or cursor < 0: raise AppError("VALIDATION","limit должен быть от 1 до 500, cursor — неотрицательным.")
        f:dict[str,Any]={}
        if status: f["STATUS"]=status
        uid=await self.resolve_user(user) if user is not None else self.user_id
        role_fields={"responsible":"RESPONSIBLE_ID","creator":"CREATED_BY","auditor":"AUDITOR","accomplice":"ACCOMPLICE"}
        if role != "all":
            if role not in role_fields: raise AppError("VALIDATION","Неизвестная роль.")
            f[role_fields[role]]=uid
        if creator is not None: f["CREATED_BY"]=await self.resolve_user(creator)
        if tag: f["TAG"]=tag
        if deadline_from: f[">=DEADLINE"]=normalize_date(deadline_from)
        if deadline_to: f["<=DEADLINE"]=normalize_date(deadline_to)
        if search: f["%TITLE"]=search
        rows:list[dict[str,Any]]=[]; start=cursor; next_value:Any=cursor; total=None
        # The legacy endpoint returns at most 50 rows per response.  Follow
        # `next` until the requested logical page is full; never silently
        # discard a server cursor.
        while len(rows)<limit and next_value is not None:
            env=await self.client.call("tasks.task.list",{"filter":f,"select":["*","UF_*","TAGS"],"start":start})
            page=_items(env); take=min(len(page),limit-len(rows)); rows.extend(page[:take])
            total=env.get("total",total); next_value=env.get("next")
            if take < len(page):
                next_value=start+take
                break
            if next_value is None or not page: break
            start=int(next_value)
        out=[self._task_view(x) for x in rows]
        await self._attach_checklists([t for t in out if not t["checklist"]])
        for t in out: t["checklist_progress"]=None if t.get("checklist_unavailable") else checklist_progress(t["checklist"])
        return {"tasks":out,"next":next_value,"total":total}

    async def _attach_checklists(self, tasks:list[dict[str,Any]])->None:
        # tasks.task.list never includes checklists, so fetch them with batch (50 commands per call).
        for i in range(0,len(tasks),50):
            chunk=tasks[i:i+50]
            commands={f"t{t['id']}":("task.checklistitem.getlist",{"TASKID":t["id"]}) for t in chunk}
            try: result=await self.client.batch(commands)
            except AppError: result={"successes":{},"errors":commands}
            for t in chunk:
                key=f"t{t['id']}"
                if key in result["successes"]: t["checklist"]=checklist_view(_items({"result":result["successes"][key]}))["items"]
                else: t["checklist_unavailable"]=True

    def _task_view(self,row:Mapping[str,Any])->dict[str,Any]:
        def person(v:Any):
            if isinstance(v,Mapping): return {"id":int(v.get("id") or v.get("ID")),"name":v.get("name") or v.get("NAME")}
            return v
        def people(ids:Any,data:Any):
            data=data if isinstance(data,Mapping) else {}
            return [{"id":int(i),"name":(data.get(str(i)) or {}).get("name")} for i in ids or [] if str(i).isdigit()]
        tid=int(row.get("id") or row.get("ID"))
        return {"id":tid,"title":row.get("title") or row.get("TITLE"),"description":row.get("description") or row.get("DESCRIPTION"),"creator":person(row.get("creator") or row.get("CREATED_BY")),"responsible":person(row.get("responsible") or row.get("RESPONSIBLE_ID")),"auditors":people(row.get("auditors") or row.get("AUDITORS"),row.get("auditorsData")),"accomplices":people(row.get("accomplices") or row.get("ACCOMPLICES"),row.get("accomplicesData")),"tags":_tags(row.get("tags") or row.get("TAGS")),"deadline":_date(row.get("deadline") or row.get("DEADLINE")),"created":_date(row.get("createdDate") or row.get("CREATED_DATE")),"changed":_date(row.get("changedDate") or row.get("CHANGED_DATE")),"status":int(row.get("status") or row.get("STATUS") or 0),"taskControl":_bool(row.get("taskControl") or row.get("TASK_CONTROL")),"matchWorkTime":_bool(row.get("matchWorkTime") or row.get("MATCH_WORK_TIME")),"allowChangeDeadline":_bool(row.get("allowChangeDeadline") or row.get("ALLOW_CHANGE_DEADLINE")),"checklist":row.get("checkList") or row.get("CHECKLIST") or [],"files":row.get("ufTaskWebdavFiles") or row.get("UF_TASK_WEBDAV_FILES") or [],"task_url":self.task_url(tid)}

    async def task_get(self, task_id:int, with_checklist:bool=False)->dict[str,Any]:
        env=await self.client.call("tasks.task.get",{"taskId":task_id,"select":["*","UF_*","TAGS"]}); raw=env.get("result",{}); raw=raw.get("task",raw) if isinstance(raw,Mapping) else {}
        if not isinstance(raw,Mapping) or not (raw.get("id") or raw.get("ID")):
            raise AppError("TASK_NOT_FOUND",f"Задача {task_id} не найдена: она удалена или у вас нет к ней доступа.")
        actions=raw.get("action") or raw.get("ACTION")
        if actions is None:
            try: actions=(await self.client.call("task.item.getallowedactions",{"TASKID":task_id})).get("result")
            except AppError: actions=None
        out=self._task_view(raw); out["permissions"]=permissions(actions)
        if with_checklist and not out["checklist"]:
            try: out["checklist"]=checklist_view(_items(await self.client.call("task.checklistitem.getlist",{"TASKID":task_id})))["items"]
            except AppError: out["checklist_unavailable"]=True
        if with_checklist: out["checklist_progress"]=None if out.get("checklist_unavailable") else checklist_progress(out["checklist"])
        return out

    async def _fields(self, **values:Any)->dict[str,Any]:
        mapping={"title":"TITLE","description":"DESCRIPTION","deadline":"DEADLINE","tags":"TAGS","task_control":"TASK_CONTROL","match_work_time":"MATCH_WORK_TIME","allow_change_deadline":"ALLOW_CHANGE_DEADLINE","parent_id":"PARENT_ID","files":"UF_TASK_WEBDAV_FILES"}
        out={mapping[k]:v for k,v in values.items() if k in mapping and v is not None}
        if "description" in values and values["description"] is not None: out["DESCRIPTION"]=values["description"] if values.get("description_format")=="bbcode" else markdown_to_bbcode(values["description"])
        if values.get("deadline") is not None: out["DEADLINE"]=normalize_date(values["deadline"]) if values["deadline"] != "" else ""
        for source,target in (("creator","CREATED_BY"),("responsible","RESPONSIBLE_ID")):
            if values.get(source) is not None: out[target]=await self.resolve_user(values[source])
        for source,target in (("auditors","AUDITORS"),("accomplices","ACCOMPLICES")):
            if values.get(source) is not None: out[target]=[await self.resolve_user(x) for x in values[source]]
        for key in ("TASK_CONTROL","MATCH_WORK_TIME","ALLOW_CHANGE_DEADLINE"):
            if key in out: out[key]="Y" if out[key] else "N"
        if "files" in values and values["files"] is not None: out["UF_TASK_WEBDAV_FILES"]=[f"n{int(str(x).removeprefix('n'))}" for x in values["files"]]
        return out

    async def task_create(self,title:str,description:str="",creator:int|str|None=None,responsible:int|str|None=None,auditors:list[int|str]|None=None,accomplices:list[int|str]|None=None,deadline:str|None=None,tags:list[str]|None=None,task_control:bool=False,match_work_time:bool=False,parent_id:int|None=None,files:list[int]|None=None,dry_run:bool=False,allow_change_deadline:bool=False,description_format:str="markdown")->dict[str,Any]:
        if not title.strip(): raise AppError("VALIDATION","Название задачи обязательно.")
        fields=await self._fields(title=title,description=description,creator=creator or self.user_id,responsible=self.user_id if responsible is None else responsible,auditors=auditors or [],accomplices=accomplices or [],deadline=deadline,tags=tags or [],task_control=task_control,match_work_time=match_work_time,parent_id=parent_id,files=files or [],allow_change_deadline=allow_change_deadline,description_format=description_format)
        duplicates=[]; start=0; incomplete=False
        for _ in range(10):
            env=await self.client.call("tasks.task.list",{"filter":{"=TITLE":title},"select":["ID","TITLE","STATUS"],"start":start})
            for r in _items(env):
                if str(r.get("title") or r.get("TITLE"))==title and int(r.get("status") or r.get("STATUS") or 0) not in {5,7}: duplicates.append({"id":int(r.get("id") or r.get("ID")),"status":int(r.get("status") or r.get("STATUS")),"task_url":self.task_url(r.get("id") or r.get("ID"))})
            if env.get("next") is None: break
            start=int(env["next"])
        else: incomplete=True
        preview={"operation":"task_create","title":title,"fields":fields,"duplicate_candidates":duplicates,"duplicate_scan_incomplete":incomplete}
        if dry_run: return {"dry_run":True,**preview}
        env=await self.client.call("tasks.task.add",{"fields":fields},mutation=True); result=env.get("result",{}); tid=int(result.get("task",{}).get("id") if isinstance(result,Mapping) and isinstance(result.get("task"),Mapping) else result.get("taskId") if isinstance(result,Mapping) else result)
        return {"id":tid,"task_url":self.task_url(tid),"duplicate_candidates":duplicates,"warnings":["Проверка дублей была неполной"] if incomplete else []}

    async def task_update(self,task_id:int,dry_run:bool=False,**changes:Any)->dict[str,Any]:
        clear_fields={
            "clear_deadline":("deadline",""), "clear_auditors":("auditors",[]),
            "clear_accomplices":("accomplices",[]), "clear_tags":("tags",[]),
            "clear_files":("files",[]), "clear_parent":("parent_id",0),
            "clear_description":("description",""),
        }
        for flag,(field,value) in clear_fields.items():
            if changes.pop(flag,False):
                if changes.get(field) is not None: raise AppError("VALIDATION",f"Нельзя одновременно задать {field} и {flag}.")
                changes[field]=value
        description_format=changes.pop("description_format",None) or "markdown"
        changes={k:v for k,v in changes.items() if v is not None}
        if not changes: raise AppError("VALIDATION","Нет полей для изменения.")
        current=await self.task_get(task_id)
        if current["permissions"]["canEdit"] is False: raise AppError("PERMISSION_DENIED","Нет прав на редактирование этой задачи: изменять её может постановщик или администратор.")
        fields=await self._fields(**changes,description_format=description_format)
        if dry_run:return {"dry_run":True,"operation":"task_update","task_id":task_id,"fields":fields,"task_url":self.task_url(task_id)}
        await self.client.call("tasks.task.update",{"taskId":task_id,"fields":fields},mutation=True)
        try:return {"applied":True,"task":await self.task_get(task_id),"task_url":self.task_url(task_id)}
        except AppError as e:return {"applied":True,"readback":False,"warning":e.message,"task_url":self.task_url(task_id)}

    async def _action(self,task_id:int,action:str,dry_run:bool=False,**params:Any)->dict[str,Any]:
        current=await self.task_get(task_id); perm="can"+action.capitalize()
        if action=="delete" and current["permissions"].get(perm) is not True: raise AppError("PERMISSION_DENIED","Удаление разрешено только при явном canDelete=true.")
        if action!="delete" and current["permissions"].get(perm) is False: raise AppError("PERMISSION_DENIED","Действие запрещено доступными правами задачи.")
        if dry_run:return {"dry_run":True,"operation":"task_"+action,"task_id":task_id,"task_url":self.task_url(task_id)}
        await self.client.call("tasks.task."+action,{"taskId":task_id,**params},mutation=True)
        if action=="delete": return {"deleted":True,"task_id":task_id,"task_url":self.task_url(task_id)}
        try:
            task=await self.task_get(task_id); return {"applied":True,"status":task["status"],"task":task,"task_url":self.task_url(task_id)}
        except AppError as e:return {"applied":True,"readback":False,"warning":e.message,"task_url":self.task_url(task_id)}
