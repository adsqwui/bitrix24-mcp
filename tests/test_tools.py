import base64
from pathlib import Path
import pytest
from bitrix_mcp.core import AppError, Settings
from bitrix_mcp.tasks import TaskTools, permissions
from bitrix_mcp.extras import ExtraTools

class Fake:
    def __init__(self,tmp_path,results=None,read_only=False):self.results=list(results or []);self.calls=[];self.settings=Settings(webhook_url="https://example.bitrix24.ru/rest/1001/secret/",audit_log=tmp_path/"a",allowed_roots=(tmp_path,),read_only=read_only,disk_folder_id=2,max_file_bytes=8)
    async def call(self,m,p=None,mutation=False):self.calls.append((m,p,mutation));return self.results.pop(0) if self.results else {"result":True}
    async def batch(self,c,mutation=False):self.calls.append(("batch",c,mutation));return {"successes":{"add0":1},"errors":{"add1":{"code":"X"}}}

@pytest.mark.asyncio
async def test_ambiguous_users(tmp_path):
    f=Fake(tmp_path,[{"result":[{"ID":"1","NAME":"Иван","LAST_NAME":"Иванов","ACTIVE":"Y"},{"ID":"2","NAME":"Иван","LAST_NAME":"Иванов","ACTIVE":"Y"}]}]);t=TaskTools(f)
    with pytest.raises(AppError) as e:await t.resolve_user("Иван Иванов")
    assert e.value.code=="USER_AMBIGUOUS" and len(e.value.details["candidates"])==2

@pytest.mark.asyncio
async def test_user_paging(tmp_path):
    f=Fake(tmp_path,[{"result":[{"ID":"1","NAME":"Олег"}],"next":50},{"result":[{"ID":"2","NAME":"Олег"}]}]);r=await TaskTools(f).user_search("Олег")
    assert len(r["users"])==2 and f.calls[1][1]["start"]==50

@pytest.mark.asyncio
async def test_duplicates_exact_deferred_open_and_dryrun(tmp_path):
    f=Fake(tmp_path,[{"result":{"tasks":[{"id":1,"title":"X","status":6},{"id":2,"title":"x","status":2},{"id":3,"title":"X","status":5}]}}]);r=await TaskTools(f).task_create("X",dry_run=True)
    assert [x["id"] for x in r["duplicate_candidates"]]==[1] and all(c[0]!="tasks.task.add" for c in f.calls)

@pytest.mark.asyncio
async def test_permission_update_denied(tmp_path):
    raw={"result":{"task":{"id":9,"title":"x","status":2,"action":{"edit":"N","delete":"N"}}}}
    t=TaskTools(Fake(tmp_path,[raw]))
    with pytest.raises(AppError) as e:await t.task_update(9,title="y")
    assert e.value.code=="PERMISSION_DENIED"

@pytest.mark.asyncio
async def test_delete_unknown_failclosed(tmp_path):
    raw={"result":{"task":{"id":9,"title":"x","status":2,"action":{}}}}
    with pytest.raises(AppError):await TaskTools(Fake(tmp_path,[raw]))._action(9,"delete")

@pytest.mark.asyncio
async def test_complete_actual_status_4(tmp_path):
    raw=lambda status:{"result":{"task":{"id":9,"title":"x","status":status,"action":{"complete":"Y"}}}}
    f=Fake(tmp_path,[raw(2),{"result":True},raw(4)]);r=await TaskTools(f)._action(9,"complete")
    assert r["status"]==4

@pytest.mark.asyncio
async def test_checklist_partial(tmp_path):
    f=Fake(tmp_path);r=await ExtraTools(f,TaskTools(f)).checklist_add(1,["a","b"])
    assert r["partial"] is True

@pytest.mark.asyncio
async def test_path_escape_and_base64_prelimit(tmp_path):
    f=Fake(tmp_path);e=ExtraTools(f,TaskTools(f))
    with pytest.raises(AppError):await e.file_upload("x",path="/etc/hosts",dry_run=True)
    with pytest.raises(AppError):await e.file_upload("x",content_base64="A"*100,dry_run=True)
    with pytest.raises(AppError):await e.file_upload("x",content_base64="not base64!",dry_run=True)

@pytest.mark.asyncio
async def test_symlink_escape(tmp_path):
    link=tmp_path/"link";link.symlink_to("/etc/hosts");e=ExtraTools(Fake(tmp_path),TaskTools(Fake(tmp_path)))
    with pytest.raises(AppError):await e.file_upload("x",path=str(link),dry_run=True)

