#!/usr/bin/env python3
"""用本机 config.yaml 的真实凭证测试公网消息上报，不打印密钥。"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import subprocess
import time
import uuid
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
PUBLIC_HOST = "monitor.xdai.top"
PUBLIC_IP = "47.237.103.27"


def main() -> int:
    parser = argparse.ArgumentParser(description="用本机客户端密钥测试公网消息上报")
    parser.add_argument("--config", default=BASE_DIR / "config.yaml", help="本机 config.yaml 路径")
    parser.add_argument("--content", default="[公网域名上传测试] 当前本地客户端")
    args = parser.parse_args()

    config_path = Path(args.config)
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    server = cfg.get("server") or {}
    client_id = str(server.get("client_id") or "")
    secret = str(server.get("secret") or "")
    if not client_id or not secret:
        raise SystemExit(f"{config_path} 缺少 server.client_id 或 server.secret")

    path = "/api/v1/messages"
    body = json.dumps(
        {
            "messages": [
                {
                    "local_id": f"local-upload-test-{uuid.uuid4()}",
                    "chat": "策略之BTC基金",
                    "sender": "XDai",
                    "msg_type": "text",
                    "content": args.content,
                    "wx_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
            ]
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    timestamp = f"{time.time():.3f}"
    body_hash = hashlib.sha256(body).hexdigest()
    canonical = f"{client_id}\n{timestamp}\n{path}\n{body_hash}"
    signature = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()

    # --resolve keeps the request on the production domain while bypassing a local DNS/VPN issue.
    result = subprocess.run(
        [
            "curl",
            "--fail-with-body",
            "--silent",
            "--show-error",
            "--resolve",
            f"{PUBLIC_HOST}:80:{PUBLIC_IP}",
            "-X",
            "POST",
            f"http://{PUBLIC_HOST}{path}",
            "-H",
            "Content-Type: application/json",
            "-H",
            f"X-Client-Id: {client_id}",
            "-H",
            f"X-Timestamp: {timestamp}",
            "-H",
            f"X-Signature: {signature}",
            "--data-binary",
            "@-",
        ],
        input=body,
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
