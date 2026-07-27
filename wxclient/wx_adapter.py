"""微信消息来源适配层。

后端由 `monitor.backend` 选择（默认 auto）：

| backend   | 适用微信版本            | 包                       | 监听方式 |
|-----------|-------------------------|--------------------------|----------|
| `wxauto4` | **4.1+**（含 4.1.8.107）| `wxauto4`(免费) / `wxautox4`(Plus) | 见下 |
| `wxauto`  | 3.9.x（旧版）           | `wxauto`                 | 轮询 `GetListenMessage()` |
| `mock`    | —                       | —                        | 读目录里的 json，任何系统可跑 |

⚠️ 关于 wxauto4 免费版 vs Plus 版（这是本文件复杂度的根源）：

  免费版 wxauto4 (cluic 41.x) 只有：ChatWith / GetSession / GetAllMessage / SendMsg /
  消息属性 type,attr,sender,content,id,hash。
  **AddListenChat / GetNextNewMessage / KeepRunning / RemoveListenChat 以及
  FileMessage.download() 全是 Plus(wxautox4) 专属**。

  因此本适配层对 wxauto4 做能力探测：
    - 有 AddListenChat（= Plus）→ 回调式监听，文件用 msg.download()，最省资源。
    - 没有（= 免费版）        → 轮询：ChatWith(群) + GetAllMessage()，比对出新消息。
      免费版**无法下载群文件**，所以文件改由 FolderWatchSource 盯微信下载目录来捕获
      （见 monitor.wechat_file_dir），或升级到 Plus。

统一归一化成 WxMessage，上层（Collector）只管调 `poll()`。
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
DOC_SUFFIXES = {".docx", ".doc", ".xlsx", ".xls", ".pdf", ".txt", ".md", ".csv"}
# wxauto4：不需要上报的内容类型
V4_SKIP_TYPES = {"time", "system", "note", "other", "recall"}
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
    raw_type: str = ""  # 后端原始 type / attr
    local_id: str = field(default="")

    def compute_local_id(self) -> str:
        """去重键。有原生消息 id/hash 就用它，否则用 群+人+内容+时间(分钟) 哈希。"""
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
        return False
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


def _msg_key(msg: Any) -> str:
    """消息去重键：优先 id/hash，退化到 attr+type+sender+content 哈希。"""
    for a in ("id", "hash"):
        v = getattr(msg, a, None)
        if v:
            return f"{a}:{v}"
    attr = getattr(msg, "attr", "")
    mtype = getattr(msg, "type", "")
    sender = getattr(msg, "sender", "")
    content = getattr(msg, "content", "")
    return "h:" + hashlib.sha256(
        f"{attr}|{mtype}|{sender}|{content}".encode("utf-8", "ignore")
    ).hexdigest()[:24]


# --------------------------------------------------------------------------- #
# 微信 4.1+：wxauto4（免费轮询）/ wxautox4（Plus 回调）
# --------------------------------------------------------------------------- #
class WxAuto4Source(BaseSource):
    """适配微信客户端 4.1+（如 4.1.8.107）。

    能力探测决定监听方式：
      - Plus（有 AddListenChat）：回调式，回调塞进线程安全队列，poll() 排空。
      - 免费版（无 AddListenChat）：轮询 ChatWith(群)+GetAllMessage()，比对出新消息。
    """

    name = "wxauto4"

    def __init__(
        self,
        save_pic: bool = False,
        download_dir: str | Path | None = None,
        mode: str = "auto",
    ):
        self.save_pic = save_pic
        self.download_dir = Path(download_dir) if download_dir else None
        if self.download_dir:
            self.download_dir.mkdir(parents=True, exist_ok=True)
        self.mode = mode  # auto | listen | poll
        self.wx: Any = None
        self.pkg = ""
        self.chats: list[str] = []
        self.use_listen = False
        self._q: queue.Queue[WxMessage] = queue.Queue()
        self._seen: dict[str, set[str]] = {}     # chat -> 已见消息 key
        self._baselined: set[str] = set()         # 已建立基线的 chat（首轮不补历史）
        self._errors = 0
        self._file_warned = False

    # ---------- 导入 ---------- #
    @staticmethod
    def import_wechat() -> tuple[Any, str]:
        """优先 Plus 版（wxautox4，能监听+下载文件），否则免费版（wxauto4，仅轮询）。"""
        last_err: Exception | None = None
        for pkg in ("wxautox4", "wxauto4"):
            try:
                module = __import__(pkg, fromlist=["WeChat"])
                return module.WeChat, pkg
            except ImportError as exc:
                last_err = exc
        raise RuntimeError(
            "未安装微信 4.x 版的 wxauto：请执行 `pip install wxauto4`"
            "（Plus 版为 wxautox4）。微信 4.1.8.107 需要这个包，"
            f"旧的 wxauto 只支持微信 3.9.x。原始错误：{last_err}"
        )

    # ---------- 启动 ---------- #
    def start(self, chats: Iterable[str]) -> None:
        WeChat, self.pkg = self.import_wechat()
        self.wx = WeChat()
        log.info("已连接微信客户端（%s）：%s", self.pkg, self.describe_me())

        self.chats = [c for c in chats if c]
        if not self.chats:
            raise RuntimeError("monitor.chats 为空，请先配置要监听的群名")

        # 能力探测：决定监听方式
        has_listen = callable(getattr(self.wx, "AddListenChat", None))
        if self.mode == "listen" and not has_listen:
            raise RuntimeError(
                "配置要求 listen 模式，但当前包没有 AddListenChat（免费版 wxauto4 不支持）。"
                "请升级到 Plus 版 wxautox4，或把 monitor.poll_mode 设为 auto/poll。"
            )
        self.use_listen = has_listen if self.mode == "auto" else (self.mode == "listen")

        if self.wx and hasattr(self.wx, "IsOnline"):
            try:
                if not self.wx.IsOnline():
                    log.warning("微信当前显示为未登录状态，可能收不到消息")
            except Exception:  # noqa: BLE001
                pass

        known = self.session_names()
        for chat in self.chats:
            if known and chat not in known:
                log.warning(
                    "会话列表里没有「%s」（当前可见：%s…）。群名必须与微信里显示的完全一致；"
                    "先在微信里打开一次该群。",
                    chat,
                    "、".join(known[:8]),
                )

        if self.use_listen:
            log.info("监听方式：回调（%s 支持 AddListenChat）", self.pkg)
            for chat in self.chats:
                self._add_listen(chat)
        else:
            log.warning(
                "监听方式：轮询（%s 为免费版，不支持后台监听）。"
                "注意：免费版无法下载群文件，方案文件请改用 wechat_file_dir 目录监视，"
                "或升级到 Plus 版 wxautox4。",
                self.pkg,
            )
            # 建立基线：把每个群当前已有消息标记为已见，避免上报历史
            for chat in self.chats:
                self._poll_chat(chat, baseline=True)

    def _add_listen(self, chat: str) -> None:
        cb = self._make_callback(chat)
        try:
            res = self.wx.AddListenChat(nickname=chat, callback=cb)
        except TypeError:
            try:
                res = self.wx.AddListenChat(who=chat, callback=cb)
            except TypeError:
                res = self.wx.AddListenChat(chat, cb)
        if _is_failure(res):
            raise RuntimeError(f"监听群「{chat}」失败：{_failure_text(res)}")
        log.info("已监听群：%s", chat)

    def describe_me(self) -> str:
        fn = getattr(self.wx, "GetMyInfo", None)
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
            sessions = fn() or []
            if isinstance(sessions, dict):
                return [str(k) for k in sessions]
            for s in sessions:
                name = getattr(s, "name", None) or getattr(s, "nickname", None)
                if not name and isinstance(s, dict):
                    name = s.get("name") or s.get("nickname")
                if not name and isinstance(s, str):
                    name = s
                if name:
                    out.append(str(name))
            return out
        except Exception as exc:  # noqa: BLE001
            log.debug("GetSession 失败：%s", exc)
            return []

    # ---------- 回调模式（Plus） ---------- #
    def _make_callback(self, chat: str):
        def _cb(msg: Any, chat_obj: Any = None) -> None:
            try:
                name = self._chat_name(chat_obj) or chat
                m = self._normalize(name, msg, allow_download=True)
                if m:
                    self._q.put(m)
            except Exception:
                log.exception("处理群「%s」的消息回调时出错", chat)

        return _cb

    @staticmethod
    def _chat_name(chat_obj: Any) -> str:
        if chat_obj is None:
            return ""
        if isinstance(chat_obj, str):
            return chat_obj
        for attr in ("chat_name", "who", "nickname", "name"):
            v = getattr(chat_obj, attr, None)
            if isinstance(v, str) and v:
                return v
        return ""

    # ---------- 轮询模式（免费版） ---------- #
    def _poll_chat(self, chat: str, baseline: bool = False) -> list[WxMessage]:
        """打开某个群，取全部消息，返回未见过的新消息。baseline=True 时只记录不返回。"""
        try:
            res = self.wx.ChatWith(chat)
            if _is_failure(res):
                log.warning("ChatWith(%s) 失败：%s", chat, _failure_text(res))
                return []
        except Exception as exc:  # noqa: BLE001
            log.warning("ChatWith(%s) 异常：%s", chat, exc)
            return []

        try:
            msgs = self.wx.GetAllMessage() or []
        except Exception as exc:  # noqa: BLE001
            log.warning("GetAllMessage(%s) 异常：%s", chat, exc)
            return []

        seen = self._seen.setdefault(chat, set())
        out: list[WxMessage] = []
        for msg in msgs:
            key = _msg_key(msg)
            if key in seen:
                continue
            seen.add(key)
            if baseline:
                continue
            # 免费版下载文件会失败，这里不尝试 download（allow_download=False）
            m = self._normalize(chat, msg, allow_download=False)
            if m:
                out.append(m)

        # 控制 seen 体积（长会话）
        if len(seen) > 4000:
            self._seen[chat] = set(list(seen)[-2000:])
        if baseline:
            self._baselined.add(chat)
            log.info("群「%s」建立基线：已有 %d 条历史消息不再上报", chat, len(seen))
        return out

    # ---------- 归一化 ---------- #
    def _normalize(self, chat: str, msg: Any, allow_download: bool) -> WxMessage | None:
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
        is_file_like = mtype == "file" or (mtype in ("image", "video") and self.save_pic)
        if is_file_like:
            if allow_download:
                file_path = self._download(msg, mtype)
            elif mtype == "file" and not self._file_warned:
                self._file_warned = True
                log.warning(
                    "收到文件消息，但免费版 wxauto4 无法下载文件（download 是 Plus 专属）。"
                    "请配置 monitor.wechat_file_dir 让目录监视器捕获方案文件，或升级 Plus 版。"
                )
            if file_path and not content:
                content = f"[{'文件' if mtype == 'file' else '图片'}] {Path(file_path).name}"

        if not content and mtype != "text":
            content = f"[{mtype}]"

        m = WxMessage(
            chat=chat,
            sender=sender,
            msg_type=mtype or "text",
            content=content.strip(),
            wx_time=wx_time,
            file_path=file_path,
            raw_type=attr or mtype,
        )
        native = getattr(msg, "id", None) or getattr(msg, "hash", None)
        m.local_id = (
            hashlib.sha256(f"{chat}|{native}".encode()).hexdigest()[:40]
            if native
            else m.compute_local_id()
        )
        if not m.content and not m.file_path:
            return None
        return m

    def _download(self, msg: Any, mtype: str) -> str | None:
        download = getattr(msg, "download", None)
        if not callable(download):
            return None
        kwargs: dict[str, Any] = {}
        if self.download_dir:
            kwargs["dir_path"] = str(self.download_dir)
        for attempt in (kwargs, {}):
            try:
                res = download(**attempt)
            except TypeError:
                continue
            except Exception as exc:  # noqa: BLE001
                log.warning("下载%s失败：%s", mtype, exc)
                return None
            path = self._extract_path(res) or self._extract_path(getattr(msg, "path", None))
            if path:
                return path
            log.warning("download() 返回 %r，未取得可用文件路径", res)
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
        if self.use_listen:
            out: list[WxMessage] = []
            while True:
                try:
                    out.append(self._q.get_nowait())
                except queue.Empty:
                    break
            if not out:
                self._check_alive()
            return out
        # 轮询模式：逐个群取新消息
        out = []
        for chat in self.chats:
            out.extend(self._poll_chat(chat, baseline=False))
        return out

    def _check_alive(self) -> None:
        fn = getattr(self.wx, "IsOnline", None)
        if not callable(fn):
            return
        try:
            ok = bool(fn())
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            self._errors = 0
        else:
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
            "mode": "listen" if self.use_listen else "poll",
            "chats": self.chats,
            "wechat_online": alive,
            "queued": self._q.qsize() if self.use_listen else 0,
        }

    def stop(self) -> None:
        for meth in ("StopListening",):
            fn = getattr(self.wx, meth, None)
            if callable(fn):
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    pass


# --------------------------------------------------------------------------- #
# 目录监视：捕获微信自动下载的方案文件（免费版下载文件的替代方案）
# --------------------------------------------------------------------------- #
class FolderWatchSource(BaseSource):
    """盯一个目录，新出现的方案文件（docx/xlsx/pdf/...）当作一条文件消息上报。

    用于免费版 wxauto4 无法 download() 的情况：让微信自动下载群文件到某目录
    （微信设置里可开自动下载，或用户手动下载后落到默认目录），本源负责捕获。
    因为拿不到"谁在哪个群发的"，chat/sender 用配置里的占位值。
    """

    name = "folderwatch"

    def __init__(
        self,
        directory: str | Path,
        chat_label: str = "微信文件",
        sender_label: str = "微信",
        suffixes: Iterable[str] | None = None,
        recursive: bool = True,
        stable_seconds: float = 3.0,
    ):
        self.dir = Path(directory)
        self.chat_label = chat_label
        self.sender_label = sender_label
        self.suffixes = {s.lower() for s in (suffixes or DOC_SUFFIXES)}
        self.recursive = recursive
        self.stable_seconds = stable_seconds
        self._seen: set[str] = set()          # 已上报文件的 key(size+mtime+path)
        self._pending: dict[str, float] = {}  # 路径 -> 首次见到的时间（等文件写完）

    def _iter_files(self):
        if not self.dir.exists():
            return
        globber = self.dir.rglob("*") if self.recursive else self.dir.glob("*")
        for p in globber:
            try:
                if p.is_file() and p.suffix.lower() in self.suffixes:
                    yield p
            except OSError:
                continue

    def start(self, chats: Iterable[str]) -> None:
        if not self.dir.exists():
            log.warning("目录监视：路径不存在（暂时）：%s（出现后会自动开始捕获）", self.dir)
        else:
            # 基线：已有文件不补报
            for p in self._iter_files():
                self._seen.add(self._key(p))
            log.info("目录监视启动：%s（已有 %d 个文件设为基线）", self.dir, len(self._seen))

    @staticmethod
    def _key(p: Path) -> str:
        try:
            st = p.stat()
            return f"{p.resolve()}|{st.st_size}"
        except OSError:
            return str(p.resolve())

    def poll(self) -> list[WxMessage]:
        out: list[WxMessage] = []
        now = time.time()
        current: set[str] = set()
        for p in self._iter_files():
            key = self._key(p)
            current.add(key)
            if key in self._seen:
                continue
            # 等文件大小稳定（避免上传下载到一半的文件）
            first = self._pending.setdefault(key, now)
            if now - first < self.stable_seconds:
                continue
            self._seen.add(key)
            self._pending.pop(key, None)
            m = WxMessage(
                chat=self.chat_label,
                sender=self.sender_label,
                msg_type="file",
                content=f"[文件] {p.name}",
                wx_time=time.strftime("%Y-%m-%d %H:%M:%S"),
                file_path=str(p),
                raw_type="folderwatch",
            )
            m.local_id = m.compute_local_id()
            out.append(m)
            log.info("目录监视捕获文件：%s", p.name)
        # 清理已消失的 pending
        self._pending = {k: v for k, v in self._pending.items() if k in current}
        return out

    def health(self) -> dict[str, Any]:
        return {"source": self.name, "dir": str(self.dir), "seen": len(self._seen)}


# --------------------------------------------------------------------------- #
# 组合源：wxauto 负责文本，FolderWatch 负责文件
# --------------------------------------------------------------------------- #
class CompositeSource(BaseSource):
    name = "composite"

    def __init__(self, sources: list[BaseSource]):
        self.sources = sources

    def start(self, chats: Iterable[str]) -> None:
        chats = list(chats)
        for s in self.sources:
            s.start(chats)

    def poll(self) -> list[WxMessage]:
        out: list[WxMessage] = []
        for s in self.sources:
            try:
                out.extend(s.poll())
            except Exception:
                log.exception("子源 %s poll 出错", s.name)
        return out

    def health(self) -> dict[str, Any]:
        return {"source": self.name, "children": [s.health() for s in self.sources]}

    def stop(self) -> None:
        for s in self.sources:
            s.stop()


# --------------------------------------------------------------------------- #
# 微信 3.9.x：老版 wxauto（轮询式）
# --------------------------------------------------------------------------- #
class WxAutoLegacySource(BaseSource):
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
    file_dir = cfg.monitor.get("wechat_file_dir")

    if backend == "wxauto":
        return WxAutoLegacySource(save_pic=save_pic)

    if backend in ("wxauto4", "auto"):
        if backend == "auto":
            # 没装 4.x 包但装了老 wxauto 时回退
            try:
                WxAuto4Source.import_wechat()
            except RuntimeError as exc:
                try:
                    import wxauto  # type: ignore  # noqa: F401
                except ImportError:
                    raise exc
                log.warning("未找到 wxauto4，回退到老版 wxauto（仅微信 3.9.x）")
                return WxAutoLegacySource(save_pic=save_pic)

        wx = WxAuto4Source(
            save_pic=save_pic,
            download_dir=download_dir,
            mode=str(cfg.monitor.get("poll_mode", "auto")).lower(),
        )
        if file_dir:
            watcher = FolderWatchSource(
                cfg.abs_path(file_dir),
                chat_label=(cfg.monitor.get("chats") or ["微信文件"])[0],
                sender_label="微信",
                suffixes=cfg.monitor.get("upload_suffixes"),
            )
            return CompositeSource([wx, watcher])
        return wx

    raise RuntimeError(f"未知 backend：{backend}")
