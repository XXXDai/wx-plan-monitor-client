#!/usr/bin/env python
"""wxauto4 适配层离线自测：用假的消息对象验证归一化逻辑，不需要 Windows / 微信。

    python tools/test_adapter.py

如果你的 wxauto4 版本行为不同（比如 download() 返回的结构变了），
先改这里的假对象复现，再改 wxclient/wx_adapter.py，改完这个脚本必须全绿。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wxclient.wx_adapter import (  # noqa: E402
    FolderWatchSource,
    WxAuto4Source,
    _is_failure,
    _msg_key,
)

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
        self._download_result = kw.get("download_result")
        self.download_calls: list[dict] = []
        if "download_result" in kw:
            self.download = self._download  # 只有文件/图片消息才有 download

    def _download(self, **kwargs):
        self.download_calls.append(kwargs)
        return self._download_result


class Chat:
    def __init__(self, name):
        self.chat_name = name


src = WxAuto4Source(save_pic=False, download_dir=TMP / "dl")
DL = True  # allow_download=True 模拟 Plus/回调路径

print("=== 消息归一化 ===")
m = src._normalize("套保方案群", Msg(sender="张总", content="今天防守为主 62800", id="m1"), DL)
check("普通群消息", m and m.sender == "张总" and m.msg_type == "text", str(m))
check("用原生消息 id 做去重键", m.local_id == src._normalize("套保方案群", Msg(sender="张总", content="别的内容", id="m1"), DL).local_id)

check("系统消息被忽略", src._normalize("群", Msg(attr="system", content="张三加入了群聊"), DL) is None)
check("时间分隔被忽略", src._normalize("群", Msg(type="time", content="昨天 21:03"), DL) is None)
check("空消息被忽略", src._normalize("群", Msg(content=""), DL) is None)

m = src._normalize("群", Msg(attr="self", content="我自己发的"), DL)
check("自己发的消息标记为「我」", m and m.sender == "我" and m.raw_type == "self", str(m))

m = src._normalize("群", Msg(type="image", content="", id="i1"), DL)
check("不下载图片时仍保留一条记录", m and m.msg_type == "image" and m.content == "[image]", str(m))
check("不下载图片时不调 download", m and m.file_path is None)

print("\n=== 文件下载（Plus / 回调路径，allow_download=True）===")
# ① download() 直接返回路径字符串
msg = Msg(type="file", sender="张总", content="套保方案清单0724.docx", id="f1", download_result=str(SAMPLE))
m = src._normalize("套保方案群", msg, DL)
check("文件消息拿到本地路径", m and m.file_path == str(SAMPLE), str(m))
check("download 带上了 dir_path", msg.download_calls and "dir_path" in msg.download_calls[0], str(msg.download_calls))
check("文件消息类型为 file", m and m.msg_type == "file", str(m and m.msg_type))

# ② download() 返回 WxResponse 风格的 dict
msg = Msg(type="file", content="a.docx", id="f2", download_result={"status": "成功", "data": str(SAMPLE)})
m = src._normalize("群", msg, DL)
check("download 返回 dict 也能取到路径", m and m.file_path == str(SAMPLE), str(m))

# ③ download() 返回列表
msg = Msg(type="file", content="b.docx", id="f3", download_result=[str(SAMPLE)])
check("download 返回 list 也能取到路径", src._normalize("群", msg, DL).file_path == str(SAMPLE))

# ④ download() 失败：返回不存在的路径
msg = Msg(type="file", content="c.docx", id="f4", download_result=r"C:\不存在\c.docx")
m = src._normalize("群", msg, DL)
check("下载失败时不返回假路径，但消息仍上报", m and m.file_path is None and m.content == "c.docx", str(m))

print("\n=== 免费版：不下载文件（allow_download=False）===")
msg = Msg(type="file", sender="张总", content="套保方案清单0724.docx", id="f5", download_result=str(SAMPLE))
src._file_warned = True  # 静音告警
m = src._normalize("套保方案群", msg, False)
check("免费版文件消息不调 download", not msg.download_calls, str(msg.download_calls))
check("免费版仍上报文件消息(留给目录监视/记录)", m and m.msg_type == "file" and m.file_path is None, str(m))

print("\n=== AddListenChat 返回值判定 ===")
check("Chat 实例视为成功", not _is_failure(Chat("套保方案群")))
check("None 视为成功（老版本无返回值）", not _is_failure(None))
check("WxResponse 失败被识别", _is_failure({"status": "失败", "message": "找不到该聊天窗口"}))
check("success=False 被识别", _is_failure({"success": False, "message": "err"}))
check("status=成功 不算失败", not _is_failure({"status": "成功"}))

print("\n=== 回调 -> 队列 -> poll()（Plus 路径）===")
lsrc = WxAuto4Source(save_pic=False, download_dir=TMP / "dl2")
lsrc.use_listen = True
lsrc._make_callback("套保方案群")(Msg(sender="张总", content="第一条", id="q1"), Chat("套保方案群"))
lsrc._make_callback("套保方案群")(Msg(sender="小王", content="第二条", id="q2"), None)
lsrc._make_callback("套保方案群")(Msg(attr="system", content="系统消息"), Chat("套保方案群"))
got = [m for m in lsrc.poll() if m]
check("回调消息按顺序进入队列", [m.content for m in got] == ["第一条", "第二条"], str([m.content for m in got]))
check("chat 名字取自回调参数/兜底", all(m.chat == "套保方案群" for m in got), str([m.chat for m in got]))
check("再次 poll 为空", lsrc.poll() == [])

print("\n=== 免费版轮询：ChatWith + GetAllMessage（无 AddListenChat）===")


class FreeWx:
    """模拟免费版 wxauto4：只有 ChatWith / GetAllMessage / GetSession，没有 AddListenChat。"""

    def __init__(self):
        self._msgs: dict[str, list] = {}
        self.current = None

    def GetSession(self):
        return list(self._msgs)

    def ChatWith(self, who, *a, **k):
        self.current = who
        return {"status": "成功"}

    def GetAllMessage(self):
        return list(self._msgs.get(self.current, []))


import wxclient.wx_adapter as wa  # noqa: E402

_orig_import = wa.WxAuto4Source.import_wechat
fake = FreeWx()
fake._msgs["套保方案群"] = [Msg(sender="老王", content="历史消息1", id="h1"),
                          Msg(sender="老王", content="历史消息2", id="h2")]
wa.WxAuto4Source.import_wechat = staticmethod(lambda: (lambda: fake, "wxauto4"))
try:
    psrc = wa.WxAuto4Source(mode="auto")
    psrc.start(["套保方案群"])
    check("免费版自动切换到轮询", psrc.use_listen is False, f"use_listen={psrc.use_listen}")
    check("首轮建立基线：历史消息不上报", psrc.poll() == [], "poll 应为空")
    # 来新消息
    fake._msgs["套保方案群"].append(Msg(sender="张总", content="现在减仓 62800", id="h3"))
    got = psrc.poll()
    check("轮询捕获到新消息", [m.content for m in got] == ["现在减仓 62800"], str([m.content for m in got]))
    check("已捕获的消息不重复", psrc.poll() == [], "重复 poll 应为空")
finally:
    wa.WxAuto4Source.import_wechat = staticmethod(_orig_import)

print("\n=== listen 模式遇到免费版应报错 ===")
fake2 = FreeWx()
wa.WxAuto4Source.import_wechat = staticmethod(lambda: (lambda: fake2, "wxauto4"))
try:
    bad = wa.WxAuto4Source(mode="listen")
    try:
        bad.start(["群"])
        check("强制 listen 但免费版无 AddListenChat 应抛错", False, "没有抛错")
    except RuntimeError as e:
        check("强制 listen 但免费版无 AddListenChat 应抛错", "AddListenChat" in str(e) or "Plus" in str(e), str(e)[:60])
finally:
    wa.WxAuto4Source.import_wechat = staticmethod(_orig_import)

print("\n=== 目录监视捕获方案文件（免费版文件方案）===")
watch_dir = TMP / "wxfiles"
watch_dir.mkdir()
(watch_dir / "旧文件.docx").write_bytes(b"old")  # 基线，不应上报
fw = FolderWatchSource(watch_dir, chat_label="套保方案群", sender_label="微信", stable_seconds=0)
fw.start([])
check("目录监视基线不上报已有文件", fw.poll() == [], "首轮 poll 应为空")
(watch_dir / "套保方案清单0724.docx").write_bytes(b"new plan")
got = fw.poll()
check("目录监视捕获新方案文件", len(got) == 1 and got[0].msg_type == "file" and got[0].file_path.endswith("套保方案清单0724.docx"), str(got))
check("捕获的文件带上群标签", got and got[0].chat == "套保方案群", str(got and got[0].chat))
check("同一文件不重复上报", fw.poll() == [], "重复 poll 应为空")
(watch_dir / "note.txt").write_bytes(b"x")  # 非方案后缀? txt 在默认后缀里，应捕获
(watch_dir / "photo.png").write_bytes(b"x")  # 图片不在 DOC_SUFFIXES，应忽略
got = fw.poll()
names = sorted(m.file_path.split("/")[-1] for m in got)
check("图片被忽略、txt 被捕获", names == ["note.txt"], str(names))

failed = [n for n, good in _res if not good]
print("\n" + "=" * 56)
print(f"通过 {len(_res) - len(failed)} 项，失败 {len(failed)} 项")
for n in failed:
    print("  ❌", n)
sys.exit(1 if failed else 0)
