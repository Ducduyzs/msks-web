"""BlobStore trên Supabase Storage (bucket riêng tư, chỉ backend truy cập bằng secret key)."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import httpx

from .errors import TransientError
from .settings import get_settings


class BlobStore:
    def __init__(self, bucket: str | None = None) -> None:
        settings = get_settings()
        self.base = f"{settings.supabase_url.rstrip('/')}/storage/v1"
        self.bucket = bucket or settings.storage_bucket
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

    # ------------------------------------------------------------ media

    def put_file(self, key: str, path: Path, content_type: str) -> None:
        """Tải file lên theo stream (không đọc cả file vào RAM)."""
        with open(path, "rb") as handle:
            response = httpx.post(
                self._url(key),
                content=iter(lambda: handle.read(1024 * 1024), b""),
                headers={**self.headers, "Content-Type": content_type, "x-upsert": "true",
                         "Content-Length": str(path.stat().st_size)},
                timeout=600,
            )
        if response.status_code >= 500:
            raise TransientError(f"Storage {response.status_code}")
        response.raise_for_status()

    def download_to(self, key: str, path: Path) -> int:
        """Tải object về file theo stream; trả số byte."""
        size = 0
        with httpx.stream("GET", self._url(key), headers=self.headers, timeout=600) as response:
            if response.status_code >= 500:
                raise TransientError(f"Storage {response.status_code}")
            response.raise_for_status()
            with open(path, "wb") as handle:
                for chunk in response.iter_bytes(1024 * 1024):
                    handle.write(chunk)
                    size += len(chunk)
        return size

    def stat(self, key: str) -> int | None:
        """Kích thước object (byte) hoặc None nếu chưa có."""
        folder, _, name = key.rpartition("/")
        response = httpx.post(f"{self.base}/object/list/{self.bucket}", headers=self.headers,
                              json={"prefix": folder, "limit": 1000, "search": name}, timeout=30)
        response.raise_for_status()
        for item in response.json():
            if item["name"] == name and item.get("id"):
                return int((item.get("metadata") or {}).get("size") or 0)
        return None

    def signed_upload_url(self, key: str) -> str:
        """URL ký sẵn để trình duyệt PUT đúng object do server chọn (hạn mặc định của Supabase: 2 giờ)."""
        response = httpx.post(f"{self.base}/object/upload/sign/{self.bucket}/{quote(key)}", headers=self.headers, timeout=30)
        if response.status_code >= 500:
            raise TransientError(f"Storage {response.status_code}")
        response.raise_for_status()
        return f"{self.base}{response.json()['url']}"

    def signed_url(self, key: str, expires_in: int = 300) -> str:
        """URL đọc có hạn ngắn — chỉ cấp sau khi API đã kiểm quyền và tombstone."""
        response = httpx.post(f"{self.base}/object/sign/{self.bucket}/{quote(key)}", headers=self.headers,
                              json={"expiresIn": expires_in}, timeout=30)
        if response.status_code >= 500:
            raise TransientError(f"Storage {response.status_code}")
        response.raise_for_status()
        return f"{self.base}{response.json()['signedURL']}"

    def healthy(self) -> bool:
        try:
            response = httpx.get(f"{self.base}/bucket/{self.bucket}", headers=self.headers, timeout=5)
            return response.status_code == 200
        except httpx.HTTPError:
            return False
