"""Живая проверка только на чтение: python scripts/live_readonly.py [--task-id N] [--chat-title T] [--dialog-id chatN]

Берёт B24_WEBHOOK_URL из окружения или из .env в корне проекта.
Режим только для чтения включается принудительно: REST-записи отклоняет сам клиент.
"""
from __future__ import annotations

import argparse, asyncio, json, os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bitrix_mcp.core import AppError, Client, Settings  # noqa: E402
from bitrix_mcp.extras import ExtraTools  # noqa: E402
from bitrix_mcp.tasks import TaskTools  # noqa: E402


def load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and not key.startswith("#") and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


async def step(name: str, coro) -> None:
    try:
        result = await coro
        text = json.dumps(result, ensure_ascii=False, default=str)
        print(f"OK   {name}: {text[:600]}{'…' if len(text) > 600 else ''}")
    except AppError as e:
        print(f"ERR  {name}: {e.code}: {e.message}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Live read-only check against your portal")
    parser.add_argument("--task-id", type=int, help="task to read (default: your latest task)")
    parser.add_argument("--chat-title", help="chat to look up with chat_find")
    parser.add_argument("--dialog-id", help="dialog to read with chat_history, e.g. chat123")
    args = parser.parse_args()
    load_env()
    os.environ["MCP_READ_ONLY"] = "true"
    os.environ.setdefault("MCP_AUDIT_LOG", str(ROOT / "var/audit-live.jsonl"))
    settings = Settings.from_env()
    assert settings.read_only
    client = Client(settings)
    tasks = TaskTools(client)
    extra = ExtraTools(client, tasks)
    try:
        me = await tasks.whoami()
        await step("whoami", asyncio.sleep(0, me))
        mine = await tasks.task_list(role="responsible", limit=3)
        await step("task_list (мои, 3 шт.)", asyncio.sleep(0, mine))
        task_id = args.task_id or next((t["id"] for t in mine.get("tasks", [])), None)
        if task_id:
            await step(f"task_get {task_id}", tasks.task_get(task_id, with_checklist=True))
            await step(f"task_update {task_id} dry_run", tasks.task_update(task_id, dry_run=True, title="проверка"))
            await step(f"task_delete {task_id} dry_run", tasks._action(task_id, "delete", dry_run=True))
            await step(f"comment_list {task_id} (3)", extra.comment_list(task_id, limit=3))
            await step(f"checklist_get {task_id}", extra.checklist_get(task_id))
        await step(f"user_search «{me['name']}»", tasks.user_search(me["name"]))
        await step("tag_list", tasks.tag_list(scan_limit=100))
        if args.chat_title:
            await step(f"chat_find «{args.chat_title}»", extra.chat_find(args.chat_title))
        if args.dialog_id:
            await step(f"chat_history {args.dialog_id} (3)", extra.chat_history(args.dialog_id, 3))
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
