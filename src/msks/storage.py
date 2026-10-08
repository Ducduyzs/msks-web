"""BlobStore trên Supabase Storage (bucket riêng tư, chỉ backend truy cập bằng secret key)."""
from __future__ import annotations

from urllib.parse import quote

import httpx

from .errors import TransientError
from .settings import get_settings


class BlobStore:
    def __init__(self) -> None:
        settings = get_settings()
        self.base = f"{settings.supabase_url.rstrip('/')}/storage/v1"
        self.bucket = settings.storage_bucket
        key = settings.supabase_secret_key
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}"}

    def _url(self, key: str) -> str:
        return f"{self.base}/object/{self.bucket}/{quote(key)}"

    def put(self, key: str, data: bytes, content_type: str) -> None:
        response = httpx.post(
            self._url(key),
            content=data,
            headers={**self.headers, "Content-Type": content_type, "x-upsert": "false"},
            timeout=120,
        )
        if response.status_code >= 500:
            raise TransientError(f"Storage {response.status_code}")
        response.raise_for_status()

    def get(self, key: str) -> bytes:
        response = httpx.get(self._url(key), headers=self.headers, timeout=120)
        if response.status_code >= 500:
            raise TransientError(f"Storage {response.status_code}")
        response.raise_for_status()
        return response.content

    def delete(self, keys: list[str]) -> None:
        if not keys:
            return
        response = httpx.request(
            "DELETE", f"{self.base}/object/{self.bucket}", json={"prefixes": keys}, headers=self.headers, timeout=60
        )
        if response.status_code >= 500:
            raise TransientError(f"Storage {response.status_code}")
        response.raise_for_status()

    def list_keys(self, prefix: str) -> list[str]:
        """Liệt kê đệ quy mọi object dưới prefix (Storage trả từng cấp thư mục)."""
        keys: list[str] = []
        response = httpx.post(f"{self.base}/object/list/{self.bucket}", headers=self.headers,
                              json={"prefix": prefix, "limit": 1000}, timeout=30)
        response.raise_for_status()
        for item in response.json():
            path = f"{prefix.rstrip('/')}/{item['name']}" if prefix else item["name"]
            if item.get("id") is None:  # thư mục
                keys.extend(self.list_keys(path))
            else:
                keys.append(path)
        return keys

    def healthy(self) -> bool:
        try:
            response = httpx.get(f"{self.base}/bucket/{self.bucket}", headers=self.headers, timeout=5)
            return response.status_code == 200
        except httpx.HTTPError:
            return False
