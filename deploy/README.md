# 部署手册

从零把 lark-copilot 跑到生产的完整步骤。涵盖飞书应用配置、部署机环境、env、首次接通、
群聊启用、systemd 常驻、smoke test 与回滚。

> 概念性背景（身份模型、wire protocol、为什么需要这层）见根 [README.md](../README.md)；
> 本文只关心"按步骤把它跑起来"。

---

## 0. 前置检查清单

部署前确认：

- [ ] 拿到飞书企业自建应用的 **app_id / app_secret**（或有权创建一个）
- [ ] 部署机能访问 `open.feishu.cn`（HTTPS 443）—— 走代理也行
- [ ] 部署机已装 / 能装 [Claude Code](https://docs.claude.com/claude-code)（agent SDK 复用它的鉴权和模型配置）
- [ ] Python ≥ 3.12
- [ ] [uv](https://github.com/astral-sh/uv) 已装
- [ ] [lark-cli](https://github.com/larksuite/lark-cli) 已装（或愿意装）

---

## 1. 飞书应用配置

### 1.1 创建应用，启用机器人能力

事件 `im.message.receive_v1` 是 bot 主体接收的，所以**必须**开启"机器人"能力。
bot 不需要起名 / 拉群 / 被人 DM，但它得"存在"才能收事件、发回复。

### 1.2 权限管理 —— 勾全下面这些 scope

| scope | 身份列 | 用途 |
|---|---|---|
| `im:message.p2p_msg:readonly` | **应用身份** | bot 接收 DM 的 `im.message.receive_v1` 事件 |
| `im:message.group_at_msg:readonly` | **应用身份** | bot 接收群里 @ 自己的 `im.message.receive_v1` 事件（**群聊功能必需**） |
| `im:message:send` | **应用身份** | bot 发 ack envelope / 群里 `+messages-reply` 引用回复 |
| `docx:document:readonly` | **用户身份** | `lark-cli docs +fetch` 拉飞书文档 |
| `drive:drive:readonly`（可选） | 用户身份 | 搜云空间文档 |

> **如果只跑 DM 不跑群聊**：`im:message.group_at_msg:readonly` 可以不勾。但建议都勾，
> 后续启用群聊不用重新审核。

### 1.3 事件订阅

- 网关选 **长连接（WebSocket）**。lark-cli `event consume` 走这条，部署机不需要公网回调。
- 应用身份订阅 → 添加 `im.message.receive_v1`。
- 平台可能强制要求附加 scope（比如 `im:resource`），按提示勾上即可。

### 1.4 改完 scope 必须发版

scope 变更要发新版本并过管理员审核。**审核通过后所有已 `auth login` 过的 user 都得重新登录一次**，
否则旧 token 里的 scope 不更新。这步是部署期最常见的踩坑点。

---

## 2. 部署机环境

### 2.1 装 lark-cli

按官方 [README](https://github.com/larksuite/lark-cli) 装即可。装完后：

```bash
lark-cli --version
```

### 2.2 装 Claude Code

claude-agent-sdk 在本项目里**继承部署机本地 Claude Code 的鉴权与模型配置**。
所以部署机必须先把 Claude Code 跑通：

```bash
# 装好桌面 app 或 CLI 后
claude --version
claude /login   # 跑一次走完登录
```

> 代码里 `ClaudeAgentOptions` 不设 `model=`，也不接受按场景切模型的 env。
> 模型完全由部署机 Claude Code 的全局配置决定。

### 2.3 配 lark-cli 凭证 + 登录两个身份

```bash
# 1. 配应用凭证（一次性）
lark-cli config init
# → 提示输入 app_id / app_secret

# 2. user 身份登录（用于拉文档）
lark-cli auth login --scope docx:document:readonly
# 可选叠加：--scope drive:drive:readonly

# 3. bot 身份不需要登录，靠 app_id/app_secret 自动拿 tenant_access_token

# 4. 验证
lark-cli auth status
# 应看到 user token 已落地，scope 列表里有 docx:document:readonly
```

### 2.4 拉代码、装依赖

```bash
git clone <repo-url> lark-copilot
cd lark-copilot
uv sync
```

---

## 3. 配置 .env

```bash
cp .env.example .env
```

按下表填写 `.env`：

| 字段 | 必填 | 说明 |
|---|---|---|
| `DRY_RUN` | 是 | **首次部署保持 `1`**（只 log 不真发回复），通了再切 `0` |
| `COLLAB_SENDERS` | 跑 envelope 才填 | peer agent 的 bot open_id，逗号分隔可多个 |
| `HUMAN_SENDERS` | 跑 human / 群聊才填 | 允许提问的真人 open_id，逗号分隔可多个；**群聊 @bot 的发送者也必须在此名单**（"只在 @ 时响应"由飞书 scope 兜底，代码层不做 mentions 匹配） |
| `DOC_FETCH_TIMEOUT_SEC` | 否 | 拉文档硬超时，默认 30 |
| `INFERENCE_TIMEOUT_SEC` | 否 | 单轮推理硬超时，含 vision tool round-trip，默认 120 |
| `SESSION_TTL_MIN` | 否 | Human/群聊 会话空闲多少分钟回收，默认 30 |
| `MAX_IMAGES_PER_QUESTION` | 否 | 单轮最多拉几张图，默认 3 |
| `LARK_CLI` | 否 | lark-cli 可执行路径，默认 `lark-cli`（PATH 上能找到就不用改） |
| `HTTP_HOST` | 跑 HTTP API 才用 | HTTP API 监听地址，默认 `127.0.0.1`；**跨机调用必须**改成 `0.0.0.0` 或内网网卡 IP，否则 peer 连不上 |
| `HTTP_PORT` | 跑 HTTP API 才用 | HTTP API 端口，默认 `8800` |
| `HTTP_API_TOKEN` | 跑 HTTP API 才用 | Bearer token；留空放行（仅内网测试），**上线务必配**。生成：`python -c "import secrets;print(secrets.token_urlsafe(32))"` |

> `DRY_RUN` 只作用于 router（IM 回复）。HTTP API 是同步返回，没有 DRY_RUN 概念。
> HTTP API 复用 `DOC_FETCH_TIMEOUT_SEC` / `INFERENCE_TIMEOUT_SEC` / `MAX_IMAGES_PER_QUESTION`。

### 3.1 怎么拿 open_id

**真人 sender 的 open_id（填 `HUMAN_SENDERS`）**

```bash
# 1. 临时起 router（DRY_RUN=1，HUMAN_SENDERS/BOT_OPEN_ID 都先留空）
uv run python router.py
# 2. 用你**真人**飞书账号 DM bot 一句话（随便发一条文本）
# 3. 日志里找 `skip_untrusted_sender`，复制 `sender` 字段（形如 ou_xxxx）
# 4. 填进 .env 的 HUMAN_SENDERS，Ctrl+C 重启 router
```

**peer agent bot 的 open_id（填 `COLLAB_SENDERS`）**

由 peer agent 那侧（如 ops-qa-bot）维护方提供，或用同样的"DM 一下、看日志"方式获取。

---

## 4. 起进程 + smoke test

### 4.1 前台起，验证三条路径

```bash
uv run python router.py
```

输出 NDJSON 日志，每行一个事件。启动期应看到：

```json
{"event":"starting","cmd":"lark-cli event consume im.message.receive_v1 --as bot"}
```

不应出现：

- `warn_no_senders_configured` —— 两个白名单都没填
- `warn_no_bot_open_id_group_chats_disabled` —— 没填 `BOT_OPEN_ID`，群聊禁用
- `consume_exited rc=...` —— 进程退出，看 §6 排查

### 4.2 DM smoke test

用 `HUMAN_SENDERS` 里的真人账号 DM bot：

```
你好
```

日志应出现：

```
event_in → human_in → session_created → session_client_spawned →
human_doc_qa_in → dry_run_send_text → human_doc_qa_done
```

`DRY_RUN=1` 时，看 `dry_run_send_text` 的 `cmd` 字段确认要发的回复内容是合理的。

### 4.3 群聊 @bot smoke test

把 bot 拉进任一群，群里 @bot 发：

```
@lark-copilot 你好
```

日志应出现：

```
event_in chat_type=group → human_in_group → ... → dry_run_send_text_reply
```

注意 `dry_run_send_text_reply` 里的命令是 `+messages-reply`（**不带** `--reply-in-thread`），不是 `+messages-send`。群里 bot 走"引用回复"，UI 上挂在原 @ 消息下面。

### 4.4 envelope smoke test（如果跑 collab 模式）

让 peer agent（如 ops-qa-bot）发一条结构化 envelope DM bot，日志应出现：

```
collab_in → doc_qa_in → doc_fetched → doc_qa_done → dry_run_send_ack
```

### 4.5 切到 DRY_RUN=0

三条路径都通了之后，改 `.env`：

```
DRY_RUN=0
```

重启进程。再发一条测消息，应在飞书客户端真收到回复。

### 4.6 HTTP API smoke test（如果跑 HTTP 接口）

前台起 HTTP API（和 router 是两个独立进程）：

```bash
uv run python http_api.py
# 启动日志：{"event":"http_starting","host":"127.0.0.1","port":8800}
# 没配 token 会先打 {"event":"warn_no_http_token",...}
```

另开一个终端打探针 + 一次真实问答（`docs +fetch` 用的是已 `auth login` 的 user 身份）：

```bash
curl -s http://127.0.0.1:8800/healthz
# {"ok":true}

curl -s http://127.0.0.1:8800/doc_qa \
  -H "Authorization: Bearer $HTTP_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"docs":["<一篇你有权限的飞书文档 url>"],"q":"这份文档讲了啥"}'
# {"ok":true,"req_id":"...","answer":"...","took_ms":...}
```

服务端日志应出现 `http_doc_qa_in → http_doc_fetched → http_doc_qa_done`。

---

## 5. systemd 常驻（Linux 生产）

把 `deploy/lark-copilot.service` 复制到 `/etc/systemd/system/`，
按机器实际情况改 `User=` / `WorkingDirectory=` / `ExecStart=`：

```bash
sudo cp deploy/lark-copilot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lark-copilot
sudo systemctl status lark-copilot
```

**跑 HTTP API 的话**，再装一份独立 unit（`deploy/lark-copilot-api.service`，跑 `http_api.py`）。
两个 unit 相互独立、可同机并跑，按需启用其一或都启用：

```bash
sudo cp deploy/lark-copilot-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lark-copilot-api
journalctl -u lark-copilot-api -f
```

日志 / 升级（§5.1、§5.2）对两个 unit 一致，把 `-u lark-copilot` 换成 `-u lark-copilot-api` 即可；
升级时记得两个都 `restart`。

### 5.1 日志在哪

走 **journald**（NDJSON 一行一条）。journald 自带持久化 / rotation / 压缩，部署
零额外依赖。

```bash
# 实时跟
journalctl -u lark-copilot -f

# 最近一小时
journalctl -u lark-copilot --since "1 hour ago"

# 跨重启
journalctl -u lark-copilot --since today

# 结构化筛：-o cat 去掉 systemd 前缀后给 jq
journalctl -u lark-copilot -o cat -f | jq -c 'select(.event=="human_doc_qa_done")'

# 看某个 chat 的全部记录
journalctl -u lark-copilot -o cat --since today | jq -c 'select(.chat_id=="oc_xxx")'

# 失败事件聚类
journalctl -u lark-copilot -o cat --since today \
  | jq -r 'select(.event|test("_failed$|_error$|timeout$")) | .event' \
  | sort | uniq -c
```

> **想落到独立日志文件而不是 journald**（场景：日志要给第三方采集器 tail）：
> unit 里把 `StandardOutput=journal` / `StandardError=journal` 换成
> `StandardOutput=append:/var/log/lark-copilot/router.log` 之类，并加
> `LogsDirectory=lark-copilot` 让 systemd 自动创建带正确权限的目录。代价是要
> 自己配 logrotate 切割（systemd append: 不切）；不推荐，journald 已经够用。

### 5.2 升级

```bash
git pull
uv sync
sudo systemctl restart lark-copilot
```

---

## 6. 常见部署问题

| 现象 | 排查 |
|---|---|
| `consume_exited rc=...` 立即退出 | (1) lark-cli 没登录 (2) 应用没开"机器人" (3) `im.message.receive_v1` 没在应用身份订阅里加 (4) `im:message.p2p_msg:readonly` 没勾在**应用身份**列 |
| `--as user is not supported` | event consume 只支持 bot；代码里写了，不该出现，出现就是改坏了 |
| `send_*_failed` 报权限 | bot 缺 `im:message:send` 应用身份 scope；改完 scope 记得发版+审核 |
| `lark-cli docs +fetch` 失败 | (a) user 没登录 / 没勾 `docx:document:readonly`（**用户身份**） (b) 登录 user 对该文档没查看权限 —— 让贴文档的人先把文档分享出来 (c) scope 改过但 user 没重新 `auth login` |
| `warn_no_senders_configured` | `.env` 里 `COLLAB_SENDERS` 和 `HUMAN_SENDERS` 都没填，所有发件人都会被 `skip_untrusted_sender` 丢 |
| 群里 @bot 没反应 | (1) `im:message.group_at_msg:readonly` scope 没勾在**应用身份** —— bot 根本收不到群事件，连 `event_in chat_type=group` 都不会出现 (2) 事件订阅没加 `im.message.receive_v1` 的应用身份订阅 (3) 发件人不在 `HUMAN_SENDERS` —— 看日志 `skip_group_untrusted_sender` (4) bot 没被拉进那个群 |
| 群里 bot 回复了不该回的消息 | 你勾了更宽的 `im:message.group_msg:readonly` scope，结果 bot 能收到群里所有消息。改回 `im:message.group_at_msg:readonly` 即可——这层过滤是飞书 scope 负责，代码不再做 mentions 匹配 |
| A 调工具后超时 | (a) B 没起 / (b) B 还在 DRY_RUN=1（不会真回） / (c) `COLLAB_SENDERS` 没含 A 的 bot open_id |
| 答案过长被截 | B 自动按 ~28KB 字节裁 `answer` 字段并打 `truncated:true`；要完整内容打开原文档 |
| 媒体下载（图）超时 | 部分网络下 `docs +media-download` 走 https_proxy 会 TLS handshake 超时。`.env` 里取消注释 `LARK_CLI_NO_PROXY=1` 让它绕代理 |
| HTTP API peer 连不上 | `HTTP_HOST` 还是默认 `127.0.0.1`，只听本机；跨机调用改成 `0.0.0.0` 或内网网卡 IP 并重启。也确认防火墙放行了 `HTTP_PORT` |
| HTTP API 全返回 401 | 服务端配了 `HTTP_API_TOKEN` 但调用方没带 / 带错 `Authorization: Bearer <token>` |
| HTTP API `/doc_qa` 报 500 | 多半是 `docs +fetch` 失败（同"`lark-cli docs +fetch` 失败"那行排查）；`error` 字段有具体类型 |
| Windows 部署 | 见根 README 的 "Windows 部署须知" 小节 |

诊断的第一手段：把 `event_in` 那一行的 `raw` 字段也打出来看完整事件载荷。

---

## 7. 回滚

```bash
# 代码回滚
git log --oneline -10
git checkout <previous-good-sha>
uv sync
sudo systemctl restart lark-copilot

# 配置回滚
cp .env .env.bad
git checkout .env.example   # 看上一个版本期望的字段
# 手动改回 .env
sudo systemctl restart lark-copilot
```

应急关闭：直接 `sudo systemctl stop lark-copilot`，bot 收不到事件但不会"以错误方式回复"——
事件会堆在飞书侧，下次起进程 lark-cli 会继续 consume（取决于 lark-cli 的 event daemon 配置）。

要彻底"静默不回"：把 `DRY_RUN=1` 打开后重启，比关进程更安全（仍能看到入站事件、便于排错）。
