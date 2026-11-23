"""Проверка записей на одной тестовой задаче: python scripts/live_write_test.py

Все участники задачи — сам владелец вебхука, в рабочие чаты ничего не пишется.
Скрипт останавливается на первой ошибке и не повторяет записи.
"""
from __future__ import annotations

import asyncio, base64, json, os, sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from live_readonly import load_env  # noqa: E402
from bitrix_mcp.core import AppError, Client, MOSCOW, Settings  # noqa: E402
from bitrix_mcp.extras import ExtraTools  # noqa: E402
from bitrix_mcp.tasks import TaskTools  # noqa: E402

TITLE = "[ТЕСТ MCP] проверка коннектора Bitrix24, можно удалить"


def show(name: str, value) -> None:
    text = json.dumps(value, ensure_ascii=False, default=str)
    print(f"OK   {name}: {text[:500]}{'…' if len(text) > 500 else ''}", flush=True)


async def main() -> None:
    load_env()
    os.environ["MCP_READ_ONLY"] = "false"
    os.environ.setdefault("MCP_AUDIT_LOG", str(ROOT / "var/audit-live.jsonl"))
    client = Client(Settings.from_env())
    tasks = TaskTools(client)
    extra = ExtraTools(client, tasks)
    try:
        show("whoami", await tasks.whoami())

        storages = await client.call("disk.storage.getlist", {"filter": {"ENTITY_TYPE": "user", "ENTITY_ID": tasks.user_id}})
        folder = int(storages["result"][0]["ROOT_OBJECT_ID"])
        uploaded = await extra.file_upload("mcp-test.txt", content_base64=base64.b64encode("Тестовый файл MCP\n".encode()).decode(), folder_id=folder)
        show("file_upload", uploaded)
        file_id = uploaded["id"]

        deadline = (datetime.now(MOSCOW) + timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0).isoformat()
        created = await tasks.task_create(
            TITLE, description="**Тест** MCP-сервера.\n\n- проверка полей\n- [ссылка](" + client.settings.portal + "/)",
            responsible=tasks.user_id, deadline=deadline, tags=["mcp-test"], task_control=True,
            allow_change_deadline=True, files=[file_id])
        show("task_create", created)
        tid = created["id"]

        got = await tasks.task_get(tid, with_checklist=True)
        show("task_get", {k: got[k] for k in ("title", "deadline", "tags", "taskControl", "allowChangeDeadline", "files", "status", "permissions")})

        dup = await tasks.task_create(TITLE, dry_run=True)
        show("дубль (dry_run)", dup["duplicate_candidates"])

        show("task_update", {k: v for k, v in (await tasks.task_update(tid, title=TITLE + " (обновлено)")).items() if k != "task"})

        added = await extra.checklist_add(tid, ["Пункт 1", "Пункт 2", "Пункт 3"])
        show("checklist_add", added)
        items = (await extra.checklist_get(tid))["items"]
        show("checklist_get", [(i.get("ID"), i.get("TITLE"), i.get("IS_COMPLETE")) for i in items])
        first = int(items[0]["ID"])
        show("checklist_toggle", await extra.checklist_toggle(tid, first, True))
        show("checklist_update", await extra.checklist_update(tid, int(items[1]["ID"]), "Пункт 2 (изменён)"))
        ids = [int(i["ID"]) for i in items]
        show("checklist_reorder", await extra.checklist_reorder(tid, list(reversed(ids))))
        show("checklist после", [(i.get("TITLE"), i.get("IS_COMPLETE"), i.get("SORT_INDEX")) for i in (await extra.checklist_get(tid))["items"]])

        show("comment_add", await extra.comment_add(tid, "Тестовый комментарий от MCP", files=[file_id]))
        show("comment_list", await extra.comment_list(tid, limit=10))

        show("task_start", {k: v for k, v in (await tasks._action(tid, "start")).items() if k != "task"})
        show("task_complete", {k: v for k, v in (await tasks._action(tid, "complete")).items() if k != "task"})
        print(f"\nТестовая задача: {created['task_url']}")
    except AppError as e:
        print(f"ERR  {e.code}: {e.message} {e.details or ''}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
