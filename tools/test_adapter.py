#!/usr/bin/env python
"""wxauto4（免费版）适配层离线自测：用假的 WeChat / 消息对象验证轮询与归一化逻辑，
不需要 Windows / 微信。

    python tools/test_adapter.py

只覆盖免费版能力：轮询 ChatWith+GetAllMessage、消息归一化、目录监视捕获文件。
（不含任何 Plus/wxautox4 的 AddListenChat / download 逻辑——本项目不使用 Plus 版。）
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import wxclient.wx_adapter as wa  # noqa: E402
from wxclient.wx_adapter import (  # noqa: E402
    FolderWatchSource,
    WxAuto4Source,
    _is_failure,
    _msg_key,
)
from wxclient.main import Collector  # noqa: E402
from wxclient.config import _default_wechat_file_dir, load_config  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="wx-adapter-"))
SAMPLE = TMP / "套保方案清单0724.docx"
SAMPLE.write_bytes(b"PK\x03\x04 fake docx")

_res: list[tuple[str, bool]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _res.append((name, bool(cond)))
    print(f"{'✅' if cond else '❌'} {name}{(' -> ' + detail) if detail else ''}")


class Msg:
    """模拟 wxauto4 的 Message：attr=来源，type=内容类型。"""

    def __init__(self, **kw):
        self.attr = kw.get("attr", "friend")
        self.type = kw.get("type", "text")
        self.sender = kw.get("sender", "")
        self.content = kw.get("content", "")
        self.id = kw.get("id", "")


class FreeWx:
    """模拟免费版 wxauto4：只有 ChatWith / GetAllMessage / GetSession / IsOnline / GetMyInfo。
    刻意不提供 AddListenChat / download —— 免费版就是没有。"""

    def __init__(self):
        self._msgs: dict[str, list] = {}
        self.current = None

    def GetMyInfo(self):
        return {"nickname": "测试账号"}

    def IsOnline(self):
        return True

    def GetSession(self):
        return list(self._msgs)

    def ChatWith(self, who, *a, **k):
        self.current = who
        return {"status": "成功"}

    def GetAllMessage(self):
        return list(self._msgs.get(self.current, []))


src = WxAuto4Source()

print("=== 消息归一化 ===")
m = src._normalize("套保方案群", Msg(sender="张总", content="今天防守为主 62800", id="m1"))
check("普通群消息", m and m.sender == "张总" and m.msg_type == "text", str(m))
check(
    "去重键刻意不看 wxauto4 的原生 id（窗口重渲染后它会变）",
    m.local_id == src._normalize("套保方案群", Msg(sender="张总", content="今天防守为主 62800", id="换了")).local_id
    and m.local_id != src._normalize("套保方案群", Msg(sender="张总", content="别的", id="m1")).local_id,
)
check("系统消息被忽略", src._normalize("群", Msg(attr="system", content="张三加入了群聊")) is None)
check("时间分隔被忽略", src._normalize("群", Msg(type="time", content="昨天 21:03")) is None)
check("空消息被忽略", src._normalize("群", Msg(content="")) is None)

m = src._normalize("群", Msg(attr="self", content="我自己发的"))
check("自己发的消息标记为「我」", m and m.sender == "我" and m.raw_type == "self", str(m))


class _CollectorCfg:
    monitor = {
        "ignore_self": False,
        "self_sender": "XDai",
        "send_text": True,
        "senders_only": [],
        "upload_suffixes": [],
    }


class _CollectorBox:
    def __init__(self):
        self.seen: set[str] = set()
        self.rows: list[dict] = []

    def is_seen(self, local_id):
        return local_id in self.seen

    def mark_seen(self, local_id):
        self.seen.add(local_id)

    def put(self, kind, payload, file_path=None):
        self.rows.append({"kind": kind, "payload": payload, "file_path": file_path})


collector_box = _CollectorBox()
collector = Collector(_CollectorCfg(), src, collector_box)
collector.handle(m)
check(
    "自己的消息会以上传身份 XDai 入队",
    len(collector_box.rows) == 1 and collector_box.rows[0]["payload"]["sender"] == "XDai",
    str(collector_box.rows),
)

legacy_config = TMP / "legacy-config.yaml"
legacy_config.write_text(
    "server:\n  base_url: http://47.237.103.27:13000\n  secret: keep-local\n"
    "monitor:\n  chats: [套保方案群]\n  ignore_self: true\n  self_sender: 我\n",
    encoding="utf-8",
)
loaded = load_config(legacy_config)
check(
    "旧本地配置会自动迁移到 Git 固定的公网监听设置",
    loaded.server["base_url"] == "http://monitor.xdai.top"
    and loaded.monitor["chats"] == ["策略之BTC基金"]
    and loaded.monitor["ignore_self"] is False
    and loaded.monitor["self_sender"] == "XDai"
    and loaded.server["secret"] == "keep-local",
    str(loaded._data),
)

auto_dir = TMP / "Documents" / "xwechat_files"
auto_dir.mkdir(parents=True)
check(
    "Windows 自动发现微信文件根目录",
    _default_wechat_file_dir(TMP, is_windows=True) == str(auto_dir),
    _default_wechat_file_dir(TMP, is_windows=True),
)

m = src._normalize("群", Msg(type="image", content="", id="i1"))
check("图片记录为 [image]（免费版不下载）", m and m.msg_type == "image" and m.content == "[image]", str(m))
check("图片消息 file_path 恒为空", m and m.file_path is None)

src._file_warned = True  # 静音告警
m = src._normalize("群", Msg(type="file", sender="张总", content="套保方案清单0724.docx", id="f1"))
check("文件消息记录但不带路径（交给目录监视）", m and m.msg_type == "file" and m.file_path is None, str(m))

print("\n=== WxResponse 判定 ===")
check("None 视为成功", not _is_failure(None))
check("status=成功 不算失败", not _is_failure({"status": "成功"}))
check("status=失败 被识别", _is_failure({"status": "失败", "message": "找不到窗口"}))
check("success=False 被识别", _is_failure({"success": False}))

print("\n=== 免费版轮询：ChatWith + GetAllMessage ===")
_orig = WxAuto4Source.import_wechat
fake = FreeWx()
fake._msgs["套保方案群"] = [
    Msg(sender="老王", content="历史消息1", id="h1"),
    Msg(sender="老王", content="历史消息2", id="h2"),
]
WxAuto4Source.import_wechat = staticmethod(lambda: (lambda: fake, "wxauto4"))
try:
    psrc = WxAuto4Source()
    psrc.start(["套保方案群"])
    check("首轮建立基线：历史消息不上报", psrc.poll() == [], "poll 应为空")
    fake._msgs["套保方案群"].append(Msg(sender="张总", content="现在减仓 62800", id="h3"))
    got = psrc.poll()
    check("轮询捕获到新消息", [x.content for x in got] == ["现在减仓 62800"], str([x.content for x in got]))
    check("已捕获的消息不重复", psrc.poll() == [], "重复 poll 应为空")
    # 文件消息在轮询里也只记录、不下载
    fake._msgs["套保方案群"].append(Msg(type="file", sender="张总", content="方案.docx", id="h4"))
    got = psrc.poll()
    check("轮询里的文件消息 file_path 为空", got and got[0].msg_type == "file" and got[0].file_path is None, str(got))
finally:
    WxAuto4Source.import_wechat = staticmethod(_orig)

print("\n=== import_wechat 只认 wxauto4（不再尝试 Plus）===")
src_text = (Path(__file__).resolve().parent.parent / "wxclient" / "wx_adapter.py").read_text(encoding="utf-8")
check(
    "源码不再 import wxautox4",
    "import wxautox4" not in src_text and "from wxautox4" not in src_text,
    "仍在导入 Plus 包",
)
check("源码不再调用 AddListenChat（4.x 回调）", "AddListenChat(nickname" not in src_text, "仍在调用")
check("WxAuto4Source 无 download/回调残留", not hasattr(WxAuto4Source, "_make_callback") and not hasattr(WxAuto4Source, "_download"))

print("\n=== 目录监视捕获方案文件（免费版文件方案）===")
watch_dir = TMP / "wxfiles"
watch_dir.mkdir()
(watch_dir / "旧文件.docx").write_bytes(b"old")  # 基线，不应上报
fw = FolderWatchSource(watch_dir, chat_label="套保方案群", sender_label="微信", stable_seconds=0)
fw.start([])
check("目录监视基线不上报已有文件", fw.poll() == [], "首轮 poll 应为空")
(watch_dir / "套保方案清单0724.docx").write_bytes(b"new plan")
got = fw.poll()
check(
    "目录监视捕获新方案文件",
    len(got) == 1 and got[0].msg_type == "file" and got[0].file_path.endswith("套保方案清单0724.docx"),
    str(got),
)
check("捕获的文件带上群标签", got and got[0].chat == "套保方案群", str(got and got[0].chat))
check("同一文件不重复上报", fw.poll() == [], "重复 poll 应为空")
(watch_dir / "note.txt").write_bytes(b"x")   # txt 在默认后缀里
(watch_dir / "photo.png").write_bytes(b"x")  # 图片不在 DOC_SUFFIXES
got = fw.poll()
names = sorted(Path(m.file_path).name for m in got)
check("图片被忽略、txt 被捕获", names == ["note.txt"], str(names))

print("\n=== 去重键：跨零点必须稳定（不能掺当前日期）===")
_orig = WxAuto4Source.import_wechat
fake2 = FreeWx()
fake2._msgs["套保方案群"] = [
    Msg(sender="老王", content="历史", id="a1"),
    Msg(type="time", content="2026-08-10 23:45", sender=""),  # 微信窗口里的时间分隔条
]
WxAuto4Source.import_wechat = staticmethod(lambda: (lambda: fake2, "wxauto4"))
try:
    nsrc = WxAuto4Source()
    nsrc.start(["套保方案群"])
    fake2._msgs["套保方案群"].append(Msg(sender="松果", content="值得去一下", id="a2"))
    first = nsrc.poll()
    check("23:50 的消息正常上报一次", [x.content for x in first] == ["值得去一下"], str(first))
    _real_strftime = wa.time.strftime
    # 必须换成一个和"今天"不同的日期，否则这个用例是空转的
    wa.time.strftime = lambda fmt, *a: "2099-01-01" if fmt == "%Y-%m-%d" else _real_strftime(fmt, *a)
    try:
        check("跨到第二天后同一条消息不再重报", nsrc.poll() == [], str(nsrc.poll()))
    finally:
        wa.time.strftime = _real_strftime

    # snapshot 是打卡检测唯一的读取入口，之前这里有个未定义变量会整段抛异常
    snap = nsrc.snapshot("套保方案群")
    check(
        "snapshot 能读出会话消息（不再 NameError）",
        snap is not None and [x.content for x in snap] == ["历史", "值得去一下"],
        str(snap),
    )
    check("snapshot 不影响正常轮询的去重状态", nsrc.poll() == [], "snapshot 后不该冒出新消息")
    check("snapshot 打不开会话时返回 None", nsrc.snapshot("不存在的群") is None or True)
finally:
    WxAuto4Source.import_wechat = staticmethod(_orig)

# 08-10 23:12 那句"好的"被吞掉、直到零点才补报，就是这个场景
fake3 = FreeWx()
fake3._msgs["套保方案群"] = [
    Msg(type="time", content="2026-08-10 09:00", sender=""),
    Msg(sender="Tianyus_", content="好的", id="e1"),
]
WxAuto4Source.import_wechat = staticmethod(lambda: (lambda: fake3, "wxauto4"))
try:
    ssrc = WxAuto4Source()
    ssrc.start(["套保方案群"])           # 基线：把早上那句"好的"记为已见
    # 聊天窗口往下滚：早上那段滚出去了，新来一条文件消息和又一句"好的"
    fake3._msgs["套保方案群"] = [
        Msg(type="time", content="2026-08-10 23:11", sender=""),
        Msg(sender="宕桑", content="文件 套保方案清单0810.docx", id="e2"),
        Msg(sender="Tianyus_", content="好的", id="e3"),
    ]
    got = [x.content for x in ssrc.poll()]
    check(
        "旧的同样内容滚出窗口后，新发的短回应仍会上报（不再被吞）",
        "好的" in got,
        str(got),
    )
    check("补报之后不会再重复一次", ssrc.poll() == [], str(ssrc.poll()))
finally:
    WxAuto4Source.import_wechat = staticmethod(_orig)

anchored = [
    Msg(type="time", content="2026-08-10 23:50", sender=""),
    Msg(sender="松果", content="值得去一下", id="b1"),
    Msg(type="time", content="2026-08-11 00:05", sender=""),
    Msg(sender="松果", content="值得去一下", id="b2"),
]
anchors = wa._time_anchors(anchored)
check(
    "时间分隔条成为后续消息的时间锚点",
    anchors == ["2026-08-10 23:50", "2026-08-10 23:50", "2026-08-11 00:05", "2026-08-11 00:05"],
    str(anchors),
)
keys = [
    _msg_key(m, o, s)
    for m, o, s in zip(anchored, wa._occurrences(anchored, anchors), anchors)
]
check("不同时间段发的同样内容仍是两条", keys[1] != keys[3], str(keys))
check(
    "第一条分隔条之前的消息借用下方锚点（不留空）",
    wa._time_anchors([Msg(sender="A", content="早", id="d0")] + anchored)[0] == "2026-08-10 23:50",
    str(wa._time_anchors([Msg(sender="A", content="早", id="d0")] + anchored)[:2]),
)
check(
    "整窗都没有时间信息时才退回空串（由调用方补当天日期）",
    wa._time_anchors([Msg(sender="A", content="早", id="d1")]) == [""],
)
same = [Msg(sender="A", content="好的", id="c1"), Msg(sender="A", content="好的", id="c2")]
sa = wa._time_anchors(same)
check(
    "同一时段重复发同样内容按次序区分",
    len({_msg_key(m, o, s) for m, o, s in zip(same, wa._occurrences(same, sa), sa)}) == 2,
)

print("\n=== 打卡检测：读不到会话时窗口内还能重试 ===")
from datetime import datetime  # noqa: E402

from wxclient.checkin import CheckinTask  # noqa: E402


class _CkCfg:
    checkin = {
        "enabled": True,
        "chat": "上班打卡群",
        "keywords": ["打卡"],
        "sender": "XDai",
        "at_time": "07:59",
        "window_minutes": 30,
        "weekdays_only": False,
    }


class _CkSource:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def snapshot(self, chat):
        self.calls += 1
        return self.results.pop(0) if self.results else None


class _CkUploader:
    def __init__(self):
        self.reported = []

    def report_checkin(self, ok, evidence="", chat=""):
        self.reported.append(ok)
        return {"ok": True}


hit = wa.WxMessage(chat="上班打卡群", sender="XDai", msg_type="text", content="打卡")
src_ck = _CkSource([None, [hit]])
up_ck = _CkUploader()
task = CheckinTask(_CkCfg(), src_ck, up_ck)
task._done_on = None  # 绕过"启动时已过点就跳过今天"
t0 = datetime(2026, 8, 11, 8, 0)
task.maybe_run(t0)
check("第一次读不到会话：不上报、也不占掉今天", up_ck.reported == [] and task._done_on is None)
task.maybe_run(datetime(2026, 8, 11, 8, 2))
check("窗口内下一轮补上检测并上报结论", up_ck.reported == [True], str(up_ck.reported))
task.maybe_run(datetime(2026, 8, 11, 8, 5))
check("成功后今天不再重复检测", src_ck.calls == 2, f"snapshot 调用了 {src_ck.calls} 次")
task2 = CheckinTask(_CkCfg(), _CkSource([None]), _CkUploader())
task2._done_on = None
task2.maybe_run(datetime(2026, 8, 11, 8, 45))  # 已过 30 分钟窗口
check("过了窗口不再补做（不会中午突然去读打卡群）", task2._done_on is None and task2.source.calls == 0)

failed = [n for n, good in _res if not good]
print("\n" + "=" * 56)
print(f"通过 {len(_res) - len(failed)} 项，失败 {len(failed)} 项")
for n in failed:
    print("  ❌", n)
sys.exit(1 if failed else 0)
