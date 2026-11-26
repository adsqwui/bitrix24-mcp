from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

MOSCOW = ZoneInfo('Europe/Moscow')

class AppError(Exception):
    def __init__(self, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details
    def as_dict(self):
        result = {'code': self.code, 'message': self.message}
        if self.details is not None:
            result['details'] = self.details
        return result

WEBHOOK_RE = re.compile(r'https://((?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63})(:[0-9]{1,5})?/rest/([1-9][0-9]{0,9})/[A-Za-z0-9_-]+/')

@dataclass(slots=True)
class Settings:
    webhook_url: str = field(repr=False)
    read_only: bool = True
    allowed_roots: tuple[Path, ...] = ()
    audit_log: Path = Path('var/audit.jsonl')
    host: str = '127.0.0.1'
    port: int = 8000
    bearer_token: str | None = field(default=None, repr=False)
    max_file_bytes: int = 10_485_760
    disk_folder_id: int | None = None
    # Taken from the webhook itself: https://<portal>/rest/<user id>/<code>/
    portal_host: str = field(init=False, default='')
    portal_port: str = field(init=False, default='')
    user_id: int = field(init=False, default=0)

    def __post_init__(self):
        match = WEBHOOK_RE.fullmatch(self.webhook_url)
        if not match:
            raise AppError('CONFIG_INVALID', 'Неверный формат B24_WEBHOOK_URL: нужен HTTPS-вебхук вида https://портал/rest/ID_пользователя/код/.')
        self.portal_host, self.portal_port, self.user_id = match.group(1).lower(), match.group(2) or '', int(match.group(3))
        if self.host not in {'127.0.0.1', 'localhost', '::1'} and not self.bearer_token:
            raise AppError('CONFIG_INVALID', 'Для публичного HTTP обязателен MCP_BEARER_TOKEN и TLS reverse proxy.')
        if self.bearer_token and len(self.bearer_token) < 24:
            raise AppError('CONFIG_INVALID', 'MCP_BEARER_TOKEN должен содержать минимум 24 символа.')
        if not 1 <= self.port <= 65535 or self.max_file_bytes <= 0:
            raise AppError('CONFIG_INVALID', 'Некорректный порт или лимит файла.')
        self.allowed_roots = tuple(Path(p).expanduser().resolve() for p in self.allowed_roots)
        self.audit_log = Path(self.audit_log)

    @property
    def portal(self) -> str:
        return f'https://{self.portal_host}{self.portal_port}'

    @classmethod
    def from_env(cls):
        try:
            flag = os.getenv('MCP_READ_ONLY', 'true').lower()
            if flag not in {'true', 'false', '1', '0'}:
                raise ValueError
            return cls(
                webhook_url=os.environ['B24_WEBHOOK_URL'], read_only=flag in {'true', '1'},
                allowed_roots=tuple(Path(p) for p in os.getenv('MCP_ALLOWED_ROOTS', '').split(os.pathsep) if p),
                audit_log=Path(os.getenv('MCP_AUDIT_LOG', 'var/audit.jsonl')),
                host=os.getenv('MCP_HOST', '127.0.0.1'), port=int(os.getenv('MCP_PORT', '8000')),
                bearer_token=os.getenv('MCP_BEARER_TOKEN') or None,
                max_file_bytes=int(os.getenv('MCP_MAX_FILE_BYTES', '10485760')),
                disk_folder_id=int(os.environ['B24_DISK_FOLDER_ID']) if os.getenv('B24_DISK_FOLDER_ID') else None,
            )
        except (KeyError, ValueError):
            raise AppError('CONFIG_INVALID', 'Проверьте переменные окружения: B24_WEBHOOK_URL, порт, флаги и числовые лимиты.') from None

READ_METHODS = frozenset({
    'profile', 'methods', 'user.get', 'user.search', 'tasks.task.get', 'tasks.task.list',
    'task.checklistitem.get', 'task.checklistitem.getlist', 'task.checklistitem.getmanifest',
    'task.commentitem.getlist', 'task.commentitem.get', 'task.item.getallowedactions',
    'im.dialog.get', 'im.dialog.messages.get', 'im.dialog.users.list', 'im.recent.list', 'im.search.chat.list',
    'im.disk.folder.get', 'disk.file.get', 'disk.attachedObject.get',
    'disk.storage.getlist', 'disk.folder.getchildren', 'tasks.task.getaccess',
})
WRITE_METHODS = frozenset({
    'tasks.task.add', 'tasks.task.update', 'tasks.task.delete', 'tasks.task.start',
    'tasks.task.complete', 'tasks.task.defer', 'tasks.task.delegate', 'tasks.task.renew',
    'task.checklistitem.add', 'task.checklistitem.update', 'task.checklistitem.delete',
    'task.checklistitem.complete', 'task.checklistitem.renew', 'task.checklistitem.moveafteritem',
    'task.commentitem.add', 'disk.folder.uploadfile',
    'tasks.task.files.attach', 'tasks.task.checklist.addAttachmentsFromDisk',
    'im.message.add', 'im.disk.file.commit', 'im.disk.file.upload',
    'tasks.task.chat.message.send',
})
# REST 3.0 methods: JSON body, URL with the /rest/api/ segment.
V3_METHODS = frozenset({'tasks.task.chat.message.send'})

def normalize_date(value):
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, datetime.min.time())
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except ValueError:
            try:
                dt = datetime.strptime(str(value), '%d.%m.%Y %H:%M:%S')
            except ValueError:
                raise AppError('INVALID_DATE', 'Некорректная дата: ожидается ISO 8601.') from None
    return (dt if dt.tzinfo else dt.replace(tzinfo=MOSCOW)).astimezone(MOSCOW).isoformat(timespec='seconds')