@pytest.mark.asyncio
async def test_upload_dryrun_no_bytes_in_result(tmp_path):
    p=tmp_path/"x";p.write_bytes(b"secret");f=Fake(tmp_path);r=await ExtraTools(f,TaskTools(f)).file_upload("x",path=str(p),dry_run=True)
    assert "secret" not in str(r) and not f.calls

@pytest.mark.asyncio
async def test_attach_atomic_preserves_existing(tmp_path):
    f=Fake(tmp_path);await ExtraTools(f,TaskTools(f)).task_attach_files(1,[4,5]);cmd=f.calls[0][1]
    assert all(v[0]=="tasks.task.files.attach" for v in cmd.values())

def test_permission_unknown():assert permissions({"delete":"maybe"})["canDelete"] is None

@pytest.mark.asyncio
async def test_chat_send_file_uses_documented_commit_contract(tmp_path):
    f=Fake(tmp_path); await ExtraTools(f,TaskTools(f)).chat_send_file("chat1489",5249)
    method,params,mutation=f.calls[0]
    assert method=="im.disk.file.commit" and params=={"DIALOG_ID":"chat1489","FILE_ID":[5249],"MESSAGE":""} and mutation

@pytest.mark.asyncio
async def test_task_list_loads_checklists(tmp_path):
    rows={"result":{"tasks":[{"id":7,"title":"a","status":2},{"id":8,"title":"b","status":2}]}}
    class F(Fake):
        async def batch(self,c,mutation=False):
            self.calls.append(("batch",c,mutation))
            return {"successes":{"t7":[{"ID":"1","PARENT_ID":"0","TITLE":"Чек-лист 1"},{"ID":"2","PARENT_ID":"1","TITLE":"Пункт","IS_COMPLETE":"N","SORT_INDEX":"0"}]},"errors":{"t8":{"code":"X"}}}
    f=F(tmp_path,[rows]);r=await TaskTools(f).task_list()
    a,b=r["tasks"]
    assert a["checklist"]==[{"id":2,"title":"Пункт","done":False,"sort":0,"group_id":1}] and "checklist_unavailable" not in a
    assert a["checklist_progress"]=="0/1"
    assert b["checklist"]==[] and b["checklist_unavailable"] is True and b["checklist_progress"] is None
    assert f.calls[-1][1]["t7"]==("task.checklistitem.getlist",{"TASKID":7})

@pytest.mark.asyncio
async def test_task_list_checklists_split_into_batches_of_50(tmp_path):
    rows={"result":{"tasks":[{"id":i,"title":"t","status":2} for i in range(1,61)]}}
    class F(Fake):
        async def batch(self,c,mutation=False):
            self.calls.append(("batch",c,mutation)); return {"successes":{k:[] for k in c},"errors":{}}
    f=F(tmp_path,[rows]);r=await TaskTools(f).task_list(limit=60)
    sizes=[len(c[1]) for c in f.calls if c[0]=="batch"]
    assert sizes==[50,10] and not any(t.get("checklist_unavailable") for t in r["tasks"])

@pytest.mark.asyncio
async def test_chat_send_by_title_resolves_exact_chat(tmp_path):
    found={"result":[{"id":"76030","name":"Разработка. Правки","type":"chat"},{"id":"5","name":"Разработка. Правки (архив)","type":"chat"}]}
    f=Fake(tmp_path,[found,{"result":321}]);r=await ExtraTools(f,TaskTools(f)).chat_send("разработка. правки","Привет")
    assert f.calls[1][0]=="im.message.add" and f.calls[1][1]["DIALOG_ID"]=="chat76030"
    assert r["message_id"]==321 and r["dialog_id"]=="chat76030"

@pytest.mark.asyncio
async def test_chat_send_by_title_not_found_lists_candidates(tmp_path):
    found={"result":[{"id":"5","name":"Разработка. Правки (архив)","type":"chat"}]}
    f=Fake(tmp_path,[found])
    with pytest.raises(AppError) as e:await ExtraTools(f,TaskTools(f)).chat_send("Разработка. Правки","Привет")
    assert e.value.code=="CHAT_NOT_FOUND" and e.value.details["candidates"][0]["dialog_id"]=="chat5"
    assert all(c[0]!="im.message.add" for c in f.calls)

