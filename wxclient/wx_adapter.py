"""微信消息来源适配层（只支持免费版 wxauto4）。

后端由 `monitor.backend` 选择（默认 auto）：

| backend   | 适用微信版本            | 包                | 监听方式 |
|-----------|-------------------------|-------------------|----------|
| `wxauto4` | **4.1+**（含 4.1.8.107）| `wxauto4`（免费）  | 轮询 ChatWith+GetAllMessage |
| `wxauto`  | 3.9.x（旧版）           | `wxauto`（免费）   | 轮询 GetListenMessage |
| `mock`    | —                       | —                 | 读目录里的 json，任何系统可跑 |

⚠️ 只用免费版。免费版 wxauto4 (cluic 41.x) 的能力有限：只有 ChatWith / GetSession /
   GetAllMessage / SendMsg，消息属性 type,attr,sender,content,id,hash。
   AddListenChat（后台监听）和 FileMessage.download()（下载文件）都是 Plus(wxautox4)
   专属，**本项目不使用、也不安装 Plus 版**。因此：

     - 文本：轮询 —— 每隔 poll_interval 秒挨个 ChatWith(群) + GetAllMessage()，比对出新消息。
     - 文件：免费版无法下载群文件，改由 FolderWatchSource 盯微信文件下载目录来捕获
             （见 monitor.wechat_file_dir）。

统一归一化成 WxMessage，上层（Collector）只管调 `poll()`。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
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
    # 来源侧的会话内去重键；上层处理失败时用它回滚，让这条消息下轮能重新读到
    dedup_key: str = field(default="")

    def compute_local_id(self, occurrence: int = 0, stamp: str = "") -> str:
        """服务端去重用的稳定 id：群+人+类型+内容+时间锚点+该锚点下同内容出现次序。

        三条硬规则：
        · **不掺当前时刻**——否则同一条历史消息每次重扫都算出新 id，服务端按
          local_id 去重就失效，同一句话反复入库、甚至反复触发方案复核；
        · **也不掺"今天是几号"**——那样一过零点，还显示在窗口里的消息就换了 id，
          等于每天零点把整窗消息重报一遍（见 _time_anchors）；
        · **不用 wxauto4 的 id/hash**——实测聊天窗口重新渲染后这两个值会变，
          等于每次重渲染都把整窗消息当成新消息。
        用"时间锚点 + 同内容次序"既能在重渲染后复现同一个 id，
        又能区分同一个人重复发的同样内容（比如分别回两条指令的两个"好的"）。
        """
        base = "|".join(
            [
                self.chat,
                self.sender,
                self.msg_type,
                self.content,
                self.wx_time or stamp or "",
                Path(self.file_path).name if self.file_path else "",
                str(occurrence),
            ]
        )
        return hashlib.sha256(base.encode("utf-8")).hexdigest()[:40]


class BaseSource:
    name = "base"

    def start(self, chats: Iterable[str]) -> None:  # pragma: no cover - 接口
        raise NotImplementedError

    def poll(self) -> list[WxMessage]:  # pragma: no cover - 接口
        raise NotImplementedError

    def snapshot(self, chat: str) -> list[WxMessage] | None:
        """一次性读某个会话的当前消息（不影响正常轮询的去重状态）。

        专给"打卡检测"用：只读一次、读完就地判断，内容不入队、不上报。
        返回 None 表示没读到（会话打不开）。
        """
        return None

    def forget(self, chat: str, dedup_key: str) -> None:
        """撤销"已见"标记，让这条消息下一轮重新读到。

        上层处理某条消息失败时必须调用：来源侧是"读到就标记已见"，
        若上层没入队成功又不回滚，这条消息就永久丢了。
        """
        return None

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


def _extract_wx_time(msg: Any) -> str | None:
    """取消息自身的发送时间（微信显示的时间）。

    这是去重最可靠的关键字段：同一条消息无论被重扫多少次，它的发送时间都不变。
    wxauto4 的消息对象有 time / time_str（见其 msgs/msg 模块），但不同消息类型
    未必都有值，所以多个字段依次尝试；**跳过可调用对象**——否则 getattr 取到方法时
    `if v:` 为真，会把 "<bound method ...>" 当成时间写进去。
    """
    for attr in ("time_str", "time", "msg_time", "datetime", "create_time", "timestamp"):
        v = getattr(msg, attr, None)
        if v is None or callable(v):
            continue
        text = str(v).strip()
        if text and text.lower() not in ("none", "null", ""):
            return text
    return None


_TIME_TEXT_RE = re.compile(
    r"^(?:\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?\s*)?(?:昨天|前天|星期[一二三四五六日天]|周[一二三四五六日天])?"
    r"\s*(?:上午|下午|凌晨|早上|中午|晚上)?\s*\d{1,2}:\d{2}(?::\d{2})?$"
)


def _is_time_divider(msg: Any) -> bool:
    """是不是聊天窗口里那条时间分隔条（wxauto4 的 type == "time"）。"""
    mtype = str(getattr(msg, "type", "") or "").lower()
    attr = str(getattr(msg, "attr", "") or "").lower()
    if mtype == "time" or attr == "time":
        return True
    if mtype in ("", "sys", "system") and not str(getattr(msg, "sender", "") or "").strip():
        content = str(getattr(msg, "content", "") or "").strip()
        return bool(content) and _TIME_TEXT_RE.match(content) is not None
    return False


def _time_anchors(msgs: list[Any]) -> list[str]:
    """给窗口里每条消息标一个"时间锚点"，作为去重键里代表时间的那一段。

    **绝不能用"现在是几号"**：那样一过零点，同一条还显示在窗口里的消息就换了键，
    整窗消息会被当成新消息重新上报一遍（实测 08-10 23:50 的三条在 00:00 又进了一次）。
    锚点只跟消息自身有关，所以跨零点也稳定：
      · 消息自带发送时间就用它；
      · 否则沿用它上方最近那条时间分隔条的文字（微信就是靠这个显示时间的）；
      · 窗口最上面那几条在第一条分隔条之前，就借用下方第一个锚点——同样是稳定值；
      · 整窗一点时间信息都没有才返回空串，由调用方退回"今天几号"（见 _poll_chat）。
    """
    out: list[str] = []
    current = ""
    for msg in msgs:
        own = _extract_wx_time(msg)
        if own:
            current = str(own).strip()
        elif _is_time_divider(msg):
            current = str(getattr(msg, "content", "") or "").strip() or current
        out.append(current)
    # 回填开头那段：它们在第一条分隔条之前，借用第一个锚点即可。
    # 别留空——留空会让"另一天发的同样内容"算出同一个 local_id，而服务端那列是
    # UNIQUE，插入直接报错、整批上报失败，客户端会无限重试。
    first = next((a for a in out if a), "")
    if first:
        for i, a in enumerate(out):
            if a:
                break
            out[i] = first
    return out


def _msg_key(msg: Any, occurrence: int = 0, stamp: str = "") -> str:
    """消息去重键。

    **刻意不用 wxauto4 的 id/hash**：实测这两个值在聊天窗口重新渲染后会变
    （切到别的会话再切回来，同一条消息就换了 id）。用它做键会导致：
      · 整窗消息被当成新消息重复上报（同一句话重复入库、重复触发方案复核）；
      · 而窗口没重新渲染时又读不到新消息。
    所以改用"内容 + 时间锚点 + 该锚点下同内容出现次序"这种可复现的身份：
    重新渲染算出来还是同一个键；同一个人重复发一样的话（比如两次"好的"）
    次序不同，仍能区分开。stamp 见 _time_anchors——它不含"当前日期"。
    """
    attr = getattr(msg, "attr", "")
    mtype = getattr(msg, "type", "")
    sender = getattr(msg, "sender", "")
    content = getattr(msg, "content", "")
    return "s:" + hashlib.sha256(
        f"{stamp}|{attr}|{mtype}|{sender}|{content}|{occurrence}".encode("utf-8", "ignore")
    ).hexdigest()[:24]


def _occurrences(msgs: list[Any], anchors: list[str] | None = None) -> list[int]:
    """给一次窗口读取里的每条消息编号：同一(时间锚点,发送人,类型,内容)第几次出现（0 起）。

    次序按时间锚点分段计数：这样窗口往上滚出几条旧消息时，后面消息的编号不会整体
    左移（否则键会变，同一条消息又成了"新消息"）。
    """
    seen: dict[tuple, int] = {}
    out: list[int] = []
    for idx, msg in enumerate(msgs):
        ident = (
            anchors[idx] if anchors else "",
            getattr(msg, "attr", ""),
            getattr(msg, "type", ""),
            getattr(msg, "sender", ""),
            getattr(msg, "content", ""),
        )
        n = seen.get(ident, 0)
        seen[ident] = n + 1
        out.append(n)
    return out


# --------------------------------------------------------------------------- #
# 微信 4.1+：免费版 wxauto4（纯轮询）
# --------------------------------------------------------------------------- #
class WxAuto4Source(BaseSource):
    """适配免费版 wxauto4（微信客户端 4.1+，如 4.1.8.107）。

    免费版没有后台监听，用轮询：每次 poll() 挨个 ChatWith(群)+GetAllMessage()，
    按消息 id/hash 比对出新消息；首轮建立基线，不补历史。
    免费版无法下载群文件，文件交给 FolderWatchSource 处理。
    """

    name = "wxauto4"

    def __init__(self, save_pic: bool = False):
        # save_pic 保留仅为兼容；免费版无法下载图片，图片只记录为 [image]
        self.wx: Any = None
        self.chats: list[str] = []
        # chat -> 已见消息 key（用 dict 保持插入顺序，裁剪时才能丢最旧的）
        self._seen: dict[str, dict[str, None]] = {}
        # 启动时基线失败的群：下轮仍按基线模式处理，避免把历史当新消息上报
        self._pending_baseline: set[str] = set()
        self._file_warned = False

    # ---------- 导入（只用免费版 wxauto4） ---------- #
    @staticmethod
    def import_wechat() -> tuple[Any, str]:
        try:
            from wxauto4 import WeChat  # type: ignore
        except ImportError as exc:
            msg = str(exc)
            # 区分"没装"和"装了但被 Windows 拦住"——后者按"没装"去 pip install 会白折腾
            blocked = (
                "DLL load failed" in msg
                or "应用程序控制策略" in msg
                or "application control policy" in msg.lower()
            )
            if blocked:
                raise RuntimeError(
                    "wxauto4 已安装，但它的原生模块被 Windows 拦住了，无法加载。\n"
                    f"原始错误：{exc}\n"
                    "这是系统的应用程序控制策略（Smart App Control / WDAC / AppLocker）"
                    "在阻止从用户可写目录（Documents、Downloads 等）加载 .pyd/.dll。\n"
                    "常见解法（从省事到彻底）：\n"
                    "  1) 先解除文件的\"来自网络\"标记：\n"
                    "     Get-ChildItem -Recurse .venv -Include *.pyd,*.dll | Unblock-File\n"
                    "  2) 把整个项目（含 .venv）移到 C:\\wxclient 这类根目录下重建虚拟环境，\n"
                    "     很多策略只拦 Documents/Downloads 等用户可写路径；\n"
                    "  3) 关掉 Smart App Control：设置 → 隐私和安全性 → Windows 安全中心 →\n"
                    "     应用和浏览器控制 → 智能应用控制 → 关闭（注意：关闭后无法再开启）；\n"
                    "  4) 若是公司管控的机器（WDAC/AppLocker），需要 IT 给该路径加白名单。\n"
                    "查具体是哪条策略拦的：\n"
                    "  Get-WinEvent -LogName Microsoft-Windows-CodeIntegrity/Operational -MaxEvents 20 |\n"
                    "    Where-Object Message -like \"*wxauto4*\" | Format-List TimeCreated, Id, Message"
                ) from exc
            raise RuntimeError(
                "未安装 wxauto4：请执行 `pip install wxauto4`（微信 4.1.8.107 免费版）。"
                "本项目只用免费版，旧的 wxauto 只支持微信 3.9.x。"
                f"原始错误：{exc}"
            ) from exc
        return WeChat, "wxauto4"

    # ---------- 启动 ---------- #
    def start(self, chats: Iterable[str]) -> None:
        WeChat, _ = self.import_wechat()
        self.wx = WeChat()
        log.info("已连接微信客户端（wxauto4 免费版）：%s", self.describe_me())

        self.chats = [c for c in chats if c]
        if not self.chats:
            raise RuntimeError("monitor.chats 为空，请先配置要监听的群名")

        if hasattr(self.wx, "IsOnline"):
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

        log.info(
            "监听方式：轮询（免费版 wxauto4）。文件请配置 monitor.wechat_file_dir 由目录监视捕获。"
        )
        # 报一次"能不能拿到消息时间"：拿不到的话去重只能退化成内容匹配，值得知道
        try:
            probe = self.wx.GetAllMessage() or []
            sample = next((m for m in reversed(probe) if _extract_wx_time(m)), None)
            if sample is not None:
                log.info("消息时间可用（去重将使用它）：示例 %s", _extract_wx_time(sample))
            elif probe:
                log.warning(
                    "拿不到消息发送时间（wxauto4 未提供），去重只能按内容匹配，"
                    "同一人重复发相同内容可能被合并"
                )
        except Exception:  # noqa: BLE001
            pass

        # 建立基线：把每个群当前已有消息标记为已见，避免上报历史。
        # 基线失败的群记下来，下轮 poll 继续按"基线模式"重试，绝不把历史当新消息上报。
        for chat in self.chats:
            if self._poll_chat(chat, baseline=True) is None:
                self._pending_baseline.add(chat)
                log.warning("群「%s」基线未建立（打开会话失败），下轮重试；期间不会上报其历史", chat)

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

    # ---------- 轮询 ---------- #
    def _poll_chat(self, chat: str, baseline: bool = False) -> list[WxMessage] | None:
        """打开某个群，取全部消息，返回未见过的新消息。baseline=True 时只记录不返回。

        返回 None 表示这次没能读到会话（打开失败/取消息异常），调用方据此判断
        基线是否建立成功——**不能**把失败当成"空列表"，否则历史会在下轮被全量上报。
        """
        try:
            res = self.wx.ChatWith(chat)
            if _is_failure(res):
                log.warning("ChatWith(%s) 失败：%s", chat, _failure_text(res))
                return None
        except Exception as exc:  # noqa: BLE001
            log.warning("ChatWith(%s) 异常：%s", chat, exc)
            return None

        try:
            msgs = self.wx.GetAllMessage() or []
        except Exception as exc:  # noqa: BLE001
            log.warning("GetAllMessage(%s) 异常：%s", chat, exc)
            return None

        seen = self._seen.setdefault(chat, {})
        out: list[WxMessage] = []
        # 整窗都没有时间信息时才退回"今天几号"：那种情况下宁可零点重报一次，
        # 也不能让不同天的同样内容算出同一个 local_id（服务端 UNIQUE，会插入失败）。
        anchors = [a or time.strftime("%Y-%m-%d") for a in _time_anchors(msgs)]
        occs = _occurrences(msgs, anchors)
        for msg, occ, stamp in zip(msgs, occs, anchors):
            key = _msg_key(msg, occ, stamp)
            if key in seen:
                continue
            seen[key] = None
            if baseline:
                continue
            m = self._normalize(chat, msg, occurrence=occ, stamp=stamp)
            if m:
                m.dedup_key = key
                out.append(m)

        if len(seen) > 4000:  # 控制长会话的内存：按插入顺序丢最旧的一半
            # 注意：不能用 set 切片（set 无序，会随机丢掉"还在窗口里"的键，
            # 那些消息下轮就被当成新消息重复上报）。dict 保证插入顺序。
            self._seen[chat] = dict.fromkeys(list(seen.keys())[-2000:])
        if baseline:
            log.info("群「%s」建立基线：已有 %d 条历史消息不再上报", chat, len(seen))
        return out

    # ---------- 归一化 ---------- #
    def _normalize(
        self, chat: str, msg: Any, occurrence: int = 0, stamp: str = ""
    ) -> WxMessage | None:
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

        wx_time = _extract_wx_time(msg)

        # 免费版不能下载文件；文件消息只记录，实际文件走 FolderWatchSource
        if mtype == "file" and not self._file_warned:
            self._file_warned = True
            log.warning(
                "收到文件消息，免费版 wxauto4 无法下载文件。"
                "方案文件请靠 monitor.wechat_file_dir 目录监视捕获（微信里开自动下载）。"
            )
        if not content and mtype != "text":
            content = f"[{mtype}]"

        m = WxMessage(
            chat=chat,
            sender=sender,
            msg_type=mtype or "text",
            content=content.strip(),
            wx_time=wx_time,
            file_path=None,
            raw_type=attr or mtype,
        )
        # 不用 wxauto4 的 id/hash：窗口重渲染后会变（详见 _msg_key 的说明）
        m.local_id = m.compute_local_id(occurrence=occurrence, stamp=stamp)
        if not m.content:
            return None
        return m

    def poll(self) -> list[WxMessage]:
        out: list[WxMessage] = []
        for chat in self.chats:
            if chat in self._pending_baseline:
                # 上次基线没建成：这轮只补基线，不上报，避免整段历史被当成新消息
                if self._poll_chat(chat, baseline=True) is not None:
                    self._pending_baseline.discard(chat)
                    log.info("群「%s」基线已补建完成，开始正常上报新消息", chat)
                continue
            got = self._poll_chat(chat, baseline=False)
            if got:
                out.extend(got)
        return out

    def forget(self, chat: str, dedup_key: str) -> None:
        if dedup_key:
            self._seen.get(chat, {}).pop(dedup_key, None)

    def snapshot(self, chat: str) -> list[WxMessage] | None:
        """只读一次该会话的当前消息，不写入 _seen（不影响正常轮询）。"""
        if not chat:
            return None
        try:
            res = self.wx.ChatWith(chat)
            if _is_failure(res):
                log.warning("snapshot ChatWith(%s) 失败：%s", chat, _failure_text(res))
                return None
        except Exception as exc:  # noqa: BLE001
            log.warning("snapshot ChatWith(%s) 异常：%s", chat, exc)
            return None
        try:
            msgs = self.wx.GetAllMessage() or []
        except Exception as exc:  # noqa: BLE001
            log.warning("snapshot GetAllMessage(%s) 异常：%s", chat, exc)
            return None
        out: list[WxMessage] = []
        anchors = [a or time.strftime("%Y-%m-%d") for a in _time_anchors(msgs)]
        for msg, occ, stamp in zip(msgs, _occurrences(msgs, anchors), anchors):
            m = self._normalize(chat, msg, occurrence=occ, stamp=stamp)
            if m:
                # snapshot 只读不入队，不写 _seen；dedup_key 仅供调用方参考
                m.dedup_key = _msg_key(msg, occ, stamp)
                out.append(m)
        return out

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
            "package": "wxauto4",
            "mode": "poll",
            "chats": self.chats,
            "wechat_online": alive,
        }


# --------------------------------------------------------------------------- #
# 目录监视：捕获微信自动下载的方案文件（免费版下载文件的替代方案）
# --------------------------------------------------------------------------- #
class FolderWatchSource(BaseSource):
    """盯一个目录，新出现的方案文件（docx/xlsx/pdf/...）当作一条文件消息上报。

    用于免费版 wxauto4 无法下载文件的情况：让微信自动下载群文件到某目录
    （微信设置里开自动下载，或用户手动下载后落到默认目录），本源负责捕获。
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
        self._seen: set[str] = set()          # 已上报文件的 key(size+path)
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
            first = self._pending.setdefault(key, now)  # 等文件大小稳定
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

    def forget(self, chat: str, dedup_key: str) -> None:
        for s in self.sources:
            try:
                s.forget(chat, dedup_key)
            except Exception:  # noqa: BLE001
                pass

    def snapshot(self, chat: str) -> list[WxMessage] | None:
        for s in self.sources:
            try:
                got = s.snapshot(chat)
            except Exception:
                log.exception("子源 %s snapshot 出错", s.name)
                continue
            if got is not None:
                return got
        return None

    def health(self) -> dict[str, Any]:
        return {"source": self.name, "children": [s.health() for s in self.sources]}

    def stop(self) -> None:
        for s in self.sources:
            s.stop()


# --------------------------------------------------------------------------- #
# 微信 3.9.x：老版 wxauto（免费，轮询式）
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
        wx_time = _extract_wx_time(msg)
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
    file_dir = cfg.monitor.get("wechat_file_dir")

    if backend == "wxauto":
        return WxAutoLegacySource(save_pic=save_pic)

    if backend in ("wxauto4", "auto"):
        if backend == "auto":
            # 没装 wxauto4 但装了老 wxauto(3.9) 时回退
            try:
                WxAuto4Source.import_wechat()
            except RuntimeError as exc:
                try:
                    import wxauto  # type: ignore  # noqa: F401
                except ImportError:
                    raise exc
                log.warning("未找到 wxauto4，回退到老版 wxauto（仅微信 3.9.x）")
                return WxAutoLegacySource(save_pic=save_pic)

        wx = WxAuto4Source(save_pic=save_pic)
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
