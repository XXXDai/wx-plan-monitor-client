#!/usr/bin/env python
"""链路自检：一条命令确认「微信 → 客户端 → 服务端 → 报警」每一段是否通。

    python tools/test_link.py                 # 全部检查（微信部分在非 Windows 上自动跳过）
    python tools/test_link.py --alert         # 顺便真推一条报警到手机，验证 Bark/Webhook/脚本
    python tools/test_link.py --skip-wx       # 只测服务端（在办公电脑上排查网络时用）
    python tools/test_link.py --file 方案.docx # 顺便上传一份方案，验证解析链路
    python tools/test_link.py --config D:\\conf\\client.yaml

退出码：0 = 全通过，1 = 有失败项。
"""

from __future__ import annotations

import argparse
import platform
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wxclient.config import load_config  # noqa: E402
from wxclient.uploader import Uploader, UploadError  # noqa: E402

OK, FAIL, SKIP = "✅", "❌", "⏭️"
_results: list[tuple[str, bool]] = []


def step(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 56 - len(title)))


def ok(msg: str) -> None:
    _results.append((msg, True))
    print(f"  {OK} {msg}")


def bad(msg: str, hint: str = "") -> None:
    _results.append((msg, False))
    print(f"  {FAIL} {msg}")
    if hint:
        for line in hint.strip().splitlines():
            print(f"       → {line.strip()}")


def skip(msg: str) -> None:
    print(f"  {SKIP} {msg}")


def info(msg: str) -> None:
    print(f"     {msg}")


# --------------------------------------------------------------------------- #
# 1. 配置
# --------------------------------------------------------------------------- #
def check_config(cfg) -> bool:
    step("1/5 配置")
    if cfg.path:
        ok(f"配置文件：{cfg.path}")
    else:
        bad(
            "没找到 config.yaml，用的是内置默认值",
            "cp config.example.yaml config.yaml 后填写 server.base_url / client_id / secret",
        )
        return False

    good = True
    base_url = cfg.server.get("base_url") or ""
    if base_url and "your-server" not in base_url:
        ok(f"服务端地址：{base_url}")
        if base_url.startswith("http://") and not base_url.startswith("http://127."):
            info("提示：走公网建议上 HTTPS，否则上报内容是明文")
    else:
        bad("server.base_url 未填写", "改成你的服务端地址，如 https://monitor.example.com")
        good = False

    if cfg.server.get("secret"):
        ok(f"上报凭证：client_id={cfg.server.get('client_id')}，secret 已配置")
    else:
        bad(
            "server.secret 为空",
            "填成与服务端 server.clients 里一致的值，或设环境变量 BARK_CLIENT_SECRET",
        )
        good = False

    chats = cfg.monitor.get("chats") or []
    if chats:
        ok(f"监听群：{'、'.join(chats)}")
    else:
        bad("monitor.chats 为空", "填上要监听的群名，必须与微信里显示的完全一致")
        good = False
    return good


