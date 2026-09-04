"""GitLab, as far as `/label` needs it.

One provider behind `Forge`. Everything GitLab-shaped is here — the `/api/v4`
paths, the `PRIVATE-TOKEN` header, the fact that a project is named by its
URL-encoded path — and nothing outside this module knows any of it.

**Labels are added, never set.** The obvious call is to send the whole `labels`
list, and it is wrong: between reading an issue and writing it back, somebody
at a desk may have labelled it themselves, and a full write would quietly
remove what they did. `add_labels` asks GitLab to add one and leave the rest,
which is the difference between a phone that participates and a phone that
overwrites.

The token needs `api` scope, because GitLab has no narrower one for writing a
label onto an issue. A *project* access token is the way to make that mean
less: the scope stays wide and its reach becomes one project.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

from halyard.tasks.spec import ForgeError, Task

logger = logging.getLogger(__name__)

#: Long enough for a slow instance, short enough that a phone is told something.
TIMEOUT = 20.0

#: GitLab pages labels at 20 by default. A project with more than this many is
#: past what a phone keyboard can show anyway — see the `labels:` narrowing in
#: the project configuration.
PER_PAGE = 100


class GitLab:
    """One GitLab project."""

    name = "GitLab"

    def __init__(
        self, host: str, path: str, token: str, *, client: httpx.AsyncClient | None = None
    ) -> None:
        self._base = f"https://{host}/api/v4"
        # Encoded whole, slashes included: GitLab names a project by its path
        # and expects `group%2Fsub%2Fproject` in the URL.
        self._project = quote(path, safe="")
        self._token = token
        self._client = client

    async def _ask(self, method: str, where: str, **kwargs) -> object:
        """One call, with GitLab's own words kept when it says no."""
        client = self._client or httpx.AsyncClient(timeout=TIMEOUT)
        try:
            answered = await client.request(
                method, f"{self._base}{where}", headers={"PRIVATE-TOKEN": self._token}, **kwargs
            )
        except httpx.HTTPError as unreachable:
            raise ForgeError(f"GitLab could not be reached: {unreachable}") from None
        finally:
            if self._client is None:
                await client.aclose()

        if answered.status_code == 401:
            raise ForgeError(
                "GitLab refused the token. It needs `api` scope and must not be expired."
            )
        if answered.status_code == 403:
            raise ForgeError("That token is not allowed to do this on this project.")
        if answered.status_code == 404:
            raise ForgeError("GitLab has no such project or issue.")
        if answered.status_code >= 400:
            # Its own message, which is more use than anything invented here.
            said = ""
            try:
                body = answered.json()
                said = str(body.get("message") or body.get("error") or "")
            except ValueError:
                said = answered.text[:200]
            raise ForgeError(f"GitLab said {answered.status_code}: {said}".strip())
        try:
            return answered.json()
        except ValueError:
            raise ForgeError("GitLab answered with something that was not JSON.") from None

    async def task(self, number: int) -> Task:
        """The issue, with what is already on it."""
        return _as_task(await self._ask("GET", f"/projects/{self._project}/issues/{number}"))

    async def labels(self) -> tuple[str, ...]:
        """Every label this project defines."""
        body = await self._ask("GET", f"/projects/{self._project}/labels?per_page={PER_PAGE}")
        if not isinstance(body, list):
            raise ForgeError("GitLab listed labels in a shape this does not understand.")
        return tuple(
            str(one.get("name")) for one in body if isinstance(one, dict) and one.get("name")
        )

    async def add_label(self, number: int, label: str) -> Task:
        """Add one label, leaving whatever else is on the issue alone."""
        return _as_task(
            await self._ask(
                "PUT",
                f"/projects/{self._project}/issues/{number}",
                json={"add_labels": label},
            )
        )


def _as_task(body: object) -> Task:
    if not isinstance(body, dict):
        raise ForgeError("GitLab described that issue in a shape this does not understand.")
    labels = body.get("labels") or []
    return Task(
        number=int(body.get("iid") or 0),
        title=str(body.get("title") or ""),
        labels=tuple(str(name) for name in labels if str(name).strip()),
        url=str(body.get("web_url") or ""),
    )