@pytest.mark.asyncio
async def test_chat_send_dialog_id_skips_lookup(tmp_path):
    f=Fake(tmp_path);await ExtraTools(f,TaskTools(f)).chat_send("chat12","x")
    assert [c[0] for c in f.calls]==["im.message.add"]

MEMBERS={"result":[{"id":13030,"first_name":"Полина","last_name":"Смирнова","name":"Полина Смирнова"},{"id":14,"first_name":"Олег","last_name":"Петров","name":"Олег Петров"},{"id":15,"first_name":"Олег","last_name":"Сидоров","name":"Олег Сидоров"},{"id":99,"first_name":"Бот","last_name":"","bot":True}]}

@pytest.mark.asyncio
async def test_mention_by_first_name_among_chat_members(tmp_path):
    f=Fake(tmp_path,[MEMBERS,{"result":1}]);await ExtraTools(f,TaskTools(f)).chat_send("chat7","@Полина посмотри, пожалуйста. Почта: a@b.ru")
    assert f.calls[0]==("im.dialog.users.list",{"DIALOG_ID":"chat7"},False)
    assert f.calls[1][1]["MESSAGE"]=="[USER=13030]Полина[/USER] посмотри, пожалуйста. Почта: a@b.ru"

@pytest.mark.asyncio
async def test_mention_full_name_disambiguates(tmp_path):
    f=Fake(tmp_path,[MEMBERS]);r=await ExtraTools(f,TaskTools(f)).chat_send("chat7","@Олег Сидоров и @Полина, готово",dry_run=True)
    assert r["message"]=="[USER=15]Олег Сидоров[/USER] и [USER=13030]Полина[/USER], готово" and len(f.calls)==1

@pytest.mark.asyncio
async def test_mention_ambiguous_first_name_blocks_send(tmp_path):
    f=Fake(tmp_path,[MEMBERS])
    with pytest.raises(AppError) as e:await ExtraTools(f,TaskTools(f)).chat_send("chat7","@Олег глянь")
    assert e.value.code=="USER_AMBIGUOUS" and {c["id"] for c in e.value.details["candidates"]}=={14,15}
    assert all(c[0]!="im.message.add" for c in f.calls)

@pytest.mark.asyncio
async def test_mention_outside_chat_falls_back_to_portal_search(tmp_path):
    users={"result":[{"ID":"77","NAME":"Анна","LAST_NAME":"Белова","ACTIVE":"Y"}]}
    f=Fake(tmp_path,[MEMBERS,users]);r=await ExtraTools(f,TaskTools(f)).chat_send("chat7","@Анна, привет",dry_run=True)
    assert r["message"]=="[USER=77]Анна[/USER], привет" and f.calls[1][0]=="user.get"

@pytest.mark.asyncio
async def test_message_without_at_skips_member_lookup(tmp_path):
    f=Fake(tmp_path);await ExtraTools(f,TaskTools(f)).chat_send("chat7","без упоминаний")
    assert [c[0] for c in f.calls]==["im.message.add"]

class BatchOk(Fake):
    async def batch(self,c,mutation=False):self.calls.append(("batch",c,mutation));return {"successes":{k:100+i for i,k in enumerate(c)},"errors":{}}

@pytest.mark.asyncio
async def test_checklist_add_with_files_uploads_and_attaches(tmp_path):
    shot=tmp_path/"s1.png";shot.write_bytes(b"png")
    attach={"result":{"checkListItem":{"id":101,"attachments":{"6816":"n500","6817":"n7"}}}}
    f=BatchOk(tmp_path,[{"result":{"ID":500}},attach]);r=await ExtraTools(f,TaskTools(f)).checklist_add(1,["Без файла",{"title":"Скрин","files":[str(shot),7]}])
    names=[c[0] for c in f.calls]
    assert names==["batch","disk.folder.uploadfile","tasks.task.checklist.addAttachmentsFromDisk"]
    assert f.calls[1][1]["generateUniqueName"] is True
    assert f.calls[2][1]=={"taskId":1,"checkListItemId":101,"filesIds":["n500","n7"]} and f.calls[2][2]
    assert r["attachments"]=={"add1":{"item_id":101,"file_ids":[500,7],"item_attachments":[7,500]}} and r["partial"] is False

