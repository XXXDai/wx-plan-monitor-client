"""上班打卡检测（客户端侧）。

工作日到点（默认 07:59）**只读一次**打卡群，就地判断我今天有没有发过"打卡"，
然后只把结论（true/false）上报服务端。

刻意这样设计：
  * 打卡群的聊天内容不入队、不上报、不入库 —— 和网页上的消息/告警功能完全分开；
  * 一天只读一次会话，不做持续轮询（读会话会切换微信当前窗口，能少切就少切）；
  * 判断在本地做，服务端只负责"没打卡就响铃"。
"""

from __future__ import annotations

import logging
from datetime import date, datetime

log = logging.getLogger("checkin")


def _hhmm(s: str) -> tuple[int, int]:
    hh, _, mm = str(s or "07:59").partition(":")
    try:
        return int(hh), int(mm or 0)
    except ValueError:
        return 7, 59


class CheckinTask:
    """按天调度的一次性打卡检测。由采集循环每轮调用 maybe_run()，到点才真正执行。"""

    def __init__(self, cfg, source, uploader) -> None:
        self.cfg = cfg
        self.source = source
        self.up = uploader
        self._done_on: date | None = None   # 已经完成检测的日期
        c = cfg.checkin or {}
        self.enabled = bool(c.get("enabled", True))
        self.chat = str(c.get("chat") or "").strip()
        self.keywords = [str(k) for k in (c.get("keywords") or ["打卡"]) if str(k).strip()]
        self.sender = str(c.get("sender") or "").strip()
        self.at_h, self.at_m = _hhmm(c.get("at_time", "07:59"))
        self.weekdays_only = bool(c.get("weekdays_only", True))
        # 只在检测时间点之后的这个窗口内才检测；过了窗口就算今天错过了。
        # 没有窗口的话，任何时间启动/运行都会立刻去读打卡群（只要今天还没读过）。
        self.window_minutes = max(1, int(c.get("window_minutes", 30)))
        # 启动瞬间不读打卡群：切会话会打断正在监听的群，而且这时读到的
        # 也只是"当前时刻有没有打卡"，跟 07:59 那个约定的检查点没关系。
        now = datetime.now()
        if self._past_check_time(now):
            self._done_on = now.date()
            if self.enabled and self.chat:
                log.info(
                    "启动时已过 %02d:%02d，今天不再检测打卡（明天到点自动检测）",
                    self.at_h, self.at_m,
                )
        if self.enabled and self.chat:
            log.info(
                "打卡检测已启用：工作日 %02d:%02d 读一次「%s」，只看%s发的含%s的消息（内容不上报）",
                self.at_h, self.at_m, self.chat,
                f"「{self.sender}」" if self.sender else "任何人",
                "/".join(self.keywords),
            )

    # ---------------- 调度 ---------------- #
    def _past_check_time(self, now: datetime) -> bool:
        return (now.hour, now.minute) >= (self.at_h, self.at_m)

    def _minutes_since_check_time(self, now: datetime) -> int:
        return (now.hour * 60 + now.minute) - (self.at_h * 60 + self.at_m)

    def _due(self, now: datetime) -> bool:
        if not (self.enabled and self.chat):
            return False
        if self.weekdays_only and now.weekday() >= 5:
            return False
        if self._done_on == now.date():
            return False
        # 必须落在 [检测时间, 检测时间+窗口) 内：过了窗口就不再补做，
        # 免得中午重启时莫名去读一次打卡群。
        delta = self._minutes_since_check_time(now)
        return 0 <= delta < self.window_minutes

    def maybe_run(self, now: datetime | None = None) -> bool:
        """到点就跑一次；返回是否执行了检测。"""
        now = now or datetime.now()
        if not self._due(now):
            return False
        # 先占住今天，避免异常时本轮反复重试
        self._done_on = now.date()
        try:
            res = self.run_once(now)
        except Exception:
            log.exception("打卡检测异常")
        else:
            # 只是这一刻读不到会话（微信窗口正忙、切会话失败）：把今天放回去，
            # 窗口内下一轮再试一次；别因为一次读失败就整天不检测了。
            # 窗口过了 _due() 自然不再触发，不会变成无限重试。
            if isinstance(res, dict) and res.get("skipped"):
                self._done_on = None
        return True

    # ---------------- 检测 ---------------- #
    def detect(self, now: datetime | None = None) -> tuple[bool | None, str]:
        """读一次打卡群，判断今天有没有打卡。返回 (结论, 证据)；结论 None = 没读到会话。"""
        now = now or datetime.now()
        msgs = self.source.snapshot(self.chat)
        if msgs is None:
            return None, ""
        today = now.strftime("%Y-%m-%d")
        hit = ""
        for m in msgs:
            if self.sender and (m.sender or "").strip() != self.sender:
                continue
            content = m.content or ""
            if not any(k in content for k in self.keywords):
                continue
            # 微信给了时间就按当天过滤；没给就只能认"当前可见窗口里的"（打卡群消息量小，够用）
            if m.wx_time and today not in str(m.wx_time):
                continue
            hit = content.strip()[:80]
        return bool(hit), hit

    def run_once(self, now: datetime | None = None) -> dict:
        now = now or datetime.now()
        ok, evidence = self.detect(now)
        if ok is None:
            log.warning("打卡检测：打不开「%s」，本次跳过（不误报）", self.chat)
            return {"skipped": "会话打不开"}
        log.info("打卡检测：%s%s", "今天已打卡" if ok else "今天还没打卡",
                 f"（{evidence}）" if evidence else "")
        try:
            res = self.up.report_checkin(ok, evidence=evidence, chat=self.chat)
            log.info("打卡结论已上报：checked_in=%s -> %s", ok, res)
            return res
        except Exception as exc:  # noqa: BLE001
            log.warning("打卡结论上报失败：%s", exc)
            return {"error": str(exc)}