def markdown_to_bbcode(text):
    # Protect fenced code from all later inline substitutions.  UUID-like
    # sentinels are deliberately generated from an index, not user text.
    blocks: list[str] = []
    def fenced(match: re.Match[str]) -> str:
        blocks.append('[code]' + match.group(1) + '[/code]')
        return f'\x00B24_CODE_{len(blocks) - 1}\x00'
    text = re.sub(r'```(?:\w+)?\n?(.*?)```', fenced, text, flags=re.S)
    text = re.sub(r'`([^`\n]+)`', r'[code]\1[/code]', text)
    text = re.sub(r'\[([^\]]+)]\((https?://[^)]+)\)', r'[url=\2]\1[/url]', text)
    text = re.sub(r'\*\*([^*\n]+)\*\*', r'[b]\1[/b]', text)
    text = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'[i]\1[/i]', text)
    text = re.sub(r'(?m)^#{1,6}\s+(.+)$', r'[b]\1[/b]', text)
    for index, block in enumerate(blocks):
        text = text.replace(f'\x00B24_CODE_{index}\x00', block)
    return text

def _flatten(value, prefix=''):
    out = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            out.extend(_flatten(item, f'{prefix}[{key}]' if prefix else str(key)))
    elif isinstance(value, (list, tuple)):
        if not value:
            out.append((prefix, ''))
        for i, item in enumerate(value):
            out.extend(_flatten(item, f'{prefix}[{i}]'))
    elif value is not None:
        out.append((prefix, 'Y' if value is True else 'N' if value is False else str(value)))
    return out

def _upstream_error(data, status=200):
    code = str(data.get('error', '')) if isinstance(data, Mapping) else ''
    if code == 'QUERY_LIMIT_EXCEEDED' or status == 429:
        return AppError('RATE_LIMITED', 'Битрикс24 ограничил частоту запросов; повторите позже.')
    # Не выдаём upstream description/code: оба могут содержать секретные данные.
    description = str(data.get('error_description', '')).lower() if isinstance(data, Mapping) else ''
    if status in {401, 403} or any(s in (code + ' ' + description).lower() for s in ('denied', 'access', 'permission', 'прав', 'доступ')):
        return AppError('PERMISSION_DENIED', 'Нет прав на операцию. Проверьте права вебхука и права владельца вебхука на эту задачу.')
    return AppError('UPSTREAM_ERROR', 'Битрикс24 отклонил запрос: проверьте поля, наличие объекта и права вебхука.')