# --------------------------------------------------------------------------- #
# 2. 微信
# --------------------------------------------------------------------------- #
def check_wechat(cfg, skip_wx: bool) -> None:
    step("2/5 微信客户端")
    if skip_wx:
        skip("按 --skip-wx 跳过")
        return
    if platform.system() != "Windows":
        skip(f"当前系统是 {platform.system()}，wxauto 只能在 Windows 上运行")
        return

    backend = str(cfg.monitor.get("backend", "auto")).lower()
    if backend == "wxauto":
        try:
            from wxauto import WeChat  # type: ignore

            pkg = "wxauto"
        except ImportError as exc:
            bad(f"导入 wxauto 失败：{exc}", "pip install wxauto（仅支持微信 3.9.x）")
            return
    else:
        from wxclient.wx_adapter import WxAuto4Source

        try:
            WeChat, pkg = WxAuto4Source.import_wechat()
        except RuntimeError as exc:
            bad(
                str(exc).splitlines()[0],
                "微信 4.1.8.107 请执行：pip install wxauto4"
                "（Plus 版 wxautox4 支持到 4.1.9.35）",
            )
            return
    ok(f"已安装 {pkg}")

    try:
        wx = WeChat()
    except Exception as exc:  # noqa: BLE001
        bad(
            f"连接微信失败：{exc}",
            "确认微信桌面版已登录、窗口没有最小化到托盘、没有锁屏；"
            "另外 wxauto 需要以和微信相同的用户身份运行",
        )
        return
    ok("已连接微信客户端")

    fn = getattr(wx, "GetMyInfo", None)
    if callable(fn):
        try:
            me = fn()
            nickname = me.get("nickname") if isinstance(me, dict) else me
            ok(f"当前登录账号：{nickname}")
        except Exception as exc:  # noqa: BLE001
            info(f"GetMyInfo 调用失败（不影响监听）：{exc}")

    fn = getattr(wx, "IsOnline", None)
    if callable(fn):
        try:
            if fn():
                ok("微信在线")
            else:
                bad("微信显示未登录", "重新登录微信后再试")
        except Exception as exc:  # noqa: BLE001
            info(f"IsOnline 调用失败：{exc}")

    # 会话列表里能不能找到要监听的群
    names: list[str] = []
    fn = getattr(wx, "GetSession", None) or getattr(wx, "GetSessionList", None)
    if callable(fn):
        try:
            sessions = fn() or []
            if isinstance(sessions, dict):  # 老版返回 {name: msgcount}
                names = [str(k) for k in sessions]
            else:
                for s in sessions:
                    name = getattr(s, "name", None) or getattr(s, "nickname", None)
                    if not name and isinstance(s, dict):
                        name = s.get("name") or s.get("nickname")
                    if not name and isinstance(s, str):
                        name = s
                    if name:
                        names.append(str(name))
        except Exception as exc:  # noqa: BLE001
            info(f"获取会话列表失败：{exc}")

    if not names:
        info("拿不到会话列表，跳过群名核对")
        return
    info(f"当前可见会话 {len(names)} 个：{'、'.join(names[:10])}{'…' if len(names) > 10 else ''}")
    for chat in cfg.monitor.get("chats") or []:
        if chat in names:
            ok(f"群「{chat}」在会话列表中")
        else:
            near = [n for n in names if chat.strip() in n or n in chat.strip()]
            bad(
                f"会话列表里没有群「{chat}」",
                (f"是不是这个：{'、'.join(near)}？" if near else "")
                + " 群名要和微信里显示的完全一致（含空格/emoji）；先在微信里打开一次该群",
            )


