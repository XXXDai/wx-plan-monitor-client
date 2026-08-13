#!/usr/bin/env python
"""打卡检测排查：读一次打卡群，把判定过程原地打出来。

    python tools/checkin_probe.py

**只在本机打印，不上报、不入库、不发报警**——打卡群的内容一个字都不会离开这台电脑。
需要在装了微信的 Windows 上跑（和客户端同一个环境）。

用来回答"我明明发了打卡，为什么判成没打卡"：能看到窗口里每条消息的发送人、
wxauto4 给的类型、以及它有没有过"是我发的"和"含关键词"这两道筛子。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wxclient.checkin import CheckinTask  # noqa: E402
from wxclient.config import load_config  # noqa: E402
from wxclient.wx_adapter import build_source  # noqa: E402

cfg = load_config()
c = cfg.checkin or {}
chat = str(c.get("chat") or "").strip()
print(f"打卡群：「{chat}」  关键词：{c.get('keywords')}  只认：{c.get('sender') or '任何人'}")
print(f"检测时间：{c.get('at_time')}（窗口 {c.get('window_minutes')} 分钟）\n")

source = build_source(cfg)
source.start([])  # 不建立任何群的轮询基线，只为拿到微信连接

msgs = source.snapshot(chat)
if msgs is None:
    print("❌ 打不开这个会话。群名必须和微信里显示的完全一致，先在微信里手动打开一次这个群。")
    sys.exit(1)

task = CheckinTask(cfg, source, uploader=None)
keywords = task.keywords
print(f"窗口里读到 {len(msgs)} 条消息：\n")
print(f"{'发送人':<12} {'wxauto类型':<10} {'raw':<8} {'是我发的':<8} {'含关键词':<8} 内容")
print("-" * 88)
for m in msgs:
    print(
        f"{(m.sender or '(空)'):<12} {m.msg_type:<10} {m.raw_type:<8} "
        f"{('✅' if task._is_me(m) else '—'):<8} "
        f"{('✅' if any(k in (m.content or '') for k in keywords) else '—'):<8} "
        f"{(m.content or '')[:30]}"
    )

ok, evidence = task.detect(datetime.now())
print("\n" + "=" * 88)
print(f"判定结果：{'✅ 今天已打卡' if ok else '❌ 今天没打卡'}" + (f"（证据：{evidence}）" if evidence else ""))
if not ok:
    print(
        "\n如果上面明明有一行你自己发的打卡：\n"
        "  · 「是我发的」那列是 — ：sender 对不上，把 config.yaml 的 checkin.sender 留空即可放开；\n"
        "  · 「含关键词」那列是 — ：微信里那条打卡的文字和 keywords 不匹配（比如打卡是图片/小程序卡片）；\n"
        "  · 整张表里根本没有那条打卡：wxauto4 没把它读出来（类型被跳过），把这张表发我。"
    )
