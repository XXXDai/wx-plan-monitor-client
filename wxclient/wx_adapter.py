"""微信消息来源适配层。

支持三种后端，由 `monitor.backend` 选择（默认 auto）：

| backend      | 适用微信版本   | 包                        | 监听方式 |
|--------------|----------------|---------------------------|----------|
| `wxauto4`    | **4.1+**（含 4.1.8.107） | `wxauto4` / `wxautox4`（Plus） | 回调 |
| `wxauto`     | 3.9.x（旧版）  | `wxauto`                  | 轮询 `GetListenMessage()` |
| `mock`       | —              | —                         | 读目录里的 json，任何系统可跑 |

两代 API 差别很大，这里的关键差异：
  - 4.x：`AddListenChat(nickname=..., callback=fn)`，回调 `fn(msg, chat)` 在 wxauto 的线程里触发；
         `msg.attr` 才是来源（system/self/friend/other），`msg.type` 是内容类型（text/image/file/...）；
         文件用 `msg.download(dir_path=...)`。
  - 3.9：`GetListenMessage()` 轮询；`msg.type` 混用了来源（sys/time/friend/self）。
统一归一化成 WxMessage，上层（Collector）只管 `poll()`。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("wx")

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
# wxauto4：不需要上报的内容类型
V4_SKIP_TYPES = {"time", "system", "note", "other"}
# wxauto 3.9：type 字段里的系统类消息
LEGACY_SYS_TYPES = {"sys", "time", "recall", "tickle"}


@dataclass
class WxMessage:
    chat: str
    sender: str
    msg_type: str  # text | file | image | video | voice | link | ...
    content: str
    wx_time: str | None = None
    file_path: str | None = None
    raw_type: str = ""  # 后端原始 type（4.x）或 attr
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
    name = "base"

    def start(self, chats: Iterable[str]) -> None:  # pragma: no cover - 接口
        raise NotImplementedError

    def poll(self) -> list[WxMessage]:  # pragma: no cover - 接口
        raise NotImplementedError

    def health(self) -> dict[str, Any]:
        return {"source": self.name}

    def stop(self) -> None:
        pass


def _is_failure(res: Any) -> bool:
    """wxauto 的 WxResponse 是个 dict：成功返回 Chat 实例，失败返回带 status/message 的响应。"""
    if res is None:
        return False  # 老版本没有返回值
    if isinstance(res, dict):
        if "success" in res:
            return not res["success"]
        status = str(res.get("status", "")).strip().lower()
        return status in ("失败", "fail", "failed", "error", "false")
    return False


def _failure_text(res: Any) -> str:
    if isinstance(res, dict):
        return str(res.get("message") or res.get("msg") or res)
    return str(res)


# --------------------------------------------------------------------------- #
# 微信 4.1+：wxauto4 / wxautox4（回调式）
# --------------------------------------------------------------------------- #
class WxAuto4Source(BaseSource):
    """适配微信客户端 4.1+（如 4.1.8.107）。

    wxauto4 的监听是回调式的：回调在库自己的守护线程里触发，
    这里把消息塞进线程安全队列，`poll()` 再排空，保持上层逻辑不变。
    """

    name = "wxauto4"

    def __init__(self, save_pic: bool = False, download_dir: str | Path | None = None):
        self.save_pic = save_pic
        self.download_dir = Path(download_dir) if download_dir else None
        if self.download_dir:
            self.download_dir.mkdir(parents=True, exist_ok=True)
        self.wx: Any = None
        self.pkg = ""
        self.chats: list[str] = []
        self._q: queue.Queue[WxMessage] = queue.Queue()
        self._errors = 0

    # ---------- 启动 ---------- #
    @staticmethod
    def import_wechat() -> tuple[Any, str]:
        """优先 Plus 版（wxautox4，支持到 4.1.9.35），否则免费版（wxauto4，支持到 4.1.8.107）。"""
        last_err: Exception | None = None
        for pkg in ("wxautox4", "wxauto4"):
            try:
                module = __import__(pkg, fromlist=["WeChat"])
                return module.WeChat, pkg
            except ImportError as exc:
                last_err = exc
        raise RuntimeError(
            "未安装微信 4.x 版的 wxauto：请执行 `pip install wxauto4`"
            "（Plus 版为 wxautox4）。当前微信 4.1.8.107 需要用这个包，"
            f"旧的 wxauto 只支持微信 3.9.x。原始错误：{last_err}"
        )

    def start(self, chats: Iterable[str]) -> None:
        WeChat, self.pkg = self.import_wechat()
        self.wx = WeChat()
        log.info("已连接微信客户端（%s）：%s", self.pkg, self.describe_me())

        if hasattr(self.wx, "IsOnline"):
            try:
                if not self.wx.IsOnline():
                    log.warning("微信当前显示为未登录状态，监听可能拿不到消息")
            except Exception as exc:  # noqa: BLE001
                log.debug("IsOnline 调用失败：%s", exc)

        self.chats = [c for c in chats if c]
        if not self.chats:
            raise RuntimeError("monitor.chats 为空，请先配置要监听的群名")

        known = self.session_names()
        for chat in self.chats:
            if known and chat not in known:
                log.warning(
                    "会话列表里没有「%s」（当前可见会话：%s…）。"
                    "群名必须与微信里显示的完全一致；先在微信里打开一次该群再试。",
                    chat,
                    "、".join(known[:8]),
                )
            self._add_listen(chat)

    def _add_listen(self, chat: str) -> None:
        cb = self._make_callback(chat)
        try:
            res = self.wx.AddListenChat(nickname=chat, callback=cb)
        except TypeError:
            # 极少数版本参数名是 who
            res = self.wx.AddListenChat(who=chat, callback=cb)
        if _is_failure(res):
            raise RuntimeError(f"监听群「{chat}」失败：{_failure_text(res)}")
        log.info("已监听群：%s", chat)

    def describe_me(self) -> str:
        for meth in ("GetMyInfo",):
            fn = getattr(self.wx, meth, None)
            if callable(fn):
                try:
                    info = fn()
                    if isinstance(info, dict):
                        return str(info.get("nickname") or info.get("name") or info)
                    return str(info)
                except Exception:  # noqa: BLE001
                    pass
        return str(getattr(self.wx, "nickname", "?"))

    def session_names(self) -> list[str]:
        fn = getattr(self.wx, "GetSession", None)
        if not callable(fn):
            return []
        try:
            out = []
            for s in fn() or []:
                name = getattr(s, "name", None) or getattr(s, "nickname", None)
                if not name and isinstance(s, dict):
                    name = s.get("name") or s.get("nickname")
                if name:
                    out.append(str(name))
            return out
        except Exception as exc:  # noqa: BLE001
            log.debug("GetSession 失败：%s", exc)
            return []

    # ---------- 回调 ---------- #
    def _make_callback(self, chat: str):
        def _cb(msg: Any, chat_obj: Any = None) -> None:
            try:
                name = self._chat_name(chat_obj) or chat
                m = self._normalize(name, msg)
                if m:
                    self._q.put(m)
            except Exception:
                log.exception("处理群「%s」的消息回调时出错", chat)

        return _cb

    @staticmethod
    def _chat_name(chat_obj: Any) -> str:
        if chat_obj is None:
            return ""
        for attr in ("chat_name", "who", "nickname", "name"):
            v = getattr(chat_obj, attr, None)
            if isinstance(v, str) and v:
                return v
        return str(chat_obj) if isinstance(chat_obj, str) else ""

    def _normalize(self, chat: str, msg: Any) -> WxMessage | None:
        # 4.x：attr = 来源（system/self/friend/other），type = 内容类型
        attr = str(getattr(msg, "attr", "") or "").lower()
        mtype = str(getattr(msg, "type", "") or "").lower()
        if attr == "system" or mtype in V4_SKIP_TYPES:
            return None

        sender = ""
        for a in ("sender_remark", "sender", "nickname"):
            v = getattr(msg, a, None)
            if isinstance(v, str) and v:
                sender = v
                break
        if not sender and attr == "self":
            sender = "我"

        content = getattr(msg, "content", "")
        content = content if isinstance(content, str) else str(content)

        wx_time = None
        for a in ("time", "msg_time", "datetime"):
            v = getattr(msg, a, None)
            if v:
                wx_time = str(v)
                break

        file_path = None
        if mtype == "file" or (mtype in ("image", "video") and self.save_pic):
            file_path = self._download(msg, mtype)
            if file_path and not content:
                content = f"[{'文件' if mtype == 'file' else '图片'}] {Path(file_path).name}"
        if not content and mtype != "text":
            content = f"[{mtype}]"  # 图片/语音等不下载，也留一条记录

        m = WxMessage(
            chat=chat,
            sender=sender,
            msg_type=mtype or "text",
            content=content.strip(),
            wx_time=wx_time,
            file_path=file_path,
            raw_type=attr or mtype,
        )
        native_id = getattr(msg, "id", None) or getattr(msg, "hash", None)
        m.local_id = (
            hashlib.sha256(f"{chat}|{native_id}".encode()).hexdigest()[:40]
            if native_id
            else m.compute_local_id()
        )
        if not m.content and not m.file_path:
            return None
        return m

    def _download(self, msg: Any, mtype: str) -> str | None:
        download = getattr(msg, "download", None)
        if not callable(download):
            log.warning("该消息对象没有 download 方法，无法保存%s", mtype)
            return None
        kwargs: dict[str, Any] = {}
        if self.download_dir:
            kwargs["dir_path"] = str(self.download_dir)
        for attempt_kwargs in (kwargs, {}):
            try:
                res = download(**attempt_kwargs)
            except TypeError:
                continue
            except Exception as exc:  # noqa: BLE001
                log.warning("下载%s失败：%s", mtype, exc)
                return None
            path = self._extract_path(res) or self._extract_path(
                getattr(msg, "path", None)
            )
            if path:
                return path
            log.warning("download() 返回了 %r，未能得到可用的文件路径", res)
            return None
        return None

    @staticmethod
    def _extract_path(res: Any) -> str | None:
        if isinstance(res, str) and os.path.isfile(res):
            return res
        if isinstance(res, (list, tuple)):
            for item in res:
                if isinstance(item, str) and os.path.isfile(item):
                    return item
        if isinstance(res, dict):
            for key in ("data", "path", "file_path", "message"):
                v = res.get(key)
                if isinstance(v, str) and os.path.isfile(v):
                    return v
                if isinstance(v, dict):
                    for k2 in ("path", "file_path"):
                        v2 = v.get(k2)
                        if isinstance(v2, str) and os.path.isfile(v2):
                            return v2
        for attr in ("path", "file_path", "data"):
            v = getattr(res, attr, None)
            if isinstance(v, str) and os.path.isfile(v):
                return v
        return None

    # ---------- 取消息 ---------- #
    def poll(self) -> list[WxMessage]:
        out: list[WxMessage] = []
        while True:
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                break
        # 回调式监听没有轮询动作，这里顺便探活
        if not out:
            self._check_alive()
        return out

    def _check_alive(self) -> None:
        fn = getattr(self.wx, "IsOnline", None)
        if not callable(fn):
            return
        try:
            if fn():
                self._errors = 0
                return
            self._errors += 1
        except Exception:  # noqa: BLE001
            self._errors += 1
        if self._errors in (5, 30) or (self._errors and self._errors % 120 == 0):
            log.warning("微信疑似离线/未响应（连续 %d 次探活失败）", self._errors)

    def health(self) -> dict[str, Any]:
        alive = None
        fn = getattr(self.wx, "IsOnline", None)
        if callable(fn):
            try:
                alive = bool(fn())
            except Exception:  # noqa: BLE001
                alive = False
        return {
            "source": self.name,
            "package": self.pkg,
            "chats": self.chats,
            "wechat_online": alive,
            "queued": self._q.qsize(),
        }

    def stop(self) -> None:
        fn = getattr(self.wx, "StopListening", None)
        if callable(fn):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- #
# 微信 3.9.x：老版 wxauto（轮询式）
# --------------------------------------------------------------------------- #
class WxAutoLegacySource(BaseSource):
    """适配微信 3.9.x + 老版 wxauto（`pip install wxauto`）。"""

    name = "wxauto"

    def __init__(self, save_pic: bool = False):
        self.save_pic = save_pic
        self.wx: Any = None
        self.chats: list[str] = []

    def start(self, chats: Iterable[str]) -> None:
        try:
            from wxauto import WeChat  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "未安装 wxauto（pip install wxauto）。注意：老版 wxauto 只支持微信 3.9.x，"
                "微信 4.1+ 请把 monitor.backend 设为 wxauto4 并 `pip install wxauto4`。"
            ) from exc

        self.wx = WeChat()
        log.info("已连接微信客户端（wxauto/3.9）：%s", getattr(self.wx, "nickname", "?"))
        self.chats = [c for c in chats if c]
        if not self.chats:
            raise RuntimeError("monitor.chats 为空，请先配置要监听的群名")
        for chat in self.chats:
            self._add_listen(chat)

    def _add_listen(self, chat: str) -> None:
        add = getattr(self.wx, "AddListenChat", None)
        if add is None:
            raise RuntimeError("当前 wxauto 版本不支持 AddListenChat，请升级")
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
        if raw_type in LEGACY_SYS_TYPES:
            return None

        sender = ""
        for attr in ("sender", "sender_remark", "sender_nickname", "user"):
            v = getattr(msg, attr, None)
            if isinstance(v, str) and v:
                sender = v
                break

        content = getattr(msg, "content", "")
        content = content if isinstance(content, str) else str(content)

        wx_time = None
        for attr in ("time", "msg_time", "datetime"):
            v = getattr(msg, attr, None)
            if v:
                wx_time = str(v)
                break

        file_path = self._extract_file(msg, content)
        if file_path:
            msg_type = "image" if Path(file_path).suffix.lower() in IMAGE_SUFFIXES else "file"
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
        return {"source": self.name, "chats": self.chats, "wechat_online": alive}


# --------------------------------------------------------------------------- #
# 调试来源：目录里的 json 文件
# --------------------------------------------------------------------------- #
class MockSource(BaseSource):
    """把 {chat,sender,content,type,file} 的 json 丢进目录即可模拟一条群消息。

    处理完会把 json 重命名为 *.done，方便反复测试。
    """

    name = "mock"

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
        return {"source": self.name, "dir": str(self.dir)}


# --------------------------------------------------------------------------- #
def build_source(cfg) -> BaseSource:
    mock_dir = cfg.runtime.get("mock_dir")
    backend = str(cfg.monitor.get("backend", "auto")).lower()
    if mock_dir or backend == "mock":
        return MockSource(cfg.abs_path(mock_dir or "mock_in"))

    save_pic = bool(cfg.monitor.get("save_pic", False))
    download_dir = cfg.abs_path(cfg.runtime.get("download_dir") or "data/downloads")

    if backend == "wxauto":
        return WxAutoLegacySource(save_pic=save_pic)
    if backend == "wxauto4":
        return WxAuto4Source(save_pic=save_pic, download_dir=download_dir)

    # auto：微信 4.x 的 wxauto4/wxautox4 优先，装了老包才回退
    try:
        WxAuto4Source.import_wechat()
        return WxAuto4Source(save_pic=save_pic, download_dir=download_dir)
    except RuntimeError as exc:
        try:
            import wxauto  # type: ignore  # noqa: F401
        except ImportError:
            raise exc
        log.warning("未找到 wxauto4，回退到老版 wxauto（仅支持微信 3.9.x）")
        return WxAutoLegacySource(save_pic=save_pic)
