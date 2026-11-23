"""Interactive .env setup, called by install.sh: python scripts/setup_env.py

Asks for the incoming webhook, checks it against the portal (read-only calls),
finds your Disk folder, asks whether writes are allowed and generates the HTTP token.
Other values in .env are kept as they are.
"""
from __future__ import annotations

import asyncio, os, re, secrets, sys
from getpass import getpass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bitrix_mcp.core import AppError, Client, Settings  # noqa: E402
from bitrix_mcp.tasks import TaskTools  # noqa: E402

ENV = ROOT / ".env"
PLACEHOLDERS = {"", "/absolute/safe/path"}


def read_env() -> dict[str, str]:
    values = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            values[key.strip()] = value.strip()
    return values


def write_env(updates: dict[str, str]) -> None:
    lines, seen = [], set()
    for line in ENV.read_text(encoding="utf-8").splitlines():
        key = line.partition("=")[0].strip()
        if key in updates and not line.lstrip().startswith("#"):
            lines.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            lines.append(line)
    lines += [f"{k}={v}" for k, v in updates.items() if k not in seen]
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(ENV, 0o600)


def yes(prompt: str) -> bool:
    return input(prompt).strip().lower() in {"y", "yes", "д", "да"}


async def check(url: str) -> tuple[dict, int | None]:
    settings = Settings(webhook_url=url, read_only=True, audit_log=ROOT / "var" / "audit.jsonl")
    client = Client(settings)
    try:
        me = await TaskTools(client).whoami()
        folder = None
        try:
            storages = await client.call("disk.storage.getlist", {"filter": {"ENTITY_TYPE": "user", "ENTITY_ID": settings.user_id}})
            folder = int(storages["result"][0]["ROOT_OBJECT_ID"])
        except (AppError, KeyError, IndexError, TypeError, ValueError):
            pass  # no disk scope: file_upload will ask for folder_id
        return me, folder
    finally:
        await client.close()


def main() -> None:
    env = read_env()
    updates: dict[str, str] = {}
    current = env.get("B24_WEBHOOK_URL", "")
    if current and "REPLACE_WITH_SECRET" not in current and not yes("Вебхук уже задан. Ввести другой? [y/N] "):
        url = current
    else:
        print("Битрикс24 → Разработчикам → Другое → Входящий вебхук. Права: task, user, disk, im.")
        url = getpass("Адрес вебхука (ввод скрыт): ").strip()

    try:
        me, folder = asyncio.run(check(url))
    except AppError as e:
        sys.exit(f"Вебхук не подошёл: {e.message}")
    print(f"Подключились: {me['name']} (id {me['id']}), портал {Settings(webhook_url=url).portal}")
    updates["B24_WEBHOOK_URL"] = url
    if folder and not env.get("B24_DISK_FOLDER_ID"):
        updates["B24_DISK_FOLDER_ID"] = str(folder)
        print(f"Папка Диска для загрузки файлов: {folder}")

    writes = yes("Разрешить изменения в портале (задачи, комментарии, сообщения)? По умолчанию только чтение. [y/N] ")
    updates["MCP_READ_ONLY"] = "false" if writes else "true"

    if len(env.get("MCP_BEARER_TOKEN", "")) < 24:
        updates["MCP_BEARER_TOKEN"] = secrets.token_urlsafe(32)
    if env.get("MCP_ALLOWED_ROOTS", "") in PLACEHOLDERS:
        files = ROOT / "var" / "files"
        files.mkdir(parents=True, exist_ok=True)
        updates["MCP_ALLOWED_ROOTS"] = str(files)
    if not re.fullmatch(r"\d+", env.get("MCP_PORT", "")):
        updates["MCP_PORT"] = "8000"
    write_env(updates)
    print(f"Сохранено в {ENV}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nОтменено.")
