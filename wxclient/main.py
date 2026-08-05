"""客户端主程序：监听微信群 -> 本地队列 -> 上报服务端。

用法：
    python -m wxclient.main                     # 正常运行（Windows + 已登录微信）
    python -m wxclient.main --mock-dir mock_in  # 模拟模式，任何系统都能跑通链路
    python -m wxclient.main --send-file 方案.docx --chat 套保群 --sender 张总
    python -m wxclient.main --ping              # 只测试与服务端的连通性和签名
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import platform
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .config import Config, load_config
from .outbox import Outbox
from .checkin import CheckinTask
from .uploader import PermanentUploadError, Uploader, UploadError
from .wx_adapter import IMAGE_SUFFIXES, BaseSource, WxMessage, build_source

log = logging.getLogger("client")
_stop = threading.Event()


def setup_logging(cfg: Config) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file := cfg.runtime.get("log_file"):
        p = cfg.abs_path(log_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                p, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
        )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=handlers,
        force=True,
    )


# --------------------------------------------------------------------------- #
# 采集
# --------------------------------------------------------------------------- #
class Collector:
    def __init__(self, cfg: Config, source: BaseSource, box: Outbox,
                 checkin: CheckinTask | None = None):
        self.cfg = cfg
        self.source = source
        self.box = box
        self.checkin = checkin
        self.suffixes = {
            s.lower() for s in (cfg.monitor.get("upload_suffixes") or []) if s
        }
        self.senders_only = [s for s in (cfg.monitor.get("senders_only") or []) if s]
        self.stats = {"messages": 0, "files": 0, "skipped": 0}

    def _should_upload_file(self, path: str) -> bool:
        suffix = Path(path).suffix.lower()
        if suffix in IMAGE_SUFFIXES:
            return False  # 图片不做解析，只当普通消息记录
        if not ((not self.suffixes) or suffix in self.suffixes):
            return False
        # 过大的文件根本不入队：网关会 413 打回，入队只会反复重试刷日志
        limit = float(self.cfg.server.get("max_upload_mb", 8))
        if limit > 0:
            try:
                size_mb = Path(path).stat().st_size / (1024 * 1024)
            except OSError:
                return True  # 拿不到大小就照常尝试，由上报环节兜底
            if size_mb > limit:
                log.warning(
                    "文件过大不上报：%s（%.1fMB > 上限 %.0fMB）",
                    Path(path).name, size_mb, limit,
                )
                return False
        return True

    def _sender_for_upload(self, m: WxMessage) -> str:
        """自消息用稳定的业务名称上报，避免服务端只看到“我”而无法识别角色。"""
        if m.raw_type == "self":
            configured = str(self.cfg.monitor.get("self_sender") or "").strip()
            if configured:
                return configured
        return m.sender

    def handle(self, m: WxMessage) -> None:
        if self.box.is_seen(m.local_id):
            return
        if self.cfg.monitor.get("ignore_self", True) and (
            m.raw_type == "self" or m.sender in ("我", "self", "Self")
        ):
            self.box.mark_seen(m.local_id)
            return

        sender = self._sender_for_upload(m)

        payload = {
            "local_id": m.local_id,
            "chat": m.chat,
            "sender": sender,
            "msg_type": m.msg_type,
            "content": m.content,
            "wx_time": m.wx_time,
        }

        if m.file_path and self._should_upload_file(m.file_path):
            self.box.put("file", payload, file_path=m.file_path)
            self.stats["files"] += 1
            log.info("入队【文件】%s / %s：%s", m.chat, m.sender, Path(m.file_path).name)
        elif self.cfg.monitor.get("send_text", True) and (
            not self.senders_only or sender in self.senders_only
        ):
            self.box.put("message", payload)
            self.stats["messages"] += 1
            log.info(
                "入队【消息】%s / %s：%s", m.chat, sender, (m.content or "")[:60].replace("\n", " ")
            )
        else:
            self.stats["skipped"] += 1

        self.box.mark_seen(m.local_id)

    def run(self) -> None:
        interval = float(self.cfg.monitor.get("poll_interval", 2))
        errors = 0
        while not _stop.is_set():
            try:
                # 逐条隔离：一条处理失败不能拖垮整批。
                # 来源侧是"读到就标记已见"，所以失败必须回滚该条的已见标记，
                # 否则这条消息永久丢失（曾因此丢过一条"好的"，导致命令确认没记上）。
                for m in self.source.poll():
                    try:
                        self.handle(m)
                    except Exception:
                        log.exception(
                            "处理消息失败，已退回下轮重试：%s / %s：%s",
                            m.chat, m.sender, (m.content or "")[:40].replace("\n", " "),
                        )
                        try:
                            self.source.forget(m.chat, m.dedup_key)
                        except Exception:  # noqa: BLE001
                            log.debug("回滚已见标记失败（该条将丢失）")
                # 到点做一次打卡检测（一天一次，读完就判、内容不入队）
                if self.checkin is not None:
                    self.checkin.maybe_run()
                errors = 0
            except Exception:
                errors += 1
                log.exception("采集异常（连续 %d 次）", errors)
                if errors >= 10:
                    log.error("采集连续失败，等待 60 秒后继续（请检查微信是否被关闭/退出登录）")
                    _stop.wait(60)
                    errors = 0
            _stop.wait(interval)


# --------------------------------------------------------------------------- #
# 上报
# --------------------------------------------------------------------------- #
class Sender:
    def __init__(self, cfg: Config, box: Outbox, uploader: Uploader):
        self.cfg = cfg
        self.box = box
        self.up = uploader
        self.batch = int(cfg.runtime.get("batch_size", 20))
        self.interval = float(cfg.runtime.get("send_interval", 3))
        self.max_attempts = int(cfg.runtime.get("max_attempts", 0))

    def _flush_messages(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        payloads = [r["payload"] for r in rows]
        try:
            res = self.up.send_messages(payloads)
        except PermanentUploadError as exc:
            for r in rows:
                self.box.done(r["id"])
            log.warning("放弃上报这 %d 条消息（重试无用）：%s", len(rows), exc)
            return
        except UploadError as exc:
            for r in rows:
                self.box.fail(r["id"], str(exc), self.max_attempts)
            log.warning("消息上报失败（%d 条稍后重试）：%s", len(rows), exc)
            return
        for r in rows:
            self.box.done(r["id"])
        log.info(
            "已上报 %d 条消息（服务端 accepted=%s duplicated=%s alerts=%s）",
            len(rows),
            res.get("accepted"),
            res.get("duplicated"),
            res.get("alerts"),
        )

    def _flush_file(self, row: dict[str, Any]) -> None:
        meta = {**row["payload"], "filename": Path(row["file_path"] or "").name}
        try:
            res = self.up.send_file(row["file_path"], meta)
        except FileNotFoundError as exc:
            log.error("文件已不存在，放弃上报：%s", exc)
            self.box.done(row["id"])
            return
        except PermanentUploadError as exc:
            # 文件过大/签名格式错等：重试也一样，直接出队，避免反复上报刷日志
            log.warning("放弃上报该文件（重试无用）：%s", exc)
            self.box.done(row["id"])
            return
        except UploadError as exc:
            self.box.fail(row["id"], str(exc), self.max_attempts)
            log.warning("文件上报失败（稍后重试）：%s", exc)
            return
        self.box.done(row["id"])
        log.info(
            "已上报文件 %s（file_id=%s duplicated=%s）",
            meta["filename"],
            res.get("file_id"),
            res.get("duplicated"),
        )

    def flush_once(self) -> int:
        rows = self.box.take(self.batch)
        if not rows:
            return 0
        msg_rows = [r for r in rows if r["kind"] == "message"]
        self._flush_messages(msg_rows)
        for r in rows:
            if r["kind"] == "file":
                self._flush_file(r)
        return len(rows)

    def run(self) -> None:
        while not _stop.is_set():
            try:
                self.flush_once()
            except Exception:
                log.exception("上报线程异常")
            _stop.wait(self.interval)


def heartbeat_loop(cfg: Config, box: Outbox, uploader: Uploader, source: BaseSource) -> None:
    interval = float(cfg.runtime.get("heartbeat_interval", 120))
    if interval <= 0:
        return
    while not _stop.is_set():
        try:
            uploader.heartbeat(
                {
                    "host": platform.node(),
                    "platform": platform.platform(),
                    "pending": box.pending(),
                    "chats": cfg.monitor.get("chats"),
                    "source": source.health(),
                    "ts": time.time(),
                }
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("心跳失败：%s", exc)
        box.prune_seen()
        _stop.wait(interval)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="微信群套保方案监控客户端")
    p.add_argument("--config", help="配置文件路径（默认 client/config.yaml）")
    p.add_argument("--mock-dir", help="模拟模式：从该目录读取 json 消息，不连接微信")
    p.add_argument("--ping", action="store_true", help="只测试服务端连通性与签名")
    p.add_argument("--send-file", help="手工上报一个文件后退出（调试用）")
    p.add_argument("--chat", default="手工上报", help="配合 --send-file 使用")
    p.add_argument("--sender", default="手工", help="配合 --send-file 使用")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = load_config(args.config)
    if args.mock_dir:
        cfg.runtime["mock_dir"] = args.mock_dir
    setup_logging(cfg)

    log.info("配置文件：%s", cfg.path or "(未找到，使用默认值)")
    uploader = Uploader(cfg)

    if args.ping:
        health = uploader.ping()
        log.info("服务端 /healthz：%s", health)
        res = uploader.heartbeat({"host": platform.node(), "probe": True})
        log.info("心跳（含签名校验）成功：%s", res)
        return 0

    box = Outbox(cfg.abs_path(cfg.runtime["db_path"]))

    if args.send_file:
        path = Path(args.send_file)
        meta = {
            "local_id": f"manual-{int(time.time())}",
            "chat": args.chat,
            "sender": args.sender,
            "msg_type": "file",
            "content": f"[文件] {path.name}",
            "wx_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        res = uploader.send_file(path, meta)
        log.info("上报结果：%s", res)
        return 0

    if platform.system() != "Windows" and not cfg.runtime.get("mock_dir"):
        log.error(
            "wxauto/wxauto4 只能在 Windows 上运行（微信 4.1.8.107 请用 pip install wxauto4）。"
            "本机调试请加 --mock-dir mock_in 走模拟模式。"
        )
        return 2

    source = build_source(cfg)
    source.start(cfg.monitor.get("chats") or [])

    checkin_task = CheckinTask(cfg, source, uploader) if (cfg.checkin or {}).get("enabled") else None
    collector = Collector(cfg, source, box, checkin=checkin_task)
    sender = Sender(cfg, box, uploader)

    def _on_signal(signum, _frame):
        log.info("收到信号 %s，正在退出…", signum)
        _stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, AttributeError):  # pragma: no cover
            pass

    threads = [
        threading.Thread(target=sender.run, name="sender", daemon=True),
        threading.Thread(
            target=heartbeat_loop, args=(cfg, box, uploader, source), name="heartbeat", daemon=True
        ),
    ]
    for t in threads:
        t.start()

    log.info("开始监听：%s", cfg.monitor.get("chats"))
    try:
        collector.run()
    except KeyboardInterrupt:
        _stop.set()

    log.info("正在把队列里剩余 %d 条发完…", box.pending())
    try:
        for _ in range(3):
            if sender.flush_once() == 0:
                break
    except Exception:
        log.exception("退出前清队列失败，数据仍保留在本地 outbox.db 中")
    source.stop()
    box.close()
    log.info("已退出。采集统计：%s", collector.stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
