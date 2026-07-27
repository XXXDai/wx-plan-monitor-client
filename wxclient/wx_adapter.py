"""微信消息来源适配层。

wxauto 各版本 API 差异较大（3.9.8 / 3.9.11 / plus 的监听与文件下载方式都不一样），
所以这里统一用 getattr 探测 + 多路兜底，并把结果规范成 WxMessage。
另外提供 MockSource：在 macOS/Linux 上放几个 json 文件就能把整条链路跑通。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("wx")

SYS_TYPES = {"sys", "time", "recall", "tickle"}


@dataclass
class WxMessage:
    chat: str
    sender: str
    msg_type: str  # text | file | image | other
    content: str
    wx_time: str | None = None
    file_path: str | None = None
    raw_type: str = ""
    local_id: str = field(default="")

    def compute_local_id(self) -> str:
        """去重键。有原生消息 id 就用它，否则用 群+人+内容+时间(分钟) 哈希。"""
        base = "|".join(
            [
                self.chat,
                self.sender,
                self.msg_type,
                self.content,
                self.wx_time or time.strftime("%Y-%m-%d %H:%M"),
                Path(self.file_path).name if self.file_path else "",
            ]
        )
        return hashlib.sha256(base.encode("utf-8")).hexdigest()[:40]


class BaseSource:
    def start(self, chats: Iterable[str]) -> None:  # pragma: no cover - 接口
        raise NotImplementedError

    def poll(self) -> list[WxMessage]:  # pragma: no cover - 接口
        raise NotImplementedError

    def health(self) -> dict[str, Any]:
        return {}


# --------------------------------------------------------------------------- #
# 真实来源：wxauto（仅 Windows）
# --------------------------------------------------------------------------- #
class WxAutoSource(BaseSource):
    def __init__(self, save_pic: bool = False):
        self.save_pic = save_pic
        self.wx: Any = None
        self.chats: list[str] = []

    def start(self, chats: Iterable[str]) -> None:
        try:
            from wxauto import WeChat  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "未安装 wxauto（pip install wxauto），或当前不是 Windows 环境。"
                "本机调试可在 config.yaml 里设置 runtime.mock_dir 走模拟模式。"
            ) from exc

        self.wx = WeChat()
        nickname = getattr(self.wx, "nickname", None) or getattr(self.wx, "self_name", "?")
        log.info("已连接微信客户端：%s", nickname)

        self.chats = [c for c in chats if c]
        if not self.chats:
            raise RuntimeError("monitor.chats 为空，请先配置要监听的群名")

        for chat in self.chats:
            self._add_listen(chat)

    def _add_listen(self, chat: str) -> None:
        add = getattr(self.wx, "AddListenChat", None)
        if add is None:
            raise RuntimeError("当前 wxauto 版本不支持 AddListenChat，请升级 wxauto")
        # 不同版本参数名不同，逐个尝试
        for kwargs in (
            {"who": chat, "savepic": self.save_pic, "savefile": True, "savevoice": False},
            {"who": chat, "savepic": self.save_pic, "savefile": True},
            {"who": chat},
        ):
            try:
                add(**kwargs)
                log.info("已监听群：%s（参数 %s）", chat, list(kwargs))
                return
            except TypeError:
                continue
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"监听群「{chat}」失败：{exc}") from exc
        raise RuntimeError(f"监听群「{chat}」失败：AddListenChat 参数不兼容")

    def poll(self) -> list[WxMessage]:
        get = getattr(self.wx, "GetListenMessage", None)
        if get is None:
            raise RuntimeError("当前 wxauto 版本不支持 GetListenMessage")
        raw = get()
        out: list[WxMessage] = []
        if not raw:
            return out

        # 返回可能是 {chatWnd: [msg]} 或 {chat_name: [msg]}
        items = raw.items() if isinstance(raw, dict) else [(None, raw)]
        for who, msgs in items:
            chat_name = self._who_name(who)
            for msg in msgs or []:
                m = self._normalize(chat_name, msg)
                if m:
                    out.append(m)
        return out

    @staticmethod
    def _who_name(who: Any) -> str:
        for attr in ("who", "nickname", "name"):
            v = getattr(who, attr, None)
            if isinstance(v, str) and v:
                return v
        return str(who) if who is not None else ""

    def _normalize(self, chat: str, msg: Any) -> WxMessage | None:
        raw_type = str(getattr(msg, "type", "") or "").lower()
        if raw_type in SYS_TYPES:
            return None

        sender = ""
        for attr in ("sender", "sender_remark", "sender_nickname", "user"):
            v = getattr(msg, attr, None)
            if isinstance(v, str) and v:
                sender = v
                break

        content = getattr(msg, "content", "")
        if not isinstance(content, str):
            content = str(content)

        wx_time = None
        for attr in ("time", "msg_time", "datetime"):
            v = getattr(msg, attr, None)
            if v:
                wx_time = str(v)
                break

        file_path = self._extract_file(msg, content)
        if file_path:
            msg_type = "image" if Path(file_path).suffix.lower() in (
                ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"
            ) else "file"
            if not content or content == file_path:
                content = f"[{'图片' if msg_type == 'image' else '文件'}] {Path(file_path).name}"
        elif raw_type in ("file", "image", "voice", "video", "link", "card", "emotion"):
            msg_type = raw_type
        else:
            msg_type = "text"

        m = WxMessage(
            chat=chat,
            sender=sender or ("我" if raw_type == "self" else ""),
            msg_type=msg_type,
            content=content.strip(),
            wx_time=wx_time,
            file_path=file_path,
            raw_type=raw_type,
        )
        # 原生消息 id 更可靠
        native_id = getattr(msg, "id", None) or getattr(msg, "msg_id", None)
        m.local_id = (
            hashlib.sha256(f"{chat}|{native_id}".encode()).hexdigest()[:40]
            if native_id
            else m.compute_local_id()
        )
        if not m.content and not m.file_path:
            return None
        return m

    @staticmethod
    def _extract_file(msg: Any, content: str) -> str | None:
        """拿到落到本地磁盘的文件路径：属性 -> download() -> content 本身是路径。"""
        for attr in ("path", "file_path", "filepath", "savepath"):
            v = getattr(msg, attr, None)
            if isinstance(v, str) and v and os.path.isfile(v):
                return v

        download = getattr(msg, "download", None)
        if callable(download):
            try:
                res = download()
                if isinstance(res, str) and os.path.isfile(res):
                    return res
                if isinstance(res, (list, tuple)):
                    for item in res:
                        if isinstance(item, str) and os.path.isfile(item):
                            return item
                for attr in ("path", "file_path"):
                    v = getattr(msg, attr, None)
                    if isinstance(v, str) and os.path.isfile(v):
                        return v
            except Exception as exc:  # noqa: BLE001
                log.warning("下载文件失败：%s", exc)

        if content and len(content) < 400 and os.path.isfile(content):
            return content
        return None

    def health(self) -> dict[str, Any]:
        alive = True
        try:
            alive = bool(getattr(self.wx, "GetSessionList", lambda: True)())
        except Exception:  # noqa: BLE001
            alive = False
        return {"source": "wxauto", "chats": self.chats, "wechat_alive": alive}


# --------------------------------------------------------------------------- #
# 调试来源：目录里的 json 文件
# --------------------------------------------------------------------------- #
class MockSource(BaseSource):
    """把 {chat,sender,content,type,file} 的 json 丢进目录即可模拟一条群消息。

    处理完会把 json 重命名为 *.done，方便反复测试。
    """

    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def start(self, chats: Iterable[str]) -> None:
        log.warning("【模拟模式】从 %s 读取消息（不会连接微信）", self.dir)

    def poll(self) -> list[WxMessage]:
        out: list[WxMessage] = []
        for path in sorted(self.dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                log.warning("模拟消息 %s 解析失败：%s", path.name, exc)
                path.rename(path.with_suffix(".bad"))
                continue

            file_path = data.get("file")
            if file_path:
                fp = Path(file_path)
                if not fp.is_absolute():
                    fp = self.dir / fp
                file_path = str(fp) if fp.is_file() else None
                if not file_path:
                    log.warning("模拟消息 %s 指向的文件不存在：%s", path.name, data.get("file"))

            m = WxMessage(
                chat=data.get("chat", "测试群"),
                sender=data.get("sender", "测试用户"),
                msg_type=data.get("type") or ("file" if file_path else "text"),
                content=data.get("content", ""),
                wx_time=data.get("time") or time.strftime("%Y-%m-%d %H:%M:%S"),
                file_path=file_path,
                raw_type="mock",
            )
            m.local_id = m.compute_local_id()
            out.append(m)
            path.rename(path.with_suffix(".json.done"))
        return out

    def health(self) -> dict[str, Any]:
        return {"source": "mock", "dir": str(self.dir)}


def build_source(cfg) -> BaseSource:
    mock_dir = cfg.runtime.get("mock_dir")
    if mock_dir:
        return MockSource(cfg.abs_path(mock_dir))
    return WxAutoSource(save_pic=bool(cfg.monitor.get("save_pic", False)))
