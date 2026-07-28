# 微信群监控客户端（wxauto）

监听指定微信群的**文本消息**和**文件**，上报到监控服务端。服务端负责解析方案、盯价格、报警。

> 本仓库是公开仓库：**不含任何密钥**。`config.yaml`、`data/`、日志都已在 `.gitignore` 中，
> 上报密钥只存在于运行这个客户端的那台机器上（或环境变量里）。

## 运行环境

- Windows 10/11（wxauto 通过 UI 自动化操作微信，必须是 Windows）
- 微信**桌面版**并已登录
- Python **3.9–3.12**（wxauto4 免费版的限制）
- 微信窗口不要最小化到托盘、不要锁屏；建议用一台常开的机器/云桌面专门跑

**本项目只用免费版**（`wxauto4`），不使用、也不安装 Plus 版 `wxautox4`。

| 微信客户端版本 | 装哪个包 | 监听方式 | 群文件 |
|---|---|---|---|
| **4.1.8.107**（当前在用）| `pip install wxauto4`（免费）| **轮询** `ChatWith`+`GetAllMessage` | 目录监视捕获 |
| 3.9.x | `pip install wxauto` | 轮询 `GetListenMessage` | wxauto 自带下载 |

> ⚠️ **免费版 wxauto4 的两个限制**（`AddListenChat`、`FileMessage.download()` 都是 Plus 专属，本项目不用）：
> 1. **没有后台监听** → 用轮询：每隔 `poll_interval` 秒（默认 10s）挨个 `ChatWith(群)` 再
>    `GetAllMessage()` 比对出新消息。副作用：会来回切换微信当前聊天窗口，属正常现象。
> 2. **不能下载群文件** → 方案文件由**目录监视**捕获：在微信设置里开启「自动下载」，
>    并把微信文件保存目录填到 `monitor.wechat_file_dir`（见下），程序会自动上传里面新出现的
>    方案文件（docx/xlsx/pdf…）。

### 配置微信文件目录（免费版必做，否则收不到方案文件）

微信 4.x 收到的群文件默认存在类似这个路径（先在微信「设置 → 文件管理」确认你的实际目录）：

```
C:\Users\你的用户名\Documents\xwechat_files\wxid_xxxxxx\msg\file
```

填进 `config.yaml`：

```yaml
monitor:
  wechat_file_dir: "C:\\Users\\你的用户名\\Documents\\xwechat_files\\wxid_xxxxxx\\msg\\file"
```

并在微信里开启群文件「自动下载」（或每次手动点一下下载），文件落盘后本客户端会在几秒内捕获并上传。

## 安装

```bat
git clone https://github.com/<你的账号>/wx-plan-monitor-client.git
cd wx-plan-monitor-client
copy config.example.yaml config.yaml
notepad config.yaml
```

`config.yaml` 只需要保留本机凭证和微信文件目录：

```yaml
server:
  base_url: http://monitor.xdai.top   # 公网入口；不需要 VPN，勿填 47.237.103.27:13000
  client_id: wx-pc-1                 # 与服务端 server.clients 的 key 一致
  secret: 与服务端一致的长随机串       # 也可用环境变量 BARK_CLIENT_SECRET
monitor:
  wechat_file_dir: "C:\\Users\\你的用户名\\Documents\\xwechat_files\\...\\msg\\file"
```

当前生产客户端把 `server.base_url`、监听群、自消息开关和 `self_sender` 固定在 Git 代码里：
拉取新版后即使本地 `config.yaml` 仍是旧值，也会自动使用 `http://monitor.xdai.top`、
`策略之BTC基金`、`ignore_self: false` 和 `XDai`。本地文件只保留 `client_id`、`secret` 和微信下载目录，
因为这些不能提交到 Git。

**装完先自检**（强烈建议，能一次性定位 90% 的问题）：

```bat
python tools\test_link.py
```

它会依次检查：配置 → 微信是否连得上/群名对不对 → 服务端是否可达 → 签名与时钟 →
价格监控是否新鲜 → DeepSeek 与报警渠道是否配好 → 上报一条测试消息。
加 `--alert` 会真推一条报警（验证手机能不能收到），加 `--file 方案.docx` 会顺带验证解析链路。

然后双击 `run.bat`（自动建虚拟环境、装依赖、启动），或手动：

```bat
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
python -m wxclient.main
```

## 命令行

