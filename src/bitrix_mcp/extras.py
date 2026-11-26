from __future__ import annotations

import base64, binascii, os, re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse
import httpx

from .core import AppError, Client, normalize_date
from .tasks import TaskTools, _items, checklist_view

class ExtraTools:
    def __init__(self, client:Client, tasks:TaskTools): self.client,self.tasks=client,tasks
    async def checklist_get(self,task_id:int)->dict[str,Any]:
        env=await self.client.call("task.checklistitem.getlist",{"TASKID":task_id}); return {**checklist_view(_items(env)),"task_url":self.tasks.task_url(task_id)}
    async def checklist_add(self,task_id:int,titles:list[str],dry_run:bool=False)->dict[str,Any]:
        if not titles or any(not x.strip() for x in titles): raise AppError("VALIDATION","Нужен непустой список пунктов.")
        if dry_run:return {"dry_run":True,"operation":"checklist_add","titles":titles,"task_url":self.tasks.task_url(task_id)}
        commands={f"add{i}":("task.checklistitem.add",{"TASKID":task_id,"FIELDS":{"TITLE":title}}) for i,title in enumerate(titles)}
        result=await self.client.batch(commands,mutation=True); return {**result,"partial":bool(result["errors"]),"task_url":self.tasks.task_url(task_id)}
    async def checklist_update(self,task_id:int,item_id:int,title:str,dry_run:bool=False)->dict[str,Any]:
        return await self._checkwrite("update",task_id,item_id,{"FIELDS":{"TITLE":title}},dry_run)
    async def checklist_toggle(self,task_id:int,item_id:int,complete:bool,dry_run:bool=False)->dict[str,Any]:
        return await self._checkwrite("complete" if complete else "renew",task_id,item_id,{},dry_run)
    async def checklist_delete(self,task_id:int,item_id:int,dry_run:bool=False)->dict[str,Any]: return await self._checkwrite("delete",task_id,item_id,{},dry_run)
    async def _checkwrite(self,action:str,task_id:int,item_id:int,extra:dict[str,Any],dry_run:bool)->dict[str,Any]:
        await self.tasks.task_get(task_id)
        if dry_run:return {"dry_run":True,"operation":"checklist_"+action,"task_id":task_id,"item_id":item_id,"params":extra,"task_url":self.tasks.task_url(task_id)}
        await self.client.call("task.checklistitem."+action,{"TASKID":task_id,"ITEMID":item_id,**extra},mutation=True)
        return {"applied":True,"task_url":self.tasks.task_url(task_id)}
    async def checklist_reorder(self,task_id:int,item_ids:list[int],dry_run:bool=False)->dict[str,Any]:
        current=await self.checklist_get(task_id); owned={x["id"] for x in current["items"]}
        if len(item_ids)!=len(set(item_ids)) or not set(item_ids)<=owned: raise AppError("VALIDATION","Порядок содержит дубли или пункты другой задачи.")
        if dry_run:return {"dry_run":True,"operation":"checklist_reorder","item_ids":item_ids,"task_url":self.tasks.task_url(task_id)}
        moved=[]
        for before,item in zip(item_ids,item_ids[1:]):
            try: await self.client.call("task.checklistitem.moveafteritem",{"TASKID":task_id,"ITEMID":item,"AFTERITEMID":before},mutation=True); moved.append(item)
            except AppError as e:return {"partial":True,"moved":moved,"failed":{"item_id":item,"error":e.as_dict()},"task_url":self.tasks.task_url(task_id)}
        return {"partial":False,"moved":moved,"task_url":self.tasks.task_url(task_id)}
    async def checklist_attach(self,*_:Any,**__:Any)->dict[str,Any]: raise AppError("CAPABILITY_UNVERIFIED","Документированного API вложений пункта чек-листа не найдено.")
    async def _task_chat(self,task_id:int)->int|None:
        # The new task card keeps comments in the task chat; legacy comment API stays empty there.
        env=await self.client.call("tasks.task.get",{"taskId":task_id,"select":["ID","CHAT_ID"]}); raw=env.get("result",{})
        raw=raw.get("task",raw) if isinstance(raw,Mapping) else {}
        chat=str(raw.get("chatId") or raw.get("CHAT_ID") or "")
        return int(chat) if chat.isdigit() and int(chat)>0 else None
    async def comment_list(self,task_id:int,limit:int=50,cursor:int=0,include_system:bool=False)->dict[str,Any]:
        chat=await self._task_chat(task_id)
        if chat:
            page=await self.chat_history(f"chat{chat}",limit,last_id=cursor or None)
            rows=page["messages"]; last=rows[-1]["id"] if rows else None
            comments=[{"id":m["id"],"text":m["text"],"author_id":m["author_id"],"author":m["author"],"date":m["date"],"files":m["files"]} for m in rows if include_system or m["author_id"]]
            return {"comments":comments,"next":last if len(rows)==min(limit,50) else None,"source":"task_chat","task_url":self.tasks.task_url(task_id)}
        env=await self.client.call("task.commentitem.getlist",{"TASKID":task_id,"ORDER":{"POST_DATE":"DESC"},"FILTER":{},"start":cursor}); rows=_items(env)[:limit]
        return {"comments":[{"id":r.get("ID"),"text":r.get("POST_MESSAGE"),"author_id":r.get("AUTHOR_ID"),"date":normalize_date(r.get("POST_DATE")) if r.get("POST_DATE") else None,"files":r.get("ATTACHED_OBJECTS") or r.get("UF_FORUM_MESSAGE_DOC") or []} for r in rows],"next":env.get("next"),"source":"legacy","task_url":self.tasks.task_url(task_id)}
    async def comment_add(self,task_id:int,text:str,files:list[int]|None=None,dry_run:bool=False)->dict[str,Any]:
        if not text.strip():raise AppError("VALIDATION","Текст комментария обязателен.")
        chat=await self._task_chat(task_id)
        if dry_run:return {"dry_run":True,"operation":"comment_add","task_id":task_id,"text":text,"files":files or [],"target":"task_chat" if chat else "legacy","task_url":self.tasks.task_url(task_id)}
        if chat:
            await self.client.call("tasks.task.chat.message.send",{"fields":{"taskId":task_id,"text":text}},mutation=True)
            sent_files=[]
            for fid in files or []:
                try: await self.client.call("im.disk.file.commit",{"DIALOG_ID":f"chat{chat}","FILE_ID":[int(fid)]},mutation=True); sent_files.append(int(fid))
                except AppError as e: return {"sent":True,"files_sent":sent_files,"failed_file":{"id":int(fid),"error":e.as_dict()},"partial":True,"task_url":self.tasks.task_url(task_id)}
            return {"sent":True,"files_sent":sent_files,"task_url":self.tasks.task_url(task_id)}
        fields={"POST_MESSAGE":text}
        if files: fields["UF_FORUM_MESSAGE_DOC"]=[f"n{int(x)}" for x in files]
        env=await self.client.call("task.commentitem.add",{"TASKID":task_id,"FIELDS":fields},mutation=True)
        return {"id":env.get("result"),"task_url":self.tasks.task_url(task_id)}
    def _safe_path(self,path:str,write:bool=False)->Path:
        candidate=Path(path).expanduser().resolve(strict=not write)
        if not any(candidate==root or root in candidate.parents for root in self.client.settings.allowed_roots):raise AppError("PATH_DENIED","Путь вне разрешённых каталогов.")
        return candidate
    async def file_upload(self,name:str,path:str|None=None,content_base64:str|None=None,folder_id:int|None=None,dry_run:bool=False)->dict[str,Any]:
        if (path is None)==(content_base64 is None):raise AppError("VALIDATION","Укажите ровно один источник: path или base64.")
        if not name.strip() or Path(name).name!=name:raise AppError("VALIDATION","Имя файла должно быть непустым и без компонентов пути.")
        folder=folder_id or self.client.settings.disk_folder_id
        if folder is None:raise AppError("CONFIG_INVALID","Не задан B24_DISK_FOLDER_ID.")
        cap=self.client.settings.max_file_bytes
        if path:
            source=self._safe_path(path); st=source.stat()
            if not source.is_file() or st.st_size>cap:raise AppError("FILE_LIMIT","Файл не является обычным или превышает лимит.")
            if dry_run:return {"dry_run":True,"operation":"file_upload","name":name,"size":st.st_size,"task_url":None}
            data=await __import__('asyncio').to_thread(source.read_bytes)
            if len(data)>cap:raise AppError("FILE_LIMIT","Файл изменился и превышает лимит.")
        else:
            encoded=content_base64 or ""
            if len(encoded)>4*((cap+2)//3)+4:raise AppError("FILE_LIMIT","Base64 превышает лимит до декодирования.")
            try:data=base64.b64decode(encoded,validate=True)
            except binascii.Error:raise AppError("VALIDATION","Некорректный base64.") from None
            if len(data)>cap:raise AppError("FILE_LIMIT","Файл превышает лимит.")
            if dry_run:return {"dry_run":True,"operation":"file_upload","name":name,"size":len(data),"task_url":None}
        env=await self.client.call("disk.folder.uploadfile",{"id":folder,"data":{"NAME":name},"fileContent":[name,base64.b64encode(data).decode()]},mutation=True); result=env.get("result",{})
        return {"id":int(result.get("ID") or result.get("id")),"name":name,"task_url":None}
    async def task_attach_files(self,task_id:int,file_ids:list[int],dry_run:bool=False)->dict[str,Any]:
        if dry_run:return {"dry_run":True,"operation":"task_attach_files","file_ids":file_ids,"task_url":self.tasks.task_url(task_id)}
        commands={f"file{i}":("tasks.task.files.attach",{"taskId":task_id,"fileId":fid}) for i,fid in enumerate(file_ids)}
        result=await self.client.batch(commands,mutation=True);return {**result,"partial":bool(result["errors"]),"task_url":self.tasks.task_url(task_id),"warning":"Использован атомарный attach; существующие вложения сохранены."}
    async def file_download(self,file_id:int,output_path:str|None=None,return_base64:bool=False,dry_run:bool=False,source:str="disk")->dict[str,Any]:
        if bool(output_path)==bool(return_base64):raise AppError("VALIDATION","Выберите ровно один результат: output_path или base64.")
        if source not in {"disk","attached"}:raise AppError("VALIDATION","source должен быть disk или attached.")
        meta=(await self.client.call("disk.attachedObject.get" if source=="attached" else "disk.file.get",{"id":file_id})).get("result",{}); url=meta.get("DOWNLOAD_URL")
        parsed=urlparse(str(url));
        if parsed.scheme!="https" or parsed.hostname!=self.client.settings.portal_host:raise AppError("UNSAFE_URL","Недопустимый адрес загрузки файла.")
        if output_path:
            target=self._safe_path(output_path,write=True)
            if target.exists():raise AppError("FILE_EXISTS","Файл назначения уже существует.")
            if dry_run:return {"dry_run":True,"operation":"file_download","file_id":file_id,"output_path":str(target),"task_url":None}
        if self.client.settings.read_only and output_path:raise AppError("READ_ONLY","Локальная запись запрещена режимом только для чтения.")
        async with httpx.AsyncClient(follow_redirects=False,timeout=30) as http:
            async with http.stream("GET",url) as response:
                if response.status_code!=200:raise AppError("DOWNLOAD_FAILED","Не удалось скачать файл.")
                chunks=[]; size=0
                async for chunk in response.aiter_bytes():
                    size+=len(chunk)
                    if size>self.client.settings.max_file_bytes:raise AppError("FILE_LIMIT","Файл превышает лимит.")
                    chunks.append(chunk)
        data=b"".join(chunks)
        if return_base64:return {"id":file_id,"content_base64":base64.b64encode(data).decode(),"size":len(data),"task_url":None}
        target.parent.mkdir(parents=True,exist_ok=True); fd=os.open(target,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,"wb") as out:out.write(data)
        return {"id":file_id,"output_path":str(target),"size":len(data),"task_url":None}
    async def chat_find(self,title:str,limit:int=50,offset:int=0)->dict[str,Any]:
        env=await self.client.call("im.search.chat.list",{"FIND":title,"OFFSET":offset,"LIMIT":min(limit,50)}); rows=_items(env)
        exact=[r for r in rows if str(r.get("name") or r.get("NAME")).casefold()==title.casefold()]
        return {"chats":[{"id":r.get("id") or r.get("ID"),"dialog_id":f"chat{r.get('id') or r.get('ID')}","name":r.get("name") or r.get("NAME"),"type":r.get("type") or r.get("TYPE")} for r in (exact or rows)],"next":offset+len(rows) if len(rows)==min(limit,50) else None,"task_url":None}
    async def _dialog(self,target:str)->str:
        # A dialog id (chat123, sg123, user id) passes as is; anything else is an exact chat title.
        raw=str(target).strip()
        if re.fullmatch(r"(chat|sg)?[1-9][0-9]*",raw):return raw
        if not raw:raise AppError("VALIDATION","Укажите dialog_id или название чата.")
        found=(await self.chat_find(raw))["chats"]
        exact=[c for c in found if str(c["name"]).strip().casefold()==raw.casefold()]
        if len(exact)!=1:
            candidates=[{"dialog_id":c["dialog_id"],"name":c["name"]} for c in (exact or found)[:10]]
            raise AppError("CHAT_AMBIGUOUS" if exact else "CHAT_NOT_FOUND","Чат с таким названием не найден однозначно; уточните название или передайте dialog_id.",{"candidates":candidates})
        return exact[0]["dialog_id"]
    async def chat_send(self,dialog_id:str,message:str,dry_run:bool=False)->dict[str,Any]:
        target=await self._dialog(dialog_id)
        if dry_run:return {"dry_run":True,"operation":"chat_send","dialog_id":target,"message":message,"task_url":None}
        env=await self.client.call("im.message.add",{"DIALOG_ID":target,"MESSAGE":message},mutation=True);return {"message_id":env.get("result"),"dialog_id":target,"task_url":None}
    async def chat_history(self,dialog_id:str,last_n:int=20,last_id:int|None=None)->dict[str,Any]:
        params:dict[str,Any]={"DIALOG_ID":dialog_id,"LIMIT":min(last_n,50)}
        if last_id: params["LAST_ID"]=last_id
        env=await self.client.call("im.dialog.messages.get",params); result=env.get("result",{})
        result=result if isinstance(result,Mapping) else {}
        users={int(u["id"]):u.get("name") for u in result.get("users",[]) if isinstance(u,Mapping) and str(u.get("id","")).isdigit()}
        files={int(f["id"]):{"id":int(f["id"]),"name":f.get("name"),"size":f.get("size")} for f in result.get("files",[]) if isinstance(f,Mapping) and str(f.get("id","")).isdigit()}
        messages=[]
        for m in result.get("messages",[]):
            if not isinstance(m,Mapping): continue
            params=m.get("params") if isinstance(m.get("params"),Mapping) else {}
            ids=[int(x) for x in params.get("FILE_ID") or [] if str(x).isdigit()]
            author=int(m.get("author_id") or 0)
            messages.append({"id":m.get("id"),"author_id":author,"author":users.get(author),"date":normalize_date(m.get("date")) if m.get("date") else None,"text":m.get("text"),"files":[files.get(i,{"id":i}) for i in ids]})
        # Bitrix returns newest first; signed download URLs are intentionally dropped.
        return {"messages":messages,"task_url":None}
    async def chat_send_file(self,dialog_id:str,file_id:int,message:str="",dry_run:bool=False)->dict[str,Any]:
        # im.disk.file.commit accepts DIALOG_ID as chatXXX, sgXXX or a user id.
        raw=str(dialog_id).strip()
        if not re.fullmatch(r"(chat|sg)?[1-9][0-9]*",raw):
            raise AppError("VALIDATION","dialog_id должен быть вида chat123, sg123 или ID пользователя.")
        if dry_run:return {"dry_run":True,"operation":"chat_send_file","dialog_id":raw,"file_id":file_id,"message":message,"task_url":None}
        env=await self.client.call("im.disk.file.commit",{"DIALOG_ID":raw,"FILE_ID":[file_id],"MESSAGE":message},mutation=True)
        # The raw result carries signed download/preview URLs; return ids only.
        result=env.get("result") if isinstance(env.get("result"),Mapping) else {}
        return {"sent":True,"message_id":result.get("MESSAGE_ID"),"file_ids":[int(x) for x in (result.get("FILES") or {}) if str(x).isdigit()],"task_url":None}
