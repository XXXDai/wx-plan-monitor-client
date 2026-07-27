"""上报到服务端：HMAC-SHA256 签名 + 重试。

签名规则（与服务端 app/security.py 完全一致）：
    canonical = f"{client_id}\\n{timestamp}\\n{path}\\n{payload_sha256}"
    X-Signature = hex(hmac_sha256(secret, canonical))
payload_sha256：
    JSON  -> sha256(请求体字节)
    文件  -> sha256(meta_json_bytes + b"|" + 文件sha256hex)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger("uploader")


class UploadError(Exception):
    """可重试的上报失败。"""


class Uploader:
    def __init__(self, cfg) -> None:
        s = cfg.server
        self.base_url = str(s["base_url"]).rstrip("/")
        self.client_id = str(s["client_id"])
        self.secret = str(s.get("secret") or "")
        self.timeout = float(s.get("timeout", 30))
        self.verify = bool(s.get("verify_ssl", True))
        if not self.secret:
            raise RuntimeError(
                "未配置上报密钥：请在 config.yaml 的 server.secret 或环境变量 "
                "BARK_CLIENT_SECRET 中填入与服务端一致的 secret"
            )
        self.session = requests.Session()

    # ---------------- 签名 ---------------- #
    def _headers(self, path: str, payload_hash: str) -> dict[str, str]:
        ts = f"{time.time():.3f}"
        canonical = f"{self.client_id}\n{ts}\n{path}\n{payload_hash}"
        sig = hmac.new(
            self.secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return {
            "X-Client-Id": self.client_id,
            "X-Timestamp": ts,
            "X-Signature": sig,
        }

    # ---------------- 请求 ---------------- #
    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = self._headers(path, hashlib.sha256(body).hexdigest())
        headers["Content-Type"] = "application/json"
        try:
            resp = self.session.post(
                self.base_url + path,
                data=body,
                headers=headers,
                timeout=self.timeout,
                verify=self.verify,
            )
        except requests.RequestException as exc:
            raise UploadError(f"网络错误：{exc}") from exc
        return self._parse(resp, path)

    def _parse(self, resp: requests.Response, path: str) -> dict[str, Any]:
        if resp.status_code >= 500 or resp.status_code == 429:
            raise UploadError(f"{path} HTTP {resp.status_code}: {resp.text[:200]}")
        if resp.status_code >= 400:
            # 4xx 多为签名/格式问题，重试也没用，但仍抛出让上层记录
            raise UploadError(f"{path} HTTP {resp.status_code}（请检查配置）: {resp.text[:300]}")
        try:
            return resp.json()
        except ValueError:
            return {"ok": True, "raw": resp.text[:200]}

    # ---------------- 业务 ---------------- #
    def send_messages(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        return self._post_json("/api/v1/messages", {"messages": messages})

    def send_file(self, file_path: str | Path, meta: dict[str, Any]) -> dict[str, Any]:
        path_obj = Path(file_path)
        if not path_obj.is_file():
            raise FileNotFoundError(f"文件不存在：{path_obj}")
        content = path_obj.read_bytes()
        file_sha = hashlib.sha256(content).hexdigest()
        meta = {**meta, "filename": meta.get("filename") or path_obj.name}
        meta_str = json.dumps(meta, ensure_ascii=False)
        payload_hash = hashlib.sha256(
            meta_str.encode("utf-8") + b"|" + file_sha.encode("utf-8")
        ).hexdigest()

        api_path = "/api/v1/files"
        headers = self._headers(api_path, payload_hash)
        try:
            resp = self.session.post(
                self.base_url + api_path,
                files={"file": (path_obj.name, content, "application/octet-stream")},
                data={"meta": meta_str},
                headers=headers,
                timeout=max(self.timeout, 60),
                verify=self.verify,
            )
        except requests.RequestException as exc:
            raise UploadError(f"网络错误：{exc}") from exc
        return self._parse(resp, api_path)

    def heartbeat(self, info: dict[str, Any]) -> dict[str, Any]:
        return self._post_json("/api/v1/heartbeat", info)

    def ping(self) -> dict[str, Any]:
        try:
            resp = self.session.get(
                self.base_url + "/healthz", timeout=self.timeout, verify=self.verify
            )
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            raise UploadError(f"服务端不可达：{exc}") from exc