# --------------------------------------------------------------------------- #
# 3~5. 服务端
# --------------------------------------------------------------------------- #
def check_server(cfg, do_alert: bool, file_path: str | None) -> None:
    step("3/5 服务端连通性")
    try:
        up = Uploader(cfg)
    except RuntimeError as exc:
        bad(str(exc))
        return

    try:
        health = up.ping()
        ok(f"/healthz 可达：{health}")
    except UploadError as exc:
        bad(
            str(exc),
            "检查服务端是否在跑（systemctl status hedge-monitor）、"
            "base_url 端口是否正确、云服务器安全组/防火墙是否放行",
        )
        return

    step("4/5 签名鉴权与服务端状态")
    try:
        res = up.heartbeat({"host": platform.node(), "probe": "test_link"})
        ok(f"心跳 + 签名校验通过：{res}")
    except UploadError as exc:
        bad(
            str(exc),
            "401 且提示签名校验失败 → client_id/secret 与服务端 server.clients 不一致；"
            "401 且提示时间戳 → 本机时钟不准，开启自动同步时间",
        )
        return

    try:
        st = up._post_json(  # noqa: SLF001 - 自检脚本直接用底层签名请求
            "/api/v1/selftest",
            {"alert": bool(do_alert), "note": f"{platform.node()} 链路自检"},
        )
    except UploadError as exc:
        bad(
            f"自检接口调用失败：{exc}",
            "服务端版本较旧（没有 /api/v1/selftest）时请更新服务端代码",
        )
        return

    ok(f"自检接口通过，服务端时间偏差 {st.get('clock_skew_sec')} 秒")
    if abs(float(st.get("clock_skew_sec") or 0)) > 60:
        bad("两端时钟偏差超过 60 秒", "签名有 ±300 秒窗口，偏差继续变大会导致 401，请校准时间")

    prices = st.get("prices") or {}
    if st.get("price_fresh"):
        ok(f"价格监控正常：{prices}（{st.get('price_age_sec')} 秒前更新）")
    else:
        bad(
            f"价格数据不新鲜：{prices}，最后更新 {st.get('price_age_sec')} 秒前",
            "服务端可能连不上 OKX（大陆机器试试 okx.base_url: https://aws.okx.com 或配置代理）",
        )

    if st.get("deepseek_configured"):
        ok("DeepSeek 已配置（方案文件能被解析）")
    else:
        bad(
            "服务端没有配置 DEEPSEEK_API_KEY",
            "没有它文件不会被解析，也就不会生成任何监控点位（没有兜底解析）",
        )

    enabled = [c for c in (st.get("alert_channels") or []) if c.get("enabled")]
    if any(c["type"] != "log" for c in enabled):
        ok(f"报警渠道：{[c.get('name') or c['type'] for c in enabled]}")
    else:
        bad(
            f"只有 log 渠道，报警不会推到手机：{enabled}",
            "在服务端 config.yaml 的 alerts.channels 里启用 bark / webhook / command",
        )

    info(f"当前监控点位 {st.get('active_tasks')} 个；特定人报警名单：{st.get('watch_senders')}")

    if do_alert:
        delivery = st.get("alert_delivery") or []
        if delivery and all("失败" not in str(d) for d in delivery):
            ok(f"报警已投递：{delivery}（看看手机是否收到）")
        else:
            bad(f"报警投递失败：{delivery}", "检查 Bark key / webhook 地址 / 脚本路径")

    step("5/5 消息与文件上报")
    local_id = f"linktest-{int(time.time())}"
    try:
        res = up.send_messages(
            [
                {
                    "local_id": local_id,
                    "chat": (cfg.monitor.get("chats") or ["链路自检"])[0],
                    "sender": "链路自检",
                    "msg_type": "text",
                    "content": f"[链路自检] {platform.node()} {time.strftime('%F %T')}",
                    "wx_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
            ]
        )
        if res.get("accepted") == 1:
            ok(f"测试消息已入库（服务端 alerts={res.get('alerts')}）")
        else:
            bad(f"消息未被接收：{res}")
    except UploadError as exc:
        bad(f"消息上报失败：{exc}")

    if file_path:
        p = Path(file_path)
        if not p.is_file():
            bad(f"文件不存在：{p}")
            return
        try:
            res = up.send_file(
                p,
                {
                    "local_id": f"{local_id}-file",
                    "chat": (cfg.monitor.get("chats") or ["链路自检"])[0],
                    "sender": "链路自检",
                    "msg_type": "file",
                    "content": f"[文件] {p.name}",
                    "wx_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                },
            )
            ok(f"文件已上报：{res}")
            info("解析在服务端后台进行，稍后看 /api/v1/events 或等「✅ 方案解析完成」报警")
        except (UploadError, FileNotFoundError) as exc:
            bad(f"文件上报失败：{exc}")
    else:
        skip("未指定 --file，跳过文件上报（加 --file 方案.docx 可顺带验证解析链路）")


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="微信监控客户端链路自检")
    ap.add_argument("--config", help="配置文件路径（默认 client/config.yaml）")
    ap.add_argument("--skip-wx", action="store_true", help="跳过微信部分，只测服务端")
    ap.add_argument("--alert", action="store_true", help="真的推一条报警，验证 Bark/Webhook/脚本")
    ap.add_argument("--file", help="顺便上传一个方案文件，验证解析链路")
    args = ap.parse_args(argv)

    print("=" * 64)
    print("  微信群套保监控 · 链路自检")
    print("=" * 64)

    cfg = load_config(args.config)
    if check_config(cfg):
        check_wechat(cfg, args.skip_wx)
        check_server(cfg, args.alert, args.file)
    else:
        print("\n配置不完整，后续检查已跳过。")

    failed = [m for m, good in _results if not good]
    print("\n" + "=" * 64)
    print(f"  通过 {len(_results) - len(failed)} 项，失败 {len(failed)} 项")
    for m in failed:
        print(f"  {FAIL} {m}")
    print("=" * 64)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