```bash
python tools/test_link.py                    # 链路自检（排障第一步）
python tools/test_link.py --alert --file 方案.docx   # 连报警和解析一起验
python tools/test_link.py --skip-wx          # 只测服务端
python tools/test_adapter.py                 # wxauto4 适配层离线自测（改适配层后跑）

python -m wxclient.main                      # 正常运行
python -m wxclient.main --ping               # 只测服务端连通性 + 签名
python -m wxclient.main --mock-dir mock_in   # 模拟模式：不连微信，从目录读 json（macOS/Linux 可用）
python -m wxclient.main --send-file 方案.docx --chat 套保群 --sender 张总   # 手工补报一个文件
python -m wxclient.main --config D:\conf\client.yaml
```

模拟模式的消息格式（放进 `mock_in/*.json`，处理后自动改名为 `.json.done`）：

```json
{"chat": "套保方案群", "sender": "张总", "type": "file", "file": "plan.docx", "content": "[文件] plan.docx"}
{"chat": "套保方案群", "sender": "张总", "content": "今天防守为主，62800 是强支持"}
```

## 工作方式

1. `wx_adapter.py` 监听群消息：
   - **wxauto4 免费版（微信 4.1.8.107）**：**轮询** `ChatWith(群)`+`GetAllMessage()`，
     用消息 `id/hash` 比对出新消息（首轮建基线，不补历史）；文件由 `FolderWatchSource` 监视
     `wechat_file_dir` 捕获。消息来源用 `msg.attr`（system/self/friend），内容类型用 `msg.type`。
   - **wxauto（微信 3.9.x）**：轮询 `GetListenMessage()`。
2. 每条消息算一个 `local_id`（有原生消息 id 用它，否则用 群+人+内容+分钟 的哈希）做去重。默认连 XDai 自己的消息也会上报，并按 `self_sender` 写为 `XDai`，让服务端能理解群内对话角色。
3. 消息/文件先进本地 SQLite 队列 `data/outbox.db`，再由上报线程发送；
   **断网、服务端重启、微信卡死都不会丢消息**，恢复后按序补发（指数退避，最长 5 分钟一次）。
4. 每 2 分钟发一次心跳（含主机名、队列积压、监听的群），服务端 `/api/v1/status` 能看到。

上报走 HMAC-SHA256 签名 + 时间戳，签名规则见 `wxclient/uploader.py` 顶部注释，与服务端 `app/security.py` 一致。

## 只上报文件、不上报聊天内容？

方案文件里有价格点位，必须上传；聊天内容如果不想全量外发，可以：

```yaml
monitor:
  send_text: false          # 完全不上报文本消息（只传文件）
  # 或者只上报关键人的发言：
  send_text: true
  senders_only: ["张总", "风控-李"]
```

注意：服务端的"特定人发言报警"依赖客户端把该人的消息传上去，所以 `senders_only` 里要包含需要报警的人。

## 常见问题

先跑 `python tools/test_link.py`，它会直接告诉你卡在哪一段。

| 现象 | 原因 / 处理 |
|---|---|
| `未安装微信 4.x 版的 wxauto` | `pip install wxauto4`（微信 4.1.8.107）；非 Windows 请用 `--mock-dir` |
| 日志里「免费版无法下载群文件」 | 正常。免费版 `download()` 是 Plus 专属；配 `monitor.wechat_file_dir` + 微信开自动下载即可捕获文件 |
| 收到方案文件但服务端没收到 | 检查 `wechat_file_dir` 路径对不对、微信是否真把文件下到了那里；日志应有「目录监视捕获文件」 |
| 微信窗口一直被切来切去 | 免费版轮询模式的正常现象（挨个 `ChatWith`）；可把 `poll_interval` 调大一点减少频率 |
| 装了 `wxauto` 但连不上微信 4.x | 老包只支持 3.9.x，换 `wxauto4` |
| `ImportError` / 安装失败 | wxauto4 免费版只支持 Python 3.9–3.12，3.13 装不上 |
| `监听群「xxx」失败` / 收不到消息 | 群名与微信显示不一致（有空格、emoji），或该群不在会话列表里；先在微信里打开一次这个群。`test_link.py` 会列出可见会话并提示相近的名字 |
| `HTTP 401 签名校验失败` | client_id/secret 与服务端不一致 |
| `时间戳超出容忍窗口` | 客户端时钟不准，校准 Windows 时间 |
| 日志一直重试 | 服务端不可达或防火墙未放行端口；队列会保留数据，修好后自动补发 |
| 微信退出登录/被顶号 | 客户端会持续报采集异常并等待，重新登录后自动恢复 |