class Client:
    def __init__(self, settings, http=None):
        self.settings = settings
        self._http = http or httpx.AsyncClient(timeout=30, follow_redirects=False)
        self._owns_http = http is None
        self._rate_lock, self._identity_lock, self._audit_lock = asyncio.Lock(), asyncio.Lock(), asyncio.Lock()
        self._last_request, self._identity_ok = 0.0, False

    async def close(self):
        if self._owns_http:
            await self._http.aclose()

    async def audit(self, event, **metadata):
        # Audit values are selected from internal enums too: merely allowing a
        # key is insufficient because upstream strings may contain a webhook.
        events = {'rest.call', 'rest.batch', 'tool.denied', 'tool.call'}
        methods = READ_METHODS | WRITE_METHODS | {'batch'}
        codes = {'READ_ONLY', 'METHOD_NOT_ALLOWED', 'METHOD_CLASSIFICATION',
                 'RATE_LIMITED', 'PERMISSION_DENIED', 'NETWORK_ERROR',
                 'OUTCOME_UNKNOWN', 'INVALID_RESPONSE', 'UPSTREAM_ERROR',
                 'IDENTITY_MISMATCH', 'AUDIT_FAILED', 'BATCH_SIZE',
                 'BATCH_INVALID', 'VALIDATION', 'USER_AMBIGUOUS', 'USER_NOT_FOUND',
                 'INVALID_DATE', 'PATH_DENIED', 'FILE_LIMIT', 'FILE_EXISTS',
                 'UNSAFE_URL', 'DOWNLOAD_FAILED', 'CONFIG_INVALID',
                 'CAPABILITY_UNVERIFIED', 'INTERNAL_ERROR', 'WEBHOOK_INVALID', 'TASK_NOT_FOUND',
                 'CHAT_NOT_FOUND', 'CHAT_AMBIGUOUS'}
        safe: dict[str, Any] = {}
        if metadata.get('method') in methods: safe['method'] = metadata['method']
        if metadata.get('code') in codes: safe['code'] = metadata['code']
        tool = metadata.get('tool')
        if isinstance(tool, str) and re.fullmatch(r'[a-z_]{1,40}', tool): safe['tool'] = tool
        for key in ('mutation', 'success', 'dry_run'):
            if isinstance(metadata.get(key), bool): safe[key] = metadata[key]
        for key in ('count', 'duration_ms'):
            value = metadata.get(key)
            if isinstance(value, int) and 0 <= value <= 1_000_000: safe[key] = value
        record = {'time': datetime.now(MOSCOW).isoformat(), 'event': event if event in events else 'rest.call', **safe}
        def append():
            self.settings.audit_log.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.settings.audit_log, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n')
        async with self._audit_lock:
            try:
                await asyncio.to_thread(append)
            except OSError:
                raise AppError('AUDIT_FAILED', 'Не удалось записать журнал аудита.') from None

    async def _pace(self):
        async with self._rate_lock:
            loop = asyncio.get_running_loop()
            wait = self._last_request + .5 - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = loop.time()

    async def _post(self, method, params, mutation, retries=3):
        for attempt in range(retries + 1):
            await self._pace()
            try:
                if method in V3_METHODS:
                    response = await self._http.post(self.settings.webhook_url.replace('/rest/', '/rest/api/', 1) + method, content=json.dumps(params, ensure_ascii=False), headers={'Content-Type': 'application/json', 'Accept': 'application/json'})
                else:
                    response = await self._http.post(self.settings.webhook_url + method + '.json', content=urlencode(_flatten(params)), headers={'Content-Type': 'application/x-www-form-urlencoded'})
            except httpx.RequestError:
                raise AppError('OUTCOME_UNKNOWN' if mutation else 'NETWORK_ERROR', 'Связь прервалась; результат изменения неизвестен. Не повторяйте запись без проверки.' if mutation else 'Битрикс24 временно недоступен.') from None
            if response.history or not 200 <= response.status_code < 300:
                if response.status_code == 429:
                    if attempt < retries:
                        await asyncio.sleep(min(.5 * 2**attempt, 4))
                        continue
                    raise _upstream_error({}, 429)
                if response.status_code >= 500 and mutation:
                    raise AppError('OUTCOME_UNKNOWN', 'Ошибка сервера: результат изменения неизвестен; автоматического повтора не было.')
                if 300 <= response.status_code < 400:
                    raise AppError('OUTCOME_UNKNOWN' if mutation else 'INVALID_RESPONSE', 'Битрикс24 вернул недопустимое перенаправление; запрос не повторялся.')
            if response.status_code == 429:
                if attempt < retries:
                    await asyncio.sleep(min(.5 * 2**attempt, 4))
                    continue
                raise _upstream_error({}, 429)
            if response.status_code >= 500 and mutation:
                raise AppError('OUTCOME_UNKNOWN', 'Ошибка сервера: результат изменения неизвестен; автоматического повтора не было.')
            try:
                data = response.json()
            except ValueError:
                raise AppError('OUTCOME_UNKNOWN' if mutation else 'INVALID_RESPONSE', 'Битрикс24 вернул некорректный ответ; проверьте результат операции.') from None
            if not isinstance(data, dict) or ('result' not in data and not data.get('error')):
                raise AppError('OUTCOME_UNKNOWN' if mutation else 'INVALID_RESPONSE', 'Битрикс24 вернул неполный ответ; проверьте результат операции.')
            if response.status_code >= 400 or data.get('error'):
                error = _upstream_error(data, response.status_code)
                if error.code == 'RATE_LIMITED' and attempt < retries:
                    await asyncio.sleep(min(.5 * 2**attempt, 4))
                    continue
                raise error
            return data

    async def _identity(self):
        if self._identity_ok:
            return
        async with self._identity_lock:
            if self._identity_ok:
                return
            try:
                envelope = await self._post('profile', {}, False)
            except AppError as exc:
                if exc.code == 'PERMISSION_DENIED':
                    raise AppError('WEBHOOK_INVALID', 'Портал отклонил вебхук: проверьте B24_WEBHOOK_URL и что вебхук не отозван.') from None
                raise
            result = envelope.get('result')
            if not isinstance(result, Mapping) or str(result.get('ID')) != str(self.settings.user_id):
                raise AppError('IDENTITY_MISMATCH', f'Вебхук должен принадлежать пользователю {self.settings.user_id}.')
            self._identity_ok = True

    def _check(self, method, mutation=False):
        if method not in READ_METHODS | WRITE_METHODS:
            raise AppError('METHOD_NOT_ALLOWED', 'REST-метод не разрешён.')
        actual = method in WRITE_METHODS
        if mutation and not actual:
            raise AppError('METHOD_CLASSIFICATION', 'Неверная классификация REST-метода.')
        if self.settings.read_only and actual:
            raise AppError('READ_ONLY', 'Изменения запрещены режимом только для чтения.')
        return actual

    async def call(self, method, params=None, mutation=False):
        actual = method in WRITE_METHODS
        try:
            actual = self._check(method, mutation)
            await self._identity()
            result = await self._post(method, params or {}, actual)
            try:
                await self.audit('rest.call', method=method, mutation=actual, success=True)
            except AppError:
                # The upstream write has already succeeded.  Never turn a
                # local logging failure into a replayable generic failure.
                if actual:
                    result = dict(result)
                    result['_local_warning'] = {'code': 'AUDIT_FAILED', 'message': 'Изменение применено, но локальный журнал аудита недоступен.'}
                else:
                    raise
            return result
        except AppError as exc:
            try:
                await self.audit('rest.call', method=method, mutation=actual, success=False, code=exc.code)
            except AppError:
                if exc.code != 'AUDIT_FAILED':
                    raise AppError('AUDIT_FAILED', 'Операция не выполнена; журнал аудита недоступен.') from None
            raise

    async def batch(self, commands, mutation=False):
        # name -> (REST method, parameters); never replay successful commands.
        actual = False
        try:
            if not 1 <= len(commands) <= 50:
                raise AppError('BATCH_SIZE', 'Пакет должен содержать от 1 до 50 команд.')
            for name, (method, params) in commands.items():
                if not re.fullmatch(r'[A-Za-z0-9_]+', name):
                    raise AppError('BATCH_INVALID', 'Некорректное имя команды.')
                method_actual = self._check(method)
                if mutation and not method_actual:
                    raise AppError('METHOD_CLASSIFICATION', 'Неверная классификация REST-метода в batch.')
                actual = method_actual or actual
            if self.settings.read_only and actual:
                raise AppError('READ_ONLY', 'Изменения запрещены режимом только для чтения.')
        except AppError as exc:
            await self.audit('rest.batch', count=len(commands), mutation=actual, success=False, code=exc.code)
            raise
        await self._identity()
        pending, successes, errors = dict(commands), {}, {}
        for attempt in range(4):
            cmd = {key: method + '?' + urlencode(_flatten(params or {})) for key, (method, params) in pending.items()}
            try:
                envelope = await self._post('batch', {'halt': 0, 'cmd': cmd}, actual)
                body = envelope.get('result', {})
                ok, failed = body.get('result', {}), body.get('result_error', {})
                # Bitrix sends an empty list instead of {} when nothing succeeded or failed.
                ok, failed = ok if ok != [] else {}, failed if failed != [] else {}
                if not isinstance(ok, dict) or not isinstance(failed, dict):
                    raise ValueError
            except (AppError, ValueError, AttributeError) as exc:
                error = exc if isinstance(exc, AppError) else AppError('OUTCOME_UNKNOWN', 'Некорректный batch-ответ.')
                for key in pending:
                    errors[key] = error.as_dict()
                break
            retry = {}
            for key in pending:
                if key in ok:
                    successes[key] = ok[key]
                elif key in failed:
                    item = failed[key]
                    if isinstance(item, dict) and item.get('error') == 'QUERY_LIMIT_EXCEEDED' and attempt < 3:
                        retry[key] = pending[key]
                    else:
                        errors[key] = _upstream_error(item).as_dict()
                else:
                    errors[key] = AppError('OUTCOME_UNKNOWN', 'Нет результата команды; автоматического повтора не было.').as_dict()
            pending = retry
            if not pending:
                break
            await asyncio.sleep(min(.5 * 2**attempt, 4))
        result = {'successes': successes, 'errors': errors}
        try:
            await self.audit('rest.batch', count=len(commands), mutation=actual, success=not errors)
        except AppError:
            if actual and successes:
                result['warning'] = {'code': 'AUDIT_FAILED', 'message': 'Часть изменений могла быть применена, но локальный журнал аудита недоступен.'}
            else:
                raise
        return result
