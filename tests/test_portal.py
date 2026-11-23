"""The server is not tied to one portal: portal and user come from the webhook URL."""
import httpx, pytest
from bitrix_mcp.core import AppError, Client, Settings
from bitrix_mcp.extras import ExtraTools
from bitrix_mcp.tasks import TaskTools

OTHER = "https://acme.bitrix24.kz/rest/7/abc_DEF-123/"


def settings(tmp_path, url=OTHER, **kw):
    return Settings(webhook_url=url, audit_log=tmp_path / "a", **kw)


def test_portal_and_user_from_webhook(tmp_path):
    s = settings(tmp_path)
    assert (s.portal_host, s.user_id, s.portal) == ("acme.bitrix24.kz", 7, "https://acme.bitrix24.kz")


def test_on_premise_portal_with_port(tmp_path):
    s = settings(tmp_path, "https://B24.Company.ru:8443/rest/15/secret/")
    assert (s.portal_host, s.user_id, s.portal) == ("b24.company.ru", 15, "https://b24.company.ru:8443")


@pytest.mark.parametrize("url", [
    "http://acme.bitrix24.kz/rest/7/secret/",           # no TLS
    "https://user:pw@acme.bitrix24.kz/rest/7/secret/",  # credentials in the URL
    "https://acme.bitrix24.kz/rest/0/secret/",          # no such user
    "https://acme.bitrix24.kz/rest/7/secret",           # missing trailing slash
    "https://acme.bitrix24.kz/rest/7/secret/extra/",
    "https://127.0.0.1/rest/7/secret/",
    "https://evil.com/x?u=https://acme.bitrix24.kz/rest/7/secret/",
])
def test_bad_webhooks_rejected(tmp_path, url):
    with pytest.raises(AppError) as e:
        settings(tmp_path, url)
    assert e.value.code == "CONFIG_INVALID"


@pytest.mark.asyncio
async def test_identity_is_checked_against_the_webhook_user(tmp_path):
    def handler(req):
        return httpx.Response(200, json={"result": {"ID": "7"}} if req.url.path.endswith("profile.json") else {"result": []})
    c = Client(settings(tmp_path), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await c.call("user.get", {})  # user 7 owns the webhook: passes
    await c._http.aclose()

    c = Client(settings(tmp_path), httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"result": {"ID": "1001"}}))))
    with pytest.raises(AppError, match="пользователю 7") as e:
        await c.call("profile")
    assert e.value.code == "IDENTITY_MISMATCH"
    await c._http.aclose()


class Fake:
    def __init__(self, tmp_path, results):
        self.settings = settings(tmp_path, read_only=False)
        self.results, self.calls = list(results), []

    async def call(self, method, params=None, mutation=False):
        self.calls.append((method, params))
        return self.results.pop(0) if self.results else {"result": True}


@pytest.mark.asyncio
async def test_task_links_and_defaults_use_the_webhook_user(tmp_path):
    f = Fake(tmp_path, [{"result": {"ID": "7", "NAME": "Ann", "LAST_NAME": "Lee"}}])
    t = TaskTools(f)
    assert t.task_url(42) == "https://acme.bitrix24.kz/company/personal/user/7/tasks/task/view/42/"
    assert (await t.whoami())["id"] == 7
    preview = await t.task_create("T", dry_run=True)
    assert preview["fields"]["CREATED_BY"] == 7 and preview["fields"]["RESPONSIBLE_ID"] == 7


@pytest.mark.asyncio
async def test_download_only_from_own_portal(tmp_path):
    foreign = {"result": {"DOWNLOAD_URL": "https://example.bitrix24.ru/disk/download/1"}}
    f = Fake(tmp_path, [foreign])
    with pytest.raises(AppError) as e:
        await ExtraTools(f, TaskTools(f)).file_download(1, return_base64=True)
    assert e.value.code == "UNSAFE_URL"
