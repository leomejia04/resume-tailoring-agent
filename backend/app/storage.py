"""Private file storage through the Supabase Storage API.

Requests carry the signed-in user's own access token (never the service
role key), so Storage's row-level security policies independently enforce
that users can only touch objects under their own ``<user_id>/`` folder.

Background jobs have no user session. Instead, the request signs URLs for
exactly the objects a job needs (``sign_download``/``sign_upload``, authorized
by the same policies), and the worker uses them through ``SignedObject``,
which holds no credentials at all.
"""

from __future__ import annotations

from urllib.parse import quote, urlsplit

import httpx

from app.config import Settings

_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
# Storage's list and bulk-delete calls accept up to 1000 entries.
_LIST_PAGE = 1000


class StorageUnavailableError(RuntimeError):
    pass


class StorageObjectMissingError(RuntimeError):
    pass


class StorageRejectedError(RuntimeError):
    def __init__(self, status: int) -> None:
        super().__init__(f"storage rejected request with status {status}")
        self.status = status


class UserStorage:
    def __init__(self, settings: Settings, access_token: str, bucket: str | None = None) -> None:
        if settings.supabase_anon_key is None:
            raise StorageUnavailableError("SUPABASE_ANON_KEY is not set")
        self._base = f"{settings.supabase_url}/storage/v1/object"
        self._bucket = bucket or settings.storage_bucket
        self._headers = {
            "apikey": settings.supabase_anon_key.get_secret_value(),
            "Authorization": f"Bearer {access_token}",
        }

    def upload(self, path: str, data: bytes, content_type: str) -> None:
        """Create a new object; never overwrites (masters are immutable)."""
        url = f"{self._base}/{self._bucket}/{quote(path)}"
        headers = {**self._headers, "Content-Type": content_type, "x-upsert": "false"}
        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                response = client.post(url, content=data, headers=headers)
        except httpx.HTTPError as exc:
            raise StorageUnavailableError("storage unreachable") from exc
        if response.status_code >= 500:
            raise StorageUnavailableError(f"storage error {response.status_code}")
        if response.status_code >= 400:
            raise StorageRejectedError(response.status_code)

    def sign_download(self, path: str, expires_in: int) -> str:
        """A relative signed URL that can read one existing object until it expires."""
        url = f"{self._base}/sign/{self._bucket}/{quote(path)}"
        body = self._post_json(url, {"expiresIn": expires_in})
        return _relative(body.get("signedURL"))

    def sign_upload(self, path: str) -> str:
        """A relative signed URL that can create one new object (never overwrite)."""
        body = self._post_json(f"{self._base}/upload/sign/{self._bucket}/{quote(path)}", None)
        return _relative(body.get("url"))

    def _post_json(self, url: str, payload: dict | None) -> dict:
        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                response = client.post(url, json=payload, headers=self._headers)
        except httpx.HTTPError as exc:
            raise StorageUnavailableError("storage unreachable") from exc
        if response.status_code >= 500:
            raise StorageUnavailableError(f"storage error {response.status_code}")
        if response.status_code in (400, 404):
            raise StorageObjectMissingError("cannot sign that object")
        if response.status_code >= 400:
            raise StorageRejectedError(response.status_code)
        return response.json()

    def list_tree(self, folder: str, *, max_depth: int = 4, max_objects: int = 20_000) -> list[str]:
        """Every object path under ``folder`` (recursively, bounded)."""
        found: list[str] = []
        pending = [(folder.strip("/"), 0)]
        while pending:
            prefix, depth = pending.pop()
            offset = 0
            while True:
                entries = self._send(
                    "POST",
                    f"{self._base}/list/{self._bucket}",
                    {"prefix": f"{prefix}/", "limit": _LIST_PAGE, "offset": offset},
                )
                if not isinstance(entries, list):
                    raise StorageUnavailableError("unexpected list response")
                for entry in entries:
                    name = entry.get("name") if isinstance(entry, dict) else None
                    if not isinstance(name, str) or not name or "/" in name:
                        raise StorageUnavailableError("unexpected list entry")
                    if entry.get("id") is None:  # a folder
                        if depth + 1 >= max_depth:
                            raise StorageUnavailableError("folders nested too deeply")
                        pending.append((f"{prefix}/{name}", depth + 1))
                    else:
                        found.append(f"{prefix}/{name}")
                if len(found) > max_objects:
                    raise StorageUnavailableError("too many objects")
                if len(entries) < _LIST_PAGE:
                    break
                offset += _LIST_PAGE
        return found

    def delete(self, paths: list[str]) -> None:
        """Delete objects (allowed by policy only while account deletion is requested)."""
        for start in range(0, len(paths), _LIST_PAGE):
            batch = paths[start : start + _LIST_PAGE]
            self._send("DELETE", f"{self._base}/{self._bucket}", {"prefixes": batch})

    def _send(self, method: str, url: str, payload: dict) -> object:
        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                response = client.request(method, url, json=payload, headers=self._headers)
        except httpx.HTTPError as exc:
            raise StorageUnavailableError("storage unreachable") from exc
        if response.status_code >= 500:
            raise StorageUnavailableError(f"storage error {response.status_code}")
        if response.status_code >= 400:
            raise StorageRejectedError(response.status_code)
        return response.json()

    def download(self, path: str) -> bytes:
        url = f"{self._base}/authenticated/{self._bucket}/{quote(path)}"
        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                response = client.get(url, headers=self._headers)
        except httpx.HTTPError as exc:
            raise StorageUnavailableError("storage unreachable") from exc
        if response.status_code in (400, 404):
            raise StorageObjectMissingError(path)
        if response.status_code >= 500:
            raise StorageUnavailableError(f"storage error {response.status_code}")
        if response.status_code >= 400:
            raise StorageRejectedError(response.status_code)
        return response.content


def _relative(value: object) -> str:
    """Keep only Storage's relative ``/object/...`` path; never trust a full URL."""
    if not isinstance(value, str):
        raise StorageUnavailableError("storage returned no signed URL")
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or not parts.path.startswith("/object/"):
        raise StorageUnavailableError("unexpected signed URL")
    if "token=" not in parts.query or len(value) > 2048:
        raise StorageUnavailableError("unexpected signed URL")
    return value


class SignedObject:
    """Read or create one object through a signed URL (no session, no keys)."""

    def __init__(self, settings: Settings, relative_url: str) -> None:
        self._url = f"{settings.supabase_url}/storage/v1{_relative(relative_url)}"

    def download(self) -> bytes:
        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                response = client.get(self._url)
        except httpx.HTTPError as exc:
            raise StorageUnavailableError("storage unreachable") from exc
        if response.status_code >= 500:
            raise StorageUnavailableError(f"storage error {response.status_code}")
        if response.status_code >= 400:
            raise StorageRejectedError(response.status_code)  # expired or revoked
        return response.content

    def upload(self, data: bytes, content_type: str) -> None:
        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                response = client.put(
                    self._url, content=data, headers={"Content-Type": content_type}
                )
        except httpx.HTTPError as exc:
            raise StorageUnavailableError("storage unreachable") from exc
        if response.status_code >= 500:
            raise StorageUnavailableError(f"storage error {response.status_code}")
        if response.status_code >= 400:
            raise StorageRejectedError(response.status_code)
