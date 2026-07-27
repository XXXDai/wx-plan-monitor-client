"""客户端配置：config.yaml + 环境变量。

注意：本目录是公开仓库，config.yaml 已在 .gitignore 中，密钥只留在本机。
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULTS: dict[str, Any] = {
    "server": {
        "base_url": "http://127.0.0.1:8000",
        "client_id": "wx-pc-1",
        "secret": "",
        "timeout": 30,
        "verify_ssl": True,
    },
    "monitor": {
        # auto = 优先用微信 4.1+ 的 wxauto4/wxautox4，找不到才回退老版 wxauto(3.9.x)
        # 可选：auto | wxauto4 | wxauto | mock
        "backend": "auto",
        # wxauto4 监听方式：auto=有 AddListenChat(Plus) 就回调，否则轮询；listen|poll 强制
        "poll_mode": "auto",
        # 免费版 wxauto4 无法下载群文件，填微信"文件下载目录"让目录监视器捕获方案文件
        # 例：C:\\Users\\你\\Documents\\xwechat_files\\<wxid>\\msg\\file
        "wechat_file_dir": "",
        "chats": [],  # 要监听的群名（必须与微信里显示的名称完全一致）
        "poll_interval": 10,  # 秒，每隔多久轮询一次群消息
        "upload_suffixes": [".docx", ".doc", ".xlsx", ".xls", ".pdf", ".txt", ".md", ".csv"],
        "send_text": True,  # 是否上报文本消息
        "senders_only": [],  # 非空时只上报这些人的文本消息（文件永远上报）
        "ignore_self": True,  # 忽略自己发的消息
        "save_pic": False,  # 是否让 wxauto 下载图片（图片不解析，默认关）
    },
    "runtime": {
        "db_path": "data/outbox.db",
        "log_file": "data/client.log",
        "download_dir": "data/downloads",  # wxauto4 下载群文件的落地目录
        "batch_size": 20,
        "send_interval": 3,  # 上报线程轮询间隔（秒）
        "max_attempts": 0,  # 0 = 无限重试（一直放在队列里）
        "heartbeat_interval": 120,
        "mock_dir": "",  # 调试用：从该目录读模拟消息，非 Windows 也能跑通链路
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, data: dict[str, Any], path: Path | None):
        self._data = data
        self.path = path

    @property
    def server(self) -> dict:
        return self._data["server"]

    @property
    def monitor(self) -> dict:
        return self._data["monitor"]

    @property
    def runtime(self) -> dict:
        return self._data["runtime"]

    def abs_path(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else BASE_DIR / p


def load_config(path: str | os.PathLike | None = None) -> Config:
    p = Path(path) if path else Path(os.getenv("WXCLIENT_CONFIG", BASE_DIR / "config.yaml"))
    raw: dict = {}
    if p.exists():
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    data = _deep_merge(DEFAULTS, raw)

    # 环境变量优先
    if v := os.getenv("BARK_SERVER_URL"):
        data["server"]["base_url"] = v
    if v := os.getenv("BARK_CLIENT_ID"):
        data["server"]["client_id"] = v
    if v := os.getenv("BARK_CLIENT_SECRET"):
        data["server"]["secret"] = v
    if v := os.getenv("WXCLIENT_MOCK_DIR"):
        data["runtime"]["mock_dir"] = v

    return Config(data, p if p.exists() else None)
