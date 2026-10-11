"""Private file storage through the Supabase Storage API.

Requests carry the signed-in user's own access token (never the service
role key), so Storage's row-level security policies independently enforce
that users can only touch objects under their own ``<user_id>/`` folder.
"""

from __future__ import annotations

from urllib.parse import quote

import httpx

from app.config import Settings

_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


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
