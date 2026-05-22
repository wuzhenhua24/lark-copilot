# lark-copilot

飞书侧的 **doc QA worker**。核心是一套"拉飞书文档 + Claude 单轮 QA（按需看图）"管道，对外有三种接入方式：

- **HTTP API**（`http_api.py`）：同内网的 peer agent 直接 `POST /doc_qa`，同步拿 answer。**peer agent 推荐走这条**——见下方 [HTTP API](#http-api)
- **Agent 模式 envelope-over-IM**（`router.py`）：跨网 / 无 HTTP 通道时的退路，peer agent 发结构化 envelope DM 过来，回 envelope ack
- **Human 模式**（`router.py`）：白名单里的真人 DM / 群里 @bot 发"飞书 URL + 问题"，回文本答案（支持多轮）

`http_api.py` 和 `router.py` 是两个独立进程，可同机各跑一个 systemd unit，互不影响。

基于 [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python) +
[lark-cli](https://github.com/larksuite/lark-cli) 搭建。

> **模型说明**：claude-agent-sdk 在本项目里继承**部署机本地 Claude Code 的鉴权与模型配置**，
> 后端是一个固定的第三方 Claude 兼容模型。所以代码里 `ClaudeAgentOptions` 不设 `model=`，
> 也没有按场景切换模型的环境变量 —— 加了也不会生效。

---

## 为什么需要这层

ops-qa-bot 部署在内网，bot 身份只有 tenant scope，**没有 user-scope 的飞书文档读取
能力**。但用户提问里经常贴飞书文档链接。

lark-copilot 跑在能访问飞书文档的机器上，**登录 user 持有 docx 读权限**，正好补这块。
两个 agent 通过飞书 IM 互发结构化 envelope 完成协作 —— 零新增网络通道、零端口暴露。

---

## 身份模型

同一进程内 per-command 切换两种身份：

| 步骤 | 身份 | 为什么 |
|---|---|---|
| `event consume im.message.receive_v1` | **bot** | 这个事件在飞书 Open Platform 是应用级的，**只支持 `--as bot`**。没有"以 user 身份订阅自己 DM"的路径。 |
| `docs +fetch` 拉文档 | **user** | docx 读权限挂在 user OAuth 上 |
| `im +messages-send` 发 ack | **bot** | 事件是 bot 收到的，回复也由 bot 发回同一 chat |

所以 peer agent（ops-qa-bot）发 envelope 时必须 DM **lark-copilot bot**，不是 DM 你
本人。chat_id 是 ops-qa-bot ↔ lark-copilot bot 这条 1v1（或者双方都在的小群）。

---

## 链路

```
                    ┌────────────────────────────────────┐
                    │ 用户在群里 @ops-qa-bot 提问           │
                    │ "这份 feishu doc 怎么处理 OOM？"      │
                    └──────────────┬─────────────────────┘
                                   │
                                   ▼
A (ops-qa-bot, 内网)   ┌─────────────────────────────────────┐
                       │ agent 决定调 ask_feishu_doc 工具      │
                       │ → DM lark-copilot bot 一条 envelope  │
                       └──────────────┬──────────────────────┘
                                      │  飞书 IM (text, JSON envelope)
                                      │  {"op":"doc_qa","req_id":"...","docs":["...", ...],"q":"..."}
                                      ▼
B (lark-copilot,   ┌──────────────────────────────────────────────┐
   有飞书出口)     │ router.py (--as bot 接事件)                   │
                   │  ├─ sender ∈ COLLAB_SENDERS? 不在则丢          │
                   │  └─ agent_collab.handle_doc_qa(evt)           │
                   │     ├─ lark-cli docs +fetch --as user        │
                   │     ├─ Claude 单轮 Q&A                        │
                   │     └─ lark-cli im +messages-send --as bot   │
                   └────────────────┬─────────────────────────────┘
                                    │  ack envelope
                                    ▼
A: rpc.try_deliver 命中 req_id → 唤醒 await 的 Future → tool 返回 answer → agent 整合
```

---

## Wire protocol

请求（A → B）：

```json
{"op":"doc_qa","req_id":"<uuid12>","docs":["<feishu-url>", "..."],"q":"<question>"}
```

成功回复：

```json
{"op":"doc_qa_ack","req_id":"<same>","ok":true,"answer":"..."}
```

失败回复：

```json
{"op":"doc_qa_ack","req_id":"<same>","ok":false,"error":"timeout|..."}
```

超长 `answer` 字段会被按 UTF-8 字节裁到飞书文本上限内，并打上 `truncated:true`。

---

## HTTP API

给**同内网、HTTP 可达**的 peer agent 用的同步接口（`http_api.py`）。相比 envelope-over-IM：
不用飞书当 broker、不用 req_id 关联、不用 listen 回复 DM；结构化 JSON 响应，没有 IM 的
~28KB 字节上限，`answer` 不截断。

> **什么时候用哪条**：peer 与本服务 HTTP 互达 → 走 HTTP（更简洁）。peer 在隔离网段、
> 只有飞书这一条双向通道 → 退回 envelope-over-IM。两条路径共用同一套 doc QA 核心。

**身份**：本进程不订阅飞书事件，只 `docs +fetch` / `docs +media-download`，全程 `--as user`，
不需要 bot 身份。

### `POST /doc_qa`

请求体：

```json
{"docs": ["<feishu-url-or-token>", "..."], "q": "<question>", "req_id": "<可选>"}
```

成功（200）：

```json
{"ok": true, "req_id": "<uuid12>", "answer": "<markdown>", "took_ms": 1234}
```

失败：`504`（拉文档 / 推理超时）、`500`（其它异常）均为 `{"ok": false, "req_id", "error", "took_ms"}`；
`401`（鉴权失败）、`422`（参数校验失败，如 `docs` 为空）。

文档里的图以 `<image idx="N"/>` 占位喂给模型，模型按需调 `fetch_doc_image` 实拉图字节看图作答
（跨文档全局连续编号，软上限 `MAX_IMAGES_PER_QUESTION`）。`answer` 是 markdown 文本。

### `GET /healthz`

存活探针，返回 `{"ok": true}`，不鉴权。

### 鉴权

`HTTP_API_TOKEN` 设了就要求请求带 `Authorization: Bearer <token>`（常量时间比较）；
留空则放行并在启动时打 `warn_no_http_token`——仅限可信内网测试，**上线务必配上**。

### 调用示例

```bash
curl -s http://<host>:8800/doc_qa \
  -H "Authorization: Bearer $HTTP_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"docs":["https://xxx.feishu.cn/docx/xxxxx"],"q":"这份文档讲了啥"}'
```

---

> **要部署到生产**：完整的端到端 runbook 见 [`deploy/README.md`](deploy/README.md)
> （飞书应用配置 → 部署机环境 → env → smoke test → systemd → 回滚）。本文档保留
> 概念解释和快速本地试跑，不重复展开部署步骤。

---

## 飞书应用配置

本仓库依赖一个**企业自建应用**承载 lark-cli 调用。要做的事：

### 1. 创建应用，开启"机器人"能力

事件 `im.message.receive_v1` 是 bot 主体接收的，所以**必须**启用机器人能力。bot 不
需要起名/拉群/被人 DM，但它得"存在"才能收事件、发 ack。

### 2. 权限管理 —— 两列都要勾

| scope | 身份列 | 用途 |
|---|---|---|
| `im:message.p2p_msg:readonly` | **应用身份** | bot 接收 `im.message.receive_v1` 事件（peer 发来的 envelope） |
| `im:message:send` | **应用身份** | bot 发 ack envelope 回 peer |
| `docx:document:readonly` | **用户身份** | `lark-cli docs +fetch` 拉飞书文档 |
| `drive:drive:readonly`（可选） | 用户身份 | 搜云空间文档 |

### 3. 事件订阅

- 网关：**长连接（WebSocket）**（lark-cli event consume 走这条，不需要公网回调）
- 事件 → 应用身份订阅 → 添加 `im.message.receive_v1`
- 平台可能强制要求带上 `im:message.group_at_msg:readonly` 等附加 scope，按提示勾上即可

### 4. 配 lark-cli + 登录两个身份

部署机上：

```bash
# 配应用凭证
lark-cli config init   # 填 app_id / app_secret

# user 身份登录（用于拉文档）
lark-cli auth login --scope docx:document:readonly
# 可选：drive:drive:readonly

# bot 身份不需要"登录"，靠 app_id/app_secret 自动拿 tenant_access_token
lark-cli auth status   # 应能看到 user token 已落地
```

### 5. 改完 scope 要发版

加完 scope 必须发新版并过管理员审核，**所有已登录的人要重新 `auth login`**，否则
token 里还是旧 scope。

---

## 快速开始

需要 Python ≥ 3.12 和 [uv](https://github.com/astral-sh/uv)：

```bash
uv sync
```

配置 `.env`（参考 `.env.example`）：

| 字段 | 含义 |
|---|---|
| `COLLAB_SENDERS` | 受信任的 peer agent open_id（如 ops-qa-bot 的 bot open_id），逗号分隔可多个 |
| `HUMAN_SENDERS` | 允许直接 DM 提问的真人 open_id，逗号分隔可多个 |
| `DOC_FETCH_TIMEOUT_SEC` | 拉文档（lark-cli docs +fetch）的硬超时，默认 30s |
| `INFERENCE_TIMEOUT_SEC` | 单轮模型推理的硬超时，含 fetch_doc_image 工具 round-trip，默认 120s |
| `SESSION_TTL_MIN` | Human 模式会话空闲多少分钟后被回收，默认 30 |
| `MAX_IMAGES_PER_QUESTION` | 单次问题里 Claude 最多通过 fetch_doc_image 工具拉几张图，默认 3 |
| `DRY_RUN` | `1` 只 log 不真发回复；`0` 真发。**首次跑保持 1。**（仅 router） |
| `LARK_CLI` | lark-cli 可执行路径，默认 `lark-cli` |
| `HTTP_HOST` / `HTTP_PORT` | HTTP API 监听地址 / 端口，默认 `127.0.0.1` / `8800`。跨机调用改成 `0.0.0.0` 或内网网卡 IP |
| `HTTP_API_TOKEN` | HTTP API 的 Bearer token；留空放行（仅内网测试），上线务必配 |

跑起来（两个进程按需各起）：

```bash
uv run python router.py     # 飞书事件监听（Agent / Human 模式）
uv run python http_api.py   # HTTP API（peer agent 直接调用），默认 127.0.0.1:8800
```

输出 NDJSON 日志，每行一个事件。关键事件：

| event | 含义 |
|---|---|
| `starting` | 启动，附 lark-cli 命令 |
| `warn_no_senders_configured` | `COLLAB_SENDERS` 和 `HUMAN_SENDERS` 都为空，所有 DM 都会被丢 |
| `event_in` | 收到一条事件 |
| `skip_untrusted_sender` | 发件人不在任一白名单 —— 想加入 Human 模式，把这一行的 `sender` 复制到 `HUMAN_SENDERS` |
| `collab_in` / `human_in` | 命中对应白名单 |
| `doc_qa_in` / `doc_fetched` / `doc_qa_done` | envelope 路径的 checkpoint |
| `session_created` / `session_evict_idle` / `session_client_spawned` | Human 模式 per-chat 会话生命周期 |
| `human_doc_qa_in` / `human_doc_qa_done` | human 路径单轮 checkpoint |
| `human_doc_qa_fetch` / `human_doc_qa_default_summary` | 文档加载 / 裸 URL 触发默认总结 |
| `human_doc_qa_reset` | 用户触发了 /reset |
| `dry_run_send_ack` / `dry_run_send_text` | DRY_RUN 下要发的命令（实际没发） |
| `send_ack_ok` / `send_text_ok` / `*_failed` | 真实回送结果 |

### Human 模式怎么用（多轮对话版）

#### 一次性配置

1. 部署机起 router 后，从你**真人**飞书账号 DM 一下 lark-copilot bot（随便发一条）
2. 在 router 日志里找 `skip_untrusted_sender`，复制 `sender` 字段（形如 `ou_xxxx`）
3. 填到 `.env` 的 `HUMAN_SENDERS`，重启 router

#### 对话

每个**会话单元** = `(chat_id, sender_id)`，DM 和群聊统一：

- DM (1v1)：sender_id 唯一，等价于 per-chat 一份 session
- 群聊：每个用户在每个群独立一份 session，多人协作互不串扰；用户**直接再
  @bot** 就能续上一轮的文档和对话历史（不用点开话题、不用引用回复）

每份会话状态包含：

- 一份 **ClaudeSDKClient**：多轮对话历史在它内部，能记住上一轮的文档和回答
- 一份 **doc cache**：URL → Markdown，避免同一文档被重复拉取
- 一个 **last_active 时间戳**：`SESSION_TTL_MIN` 分钟无活动 → 整份状态被丢弃

`/reset` 只清当前用户在当前 chat 的会话单元（不影响别人）。

群聊里 bot 回复通过 lark-cli `+messages-reply` 走"**引用回复**"，飞书 UI 上 bot
的回复会带上原 @ 消息的引用条，一眼能看出是答哪条提问，但不开话题。这套设计
跟同仓库的 [ops-qa-bot](../ops-qa-bot) 一致。

支持的输入：

| 你发 | bot 行为 |
|---|---|
| `<URL> 这文档讲了啥` | 拉文档、入缓存、回答 |
| `<URL1> <URL2> 对比一下这两份文档` | 同时拉两篇、入缓存、回答 |
| `刚才那段 OOM 处置具体怎么做？` | 复用上一轮文档，直接回答（无需重贴 URL） |
| `<新 URL>` | 只加载，不回答；回一个"已加载 N 篇文档"的确认 |
| `<新 URL> 新问题` | 加载新文档（旧的也仍在）+ 用所有缓存文档作答 |
| `/reset` / `/new` / `重置` / `清空` | 清空当前会话状态，下次重新开始 |

⚠️ **多轮的代价**：每次新加文档都会把它整段塞进对话历史。文档越多 / 轮次越多，
单次 prompt 越大。撑爆上下文了 bot 会报模型错误——这时发 `/reset` 重开就行。

---

## 项目结构

```
lark-copilot/
├── README.md          本文件
├── pyproject.toml     依赖声明（uv 管理）
├── .env.example       配置模板
├── router.py          薄 dispatcher：event consume → 按 sender 白名单 → agent_collab
├── http_api.py        HTTP API：POST /doc_qa（同内网 peer agent 直接调用）→ agent_collab
├── agent_collab.py    doc QA 主体：fetch 文档 + Claude QA（按需看图）+ 回 ack envelope / 纯文本
└── deploy/            systemd unit（router 一份、http_api 一份）+ 部署 runbook
```

---

## 部署顺序（首次接通 ops-qa-bot）

两侧都跑起来才能 round-trip：

1. **B 侧（本仓库）**：配飞书应用、`auth login` user 身份、起 `router.py`（DRY_RUN=1）
2. **B 侧**：拿到 ops-qa-bot 的 bot open_id，填到 `COLLAB_SENDERS`
3. **A 侧（ops-qa-bot）**：拿到 **lark-copilot bot 的 open_id**（不是你 user 的！），填到对应 peer 配置
4. **A 侧**：启动 ws_server（带 ask_feishu_doc 工具）
5. **测试**：群里 @ A，让它处理某个飞书文档。观察：
   - A 的 log：`feishu_doc_rpc out: req_id=...`
   - B 的 log：`collab_in` → `doc_qa_in` → `doc_fetched` → `doc_qa_done`（或 dry_run）
   - A 的 log：`feishu_doc_rpc ack: req_id=... ok=True`
6. 通了后把 B 的 `DRY_RUN=0` 打开

---

## Windows 部署须知

代码已经做了跨平台兼容（UTF-8 stdout、CRLF 容忍），但有几点 Windows 用户需要单独注意：

| 注意点 | 说明 |
|---|---|
| 控制台编码 | router.py 入口已 `sys.stdout.reconfigure(encoding="utf-8")`，正常运行不会乱码。但**如果你重定向到第三方 log 收集（如 nssm）**，对方可能仍按系统 locale 解码，建议在启动前 `chcp 65001` 或 `set PYTHONIOENCODING=utf-8` |
| `proc.terminate()` 行为 | Windows 上等同 SIGKILL，lark-cli 拿不到 graceful shutdown 机会。如果遇到 lark-cli event daemon 卡住，手动 `lark-cli event reset` 一下 |
| DRY_RUN 日志里的命令字符串 | 用的是 Unix shell 引号（`shlex.quote`），**直接复制到 cmd / PowerShell 跑不通**。只是日志展示问题，实际执行走 list-form `create_subprocess_exec`，正常 |
| lark-cli 二进制 | 用官方 Windows release，丢到 PATH 上即可。`LARK_CLI=lark-cli` 会自动找 `lark-cli.exe` |
| Claude Code CLI | `ClaudeSDKClient` 依赖部署机的 Claude Code。Windows 装好桌面 app 或 CLI 后 `claude auth login` 跑一次，SDK 自动复用 |

---

## 故障排查

| 现象 | 排查方向 |
|---|---|
| `consume_exited` 后立即退出 | lark-cli 未登录 / 应用没开"机器人"能力 / `im.message.receive_v1` 没在应用身份订阅里加 / `im:message.p2p_msg:readonly` 没勾在应用身份列 |
| `--as user is not supported` | 又把 `--as user` 用到了 event consume 上了，这事件只支持 bot |
| `send_ack_failed` 报权限 | bot 缺 `im:message:send` 应用身份 scope |
| `lark-cli docs +fetch` 失败 | (a) user 没登录 / 没勾 `docx:document:readonly`（用户身份） (b) 登录 user 对该文档没查看权限——让用户先把文档分享给你 |
| `warn_no_senders_configured` | `.env` 里 `COLLAB_SENDERS` 和 `HUMAN_SENDERS` 都没填，所有发件人都会被 `skip_untrusted_sender` 丢 |
| A 调工具后超时 | (a) B 没起 / (b) DRY_RUN=1（B 不会真回） / (c) `COLLAB_SENDERS` 没含 A 的 bot open_id |
| 答案过长被截 | B 自动按 ~28KB 字节裁 `answer` 字段并打 `truncated:true`；要完整内容打开原文档 |
| 同一份文档反复被拉 | 本期没做缓存。高频场景可在 B 侧加 docx_token → markdown 的本地 TTL 缓存 |

需要看更详细的事件载荷，把 `event_in` 那行的 `raw` 字段也打出来即可。

---

## 后续规划

- 文档拉取缓存层 —— 高频协作值得加
- 多 capability 工作流：让 router 支持多个 handler，比如 sheet QA、bitable 查询
- 文档起草：基于 lark-doc skill，把 envelope 里的需求草拟成文档
