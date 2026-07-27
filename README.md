# 微信群监控客户端（wxauto）

监听指定微信群的**文本消息**和**文件**，上报到监控服务端。服务端负责解析方案、盯价格、报警。

> 本仓库是公开仓库：**不含任何密钥**。`config.yaml`、`data/`、日志都已在 `.gitignore` 中，
> 上报密钥只存在于运行这个客户端的那台机器上（或环境变量里）。

## 运行环境

- Windows 10/11（wxauto 通过 UI 自动化操作微信，必须是 Windows）
- 微信**桌面版**并已登录（建议 3.9.x；wxauto 对 4.x 支持有限）
- Python 3.9+
- 微信窗口不要最小化到托盘、不要锁屏；建议用一台常开的机器/云桌面专门跑

## 安装

```bat
git clone https://github.com/<你的账号>/wx-plan-monitor-client.git
cd wx-plan-monitor-client
copy config.example.yaml config.yaml
notepad config.yaml
```

`config.yaml` 至少要改三处：

```yaml
server:
  base_url: http://你的服务器:8000    # 建议上 HTTPS
  client_id: wx-pc-1                 # 与服务端 server.clients 的 key 一致
  secret: 与服务端一致的长随机串       # 也可用环境变量 BARK_CLIENT_SECRET
monitor:
  chats: ["套保方案群"]               # 群名必须与微信里显示的完全一致
```

然后双击 `run.bat`（自动建虚拟环境、装依赖、启动），或手动：

```bat
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
python -m wxclient.main
```

## 命令行

```bash
python -m wxclient.main                      # 正常运行
python -m wxclient.main --ping               # 只测服务端连通性 + 签名（排障第一步）
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

1. `wx_adapter.py` 调 `wxauto.WeChat().AddListenChat(...)` 监听群，轮询 `GetListenMessage()`；
   各版本 wxauto 的参数与文件下载方式不同，这里做了多路兜底（属性 → `download()` → content 本身是路径）。
2. 每条消息算一个 `local_id`（有原生消息 id 用它，否则用 群+人+内容+分钟 的哈希）做去重。
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

| 现象 | 原因 / 处理 |
|---|---|
| `未安装 wxauto` | `pip install wxauto`；非 Windows 请用 `--mock-dir` |
| `监听群「xxx」失败` | 群名与微信显示不一致（有空格、emoji），或该群不在会话列表里；先在微信里打开一次这个群 |
| 收到消息但没有文件 | 微信没自动下载附件。wxauto 需要 `savefile=True`（已默认）；也可在微信设置里开启自动下载 |
| `HTTP 401 签名校验失败` | client_id/secret 与服务端不一致 |
| `时间戳超出容忍窗口` | 客户端时钟不准，校准 Windows 时间 |
| 日志一直重试 | 服务端不可达或防火墙未放行端口；队列会保留数据，修好后自动补发 |
| 微信退出登录/被顶号 | 客户端会持续报采集异常并等待，重新登录后自动恢复 |