@pytest.mark.asyncio
async def test_checklist_add_bad_path_writes_nothing(tmp_path):
    f=BatchOk(tmp_path)
    with pytest.raises(AppError) as e:await ExtraTools(f,TaskTools(f)).checklist_add(1,[{"title":"x","files":["/etc/hosts"]}])
    assert e.value.code=="PATH_DENIED" and not f.calls

@pytest.mark.asyncio
async def test_checklist_add_dry_run_lists_files(tmp_path):
    shot=tmp_path/"s.png";shot.write_bytes(b"pn")
    f=BatchOk(tmp_path);r=await ExtraTools(f,TaskTools(f)).checklist_add(1,[{"title":"x","files":[str(shot)]}],dry_run=True)
    assert r["titles"]==["x"] and r["files"]=={"add0":[{"path":str(shot),"name":"s.png","size":2}]} and not f.calls

@pytest.mark.asyncio
async def test_checklist_add_attach_failure_is_partial(tmp_path):
    class F(BatchOk):
        async def call(self,m,p=None,mutation=False):
            self.calls.append((m,p,mutation))
            if m=="tasks.task.checklist.addAttachmentsFromDisk":raise AppError("UPSTREAM_ERROR","no")
            return {"result":True}
    f=F(tmp_path);r=await ExtraTools(f,TaskTools(f)).checklist_add(1,[{"title":"x","files":[5]}])
    assert r["partial"] is True and r["attachment_errors"]["add0"]["code"]=="UPSTREAM_ERROR" and r["successes"]=={"add0":100}

@pytest.mark.asyncio
async def test_checklist_attach_existing_item(tmp_path):
    f=Fake(tmp_path,[{"result":{"checkListItem":{"attachments":{"1":"n9"}}}}]);r=await ExtraTools(f,TaskTools(f)).checklist_attach(3,44,[9])
    assert f.calls==[("tasks.task.checklist.addAttachmentsFromDisk",{"taskId":3,"checkListItemId":44,"filesIds":["n9"]},True)]
    assert r["applied"] is True and r["item_attachments"]==[9]

URL_PREFIX="https://example.bitrix24.ru/company/personal/user/1001/tasks/task/view/"

@pytest.mark.asyncio
async def test_task_tag_becomes_link_with_title(tmp_path):
    class F(Fake):
        async def batch(self,c,mutation=False):
            self.calls.append(("batch",c,mutation));return {"successes":{"t90958":{"task":{"id":"90958","title":"Правки [сайт]"}}},"errors":{"t5":{"code":"X"}}}
    f=F(tmp_path);r=await ExtraTools(f,TaskTools(f)).chat_send("chat1","См. [TASK=90958] и [task=5]",dry_run=True)
    assert r["message"]==f"См. [URL={URL_PREFIX}90958/]Правки (сайт) (#90958)[/URL] и [URL={URL_PREFIX}5/]задача #5[/URL]"
    assert f.calls[0]==("batch",{"t5":("tasks.task.get",{"taskId":5,"select":["ID","TITLE"]}),"t90958":("tasks.task.get",{"taskId":90958,"select":["ID","TITLE"]})},False)

@pytest.mark.asyncio
async def test_task_number_after_word_becomes_link(tmp_path):
    f=Fake(tmp_path);r=await ExtraTools(f,TaskTools(f)).chat_send("chat1","Закрыл задачу №90958, по задаче 91000 вопрос. Счёт 12345 не трогаю",dry_run=True)
    assert r["message"]==f"Закрыл задачу [URL={URL_PREFIX}90958/]№90958[/URL], по задаче [URL={URL_PREFIX}91000/]91000[/URL] вопрос. Счёт 12345 не трогаю"
    assert not f.calls

@pytest.mark.asyncio
async def test_untitled_task_tag_is_not_linked_twice(tmp_path):
    class F(Fake):
        async def batch(self,c,mutation=False):return {"successes":{},"errors":{"t90958":{"code":"X"}}}
    f=F(tmp_path);r=await ExtraTools(f,TaskTools(f)).chat_send("chat1","[TASK=90958]",dry_run=True)
    assert r["message"]==f"[URL={URL_PREFIX}90958/]задача #90958[/URL]"

def test_checklist_progress_counts_done_items():
    from bitrix_mcp.tasks import checklist_progress
    items=[{"done":True}]*10+[{"done":False}]*3
    assert checklist_progress(items)=="10/13" and checklist_progress([]) is None
