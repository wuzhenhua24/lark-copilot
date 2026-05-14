# lark-copilot

个人飞书 copilot，基于 [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python) + [lark-cli](https://github.com/larksuite/lark-cli) 搭建。

> **模型说明**：claude-agent-sdk 在本项目里继承**部署机本地 Claude Code 的鉴权与模型配置**，
> 后端是一个固定的第三方 Claude 兼容模型。所以代码里所有 `ClaudeAgentOptions` 都不设 `model=`，
> 也没有 `CLASSIFY_MODEL` / `DOC_QA_MODEL` 这类按场景切换模型的环境变量 —— 加了也不会生效。
> 如果将来切到能直连 Anthropic 的部署，再考虑按场景挑模型。

当前已实现：

1. **DM 智能路由** —— 监听私聊消息，识别测试环境相关问题，自动回复引流到群聊机器人
2. **Agent-to-agent 文档协作** —— 给内网部署的 [ops-qa-bot](../ops-qa-bot) 当"飞书外设"，通过飞书 IM 接收结构化请求、拉飞书文档、跑 Claude 答题、把答案回传

规划中：文档起草、更多多 agent 工作流。

---

## 解决的痛点

负责测试环境维护时，常被同事 DM 问各种问题（"测试环境登不上""数据库连不上""怎么申请账号"……）。希望统一引导到「测试环境支持群」@ 机器人提问，但很多人习惯先私聊。

lark-copilot 做的事：

1. 监听你的飞书私聊
2. 用 Claude 判断是否是测试环境问题
3. 命中则自动回复一段引流文案（带群链接），并对同一个人 24h 内不再重复打扰
4. 闲聊、其他工作问题、不明确的消息一律放过

---

## 工作原理

### 数据流

```
┌─────────────────────────────────────────────────────────────────────┐
│                          飞书服务端                                  │
│                                                                     │
│   同事 DM 你 ──► im.message.receive_v1 事件                          │
└──────────┬──────────────────────────────────────────────▲───────────┘
           │ WebSocket 长连接 (lark-cli 持有 user token)    │ 发消息 API
           │                                                │
           ▼                                                │
┌─────────────────────────────────────────────────────────────────────┐
│                          本机                                        │
│                                                                     │
│  ┌──────────────────────────┐                                       │
│  │ lark-cli event consume   │ stdout NDJSON (每行一个事件)          │
│  │ im.message.receive_v1    │──────┐                                │
│  │ --as user                │      │                                │
│  └──────────────────────────┘      │                                │
│                                     ▼                                │
│  ┌─────────────────────────────────────────────────────────┐        │
│  │ router.py (asyncio 主循环)                              │        │
│  │                                                         │        │
│  │  ① stream_events()    读 stdout 每行                     │        │
│  │  ② extract_message()  解析 sender/chat_type/text         │        │
│  │  ③ handle()                                              │        │
│  │     ├─ 过滤：p2p? text? 白/黑名单? 冷却内?              │        │
│  │     ├─ classify()  ─► Claude API ─► TEST_ENV / OTHER     │        │
│  │     └─ 命中 → send_redirect()                            │        │
│  │                                                         │        │
│  │  状态：.state/cooldown.json (谁、什么时候回过)            │        │
│  └─────────────────────────────────────────────────────────┘        │
│                                     │                                │
│                                     ▼                                │
│  ┌──────────────────────────┐                                       │
│  │ lark-cli im              │ 子进程，注入 user token               │
│  │ +messages-send --as user │──────┐                                │
│  │ --chat-id X --text "..." │      │                                │
│  └──────────────────────────┘      │                                │
└─────────────────────────────────────┼───────────────────────────────┘
                                      │
                                      ▼ HTTPS (Feishu OpenAPI)
                                  消息送达对方
```

### 权限与身份

两套独立的凭证，router.py 本身不持有任何 token：

| 身份 | 谁持有 | 怎么获得 | 用途 |
|---|---|---|---|
| 飞书 user token | lark-cli 本地配置 | `lark-cli auth login --scope ...` 浏览器 OAuth | 收 DM、发 DM |
| Claude API key | claude-agent-sdk | `ANTHROPIC_API_KEY` 环境变量或 Claude Code 登录态 | 跑分类 |

- 所有飞书操作通过 `subprocess` 启动 lark-cli 子进程完成，token 由 lark-cli 保管。
- 以 `--as user`（你自己）身份收发消息，不是机器人 —— 对方看到的是「你」回了私聊。
- 事件流是 WebSocket 长连接，不需要暴露公网端口、不用配回调地址。

### 设计细节

- **冷却**：24h 内同一个人只回一次，记录在 `.state/cooldown.json`。
- **白/黑名单**：`ONLY_SENDERS` 灰度阶段只回特定 open_id；`SKIP_SENDERS` 永不回（老板、家人）。
- **分类边界**：prompt 里写明"宁可漏一个，不要误判朋友的私聊"，暧昧消息一律 OTHER。
- **DRY_RUN**：默认开启，分类照跑但不真的发消息，只 log 出待发送的命令，先观察几天再放开。
- **群聊忽略**：只处理 `chat_type == "p2p"`，群聊事件直接丢弃。

---

## 企业部署：飞书应用准备

本项目运行依赖 `lark-cli`，而 `lark-cli` 本身必须挂在一个**飞书自建应用**上 ——
应用提供 `app_id` / `app_secret` 和可申请的 scope 范围，`lark-cli auth login` 的
OAuth 授权也是走这个应用。所以在公司部署前，先把应用准备好。

### 层级关系

```
企业飞书应用 (app_id / app_secret + scope 白名单 + 事件订阅)
        │  提供 OAuth 容器
        ▼
   lark-cli (本地 config 持有 app credentials)
        │  浏览器 OAuth → 每个运行人各自的 user token
        ▼
   router.py / agent_collab.py (subprocess 调用 lark-cli, 不直接持 token)
```

注意身份关系：

- router.py 发消息走 `--as user`，对方看到的是「**你本人**」回的私聊，不是机器人
- 所以这个应用在用户视角是「幕后的 OAuth 容器」，不是 bot 实体
- agent_collab 协议里的 `COLLAB_SENDERS`（对端 bot open_id）是 **ops-qa-bot 那侧的应用**，
  跟本仓库挂的这个应用是两个不同的 app

### 在企业后台要做的事

1. 在企业飞书开发者后台创建一个**企业自建应用**（无需发布应用市场，可见范围给内部成员即可）
2. 在该应用的「权限管理」里勾选下列 scope。
   **关键：本项目所有飞书调用都走 `--as user`，所以全部勾在「用户身份权限」一列，
   「应用身份权限」一列不需要勾任何东西。**

   | scope | 身份列 | 谁用 | 用途 |
   |---|---|---|---|
   | `im:message` | 用户身份 | router.py | 订阅 `im.message.receive_v1`（收 DM） |
   | `im:message:send_as_user` | 用户身份 | router.py | 以 user 身份发 DM（scope 本身就是 user-only） |
   | `docx:document:readonly` | 用户身份 | agent_collab.py | `lark-cli docs +fetch` 拉飞书文档 |
   | `drive:drive:readonly` | 用户身份（可选） | agent_collab.py | 搜云空间 |
   | `contact:user.base:readonly` | 用户身份（可选） | router.py | 日志里把 open_id 解析成姓名 |

   为什么不勾应用身份：

   - 应用身份权限走 `tenant_access_token`，是「以 bot 名义操作」。本项目对方看到的
     是「你本人」回私聊，不是 bot 发的，全程用不到 tenant token
   - 应用身份能拉到的文档/联系人受限于「应用可见范围 + 文档主动分享给 bot」；
     用户身份直接复用你的飞书账号权限，覆盖面正好是项目想要的范围
   - 事件订阅 `im.message.receive_v1` 在应用身份下推的是「@ 机器人的消息」，
     在用户身份下推的才是「发给你本人的 DM」—— 我们要的是后者

3. 在「事件与回调」里：
   - 「网关」选 **长连接（WebSocket）** —— `lark-cli event consume` 走这条通道，
     不需要公网回调地址、不需要配 redirect URL
   - 「事件订阅」展开**「应用身份订阅」**列表，添加 `im.message.receive_v1`。
     ⚠️ 这个事件只能在「应用身份订阅」里加，「用户身份订阅」列表里**没有**它 ——
     这是平台对事件的归类，不代表事件按 bot 身份投递（见下方说明）
   - 添加事件后，平台会**强制要求**勾上一批**应用身份 scope**
     （如 `im:message.group_at_msg:readonly`、`im:message.p2p_msg:readonly`）。
     按提示勾上即可 —— 这只是事件注册的合法性声明，不影响运行时行为（详见下方"应用身份 scope 强制项"）
   - 另外，本项目用到的 `im:message`、`im:message:send_as_user` 等业务 scope，
     去权限管理把它们勾在**用户身份**那一列
4. 把 `app_id` / `app_secret` 配进部署机的 lark-cli 配置（见 lark-cli 文档的 `config init`）
5. 每个运行 router 的人，在自己的部署机上跑一次 `lark-cli auth login`，
   浏览器 OAuth 授权这个应用，拿到**属于自己的 user token**（存在 lark-cli 本地 config 里）

#### 为什么「事件订阅在应用身份」但「scope 在用户身份」

这是两件独立的事，容易混。心智模型：

| 层 | 决定什么 | 本项目设置 |
|---|---|---|
| **事件注册** | 应用是否监听某类事件 | 应用身份订阅 → 添加 `im.message.receive_v1` |
| **权限 scope** | 哪种身份的 token 能解开事件载荷 | 用户身份权限 → 勾 `im:message` 等 |
| **运行时投递** | 用什么 token 接长连接，决定看到谁的视角 | `lark-cli` 用 user token 接 WebSocket |

事件**注册**只在应用身份订阅里 —— 这是平台分类，没得选。但事件**投递**按接长连接的
token 身份过滤：

- 用 tenant token 接 → 收 bot 视角的（@bot、bot 所在群的消息）
- 用 user token 接 → 收**该用户视角**的（发给该用户的 DM、@该用户的消息）—— 这是本项目要的

所以「事件订阅在哪一列」和「scope 在哪一列」**不需要对齐**，按上表各自就位即可。

#### 应用身份 scope 强制项（注册事件时被平台拉进来的）

把 `im.message.receive_v1` 加到「应用身份订阅」后，平台会要求声明一批
**应用身份** scope，常见的：

- `im:message.group_at_msg:readonly` —— 读群里 @bot 的消息
- `im:message.p2p_msg:readonly` —— 读发给 bot 的 DM
- `im:resource`（若提示则需）—— 读消息中的富文本/附件资源

**按提示勾上即可，不会改变运行时行为**：

1. 这些 scope 是**事件注册的合法性声明**，不是运行时调用授权 ——
   平台规则是"既然订阅了这类事件，应用层就得声明能读这类消息"
2. 本项目代码**从不使用 `tenant_access_token`**（router.py / agent_collab.py 全程 `--as user`），
   所以这些 scope 即便授予，也没有 code path 去用
3. 真正决定运行时能拿到什么的，是「lark-cli 用 user token 接 WebSocket」+
   「用户身份 `im:message` scope」—— 这条链路不变

**连带影响**：这些 app-identity scope 通常**绑定在「机器人」能力上**，所以你可能需要
顺便把「应用能力 → 机器人」启用。bot 只是形式上的占位 —— 不用起名、不用拉进任何群、
不用任何人 DM 它。若想进一步降噪，可以把 bot 的"允许被搜索"、"允许被加入群聊"关掉。

**安全视角**：

| 担心 | 实际风险 |
|---|---|
| bot 拿到 @它的消息会被本项目处理吗 | 不会。代码从不用 tenant token 连 WebSocket，bot 视角的事件不会进 router |
| 攻击者能利用 bot 身份 scope 做事吗 | 需先拿到 `app_secret`。跟"勾不勾这些 scope"无关，是 secret 保管问题 |
| 用户身份 scope 会被悄悄扩大吗 | 不会。用户身份权限那列单独勾选，跟应用身份互不影响 |

### 应用配置一览（不是 scope，是开关）

除了上面的 scope 勾选，应用本身还要满足这些前提：

| 配置项 | 在哪里 | 应为 | 备注 |
|---|---|---|---|
| 应用启用 | 应用基础信息 | 启用 | 关掉的话什么都不通 |
| 可见范围 | 应用基础信息 → 可见范围 | 包含运行 router 的同事 | 不在范围内的人 OAuth 时会被拒 |
| 事件订阅网关 | 事件与回调 → 网关 | 长连接（WebSocket） | 不需要公网回调地址 |
| 订阅事件 | 事件与回调 → **应用身份订阅** | `im.message.receive_v1` | 用户身份订阅列表里没有此事件；按上文说明，注册位置和 scope 位置不需要对齐 |
| 用户身份权限 | 权限管理 → 用户身份权限 | 勾上前面列的 5 个 scope | 应用身份那列全空 |
| 版本发布 | 版本管理 | 改完 scope 要发新版并过管理员审核 | 容易忘 —— 没发版的话 OAuth 拿不到新 scope |

### 不需要做的事 / 容易踩的点

- 「机器人」能力**可能要顺手开**（因为订阅 `im.message.receive_v1` 会拉进若干
  app-identity scope，那些 scope 绑在 bot 能力上）—— 但它只是占位，运行时不被使用，
  代码不发 bot 消息也不接 bot @ 事件，详见上文"应用身份 scope 强制项"
- **不需要**配 redirect URL —— `lark-cli auth login` 用 device flow / 本地回调，不走 Web OAuth redirect
- **不需要**配「网页应用」「小程序」之类的能力
- 加完 scope **必须重新发版并过管理员审核**，否则新登录的用户拿不到新权限
- 已经登录过 lark-cli 的人，scope 变更后要 `lark-cli auth logout` + `lark-cli auth login`
  重走一次 OAuth，否则 token 里还是旧 scope

### 多人复用同一个应用

一般做法是「一个公共企业应用 + 每人各自 OAuth + lark-cli config 各存各的 token」。
应用本身共享，scope 在应用层一次配齐；token 是 per-user 的，互不影响。

---

## 快速开始

### 1. 准备环境

需要 Python ≥ 3.12 和 [uv](https://github.com/astral-sh/uv)：

```bash
# 在仓库根目录
uv sync
```

依赖：`claude-agent-sdk`、`python-dotenv`。

### 2. 安装并登录 lark-cli

前提：已按上文「[企业部署：飞书应用准备](#企业部署飞书应用准备)」准备好飞书应用、
lark-cli 本地已配好 `app_id` / `app_secret`。

按 [lark-cli 文档](https://github.com/larksuite/lark-cli) 安装后，授权所需 scope：

```bash
lark-cli auth login \
  --scope im:message \
  --scope im:message:send_as_user
# 可选：contact:user.base:readonly  —— 用于把 open_id 转成姓名打日志
```

所需 scope 说明：
- `im:message`：订阅 `im.message.receive_v1` 事件（收 DM）
- `im:message:send_as_user`：以你的身份发 DM
- `contact:user.base:readonly`（可选）：日志里把 open_id 解析为姓名

> 登录态保存在 lark-cli 自己的配置里，router.py 不接触。

### 3. 配置 .env

复制 `.env.example` 为 `.env`，按实际填写：

```bash
cp .env.example .env
```

关键字段：

| 字段 | 说明 |
|---|---|
| `GROUP_NAME` | 测试环境支持群的群名 |
| `GROUP_BOT_NAME` | 群里那个负责答疑的机器人名字 |
| `GROUP_LINK` | 群邀请链接（applink.feishu.cn 那种） |
| `DRY_RUN` | `1` 只 log 不发；`0` 真发。**首次跑保持 1。** |
| `COOLDOWN_HOURS` | 对同一个人的回复冷却，默认 24 |
| `ONLY_SENDERS` | 逗号分隔的 open_id 白名单，灰度阶段用 |
| `SKIP_SENDERS` | 逗号分隔的 open_id 黑名单 |
| `LARK_CLI` | lark-cli 可执行路径，默认 `lark-cli` |
| `STATE_FILE` | 冷却状态文件，默认 `.state/cooldown.json` |

### 4. 跑起来（先 dry-run）

```bash
uv run python router.py
```

输出 NDJSON 日志，每行一个事件。关键事件类型：

| event | 含义 |
|---|---|
| `starting` | 启动，附 lark-cli 命令和 dry_run 状态 |
| `event_in` | 收到一条事件 |
| `skip_*` | 被各种规则过滤（非 p2p、非 text、冷却中、黑名单等） |
| `classified` | 分类结果，含 sender、分类、原因、消息前 80 字 |
| `dry_run_send` | DRY_RUN 模式下要发的命令（实际没发） |
| `send_ok` / `send_failed` | 真实发送结果 |
| `handler_error` | 处理异常 |

### 5. 灰度策略（建议）

```
第 1-2 天  DRY_RUN=1, ONLY_SENDERS=<1-2 个常问你测试环境的同事>
           观察 classified 事件，看分类是否准
第 3-4 天  DRY_RUN=1, ONLY_SENDERS 去掉
           观察全量分类
第 5 天+   DRY_RUN=0
           真正开始自动回复
```

如果发现分类边界不对，改 `router.py` 里的 `SYSTEM_PROMPT` —— 已经举了一批正反例，按你的实际情况补充。

---

## 项目结构

```
lark-copilot/
├── README.md            本文件
├── pyproject.toml       依赖声明（uv 管理）
├── uv.lock              uv 锁文件
├── .env.example         配置模板（复制为 .env）
├── .env                 你的本地配置（.gitignore 忽略）
├── router.py            Capability 1：DM 路由主程序（含协作 sender 短路）
├── agent_collab.py      Capability 2：飞书文档协作处理器
└── .state/
    └── cooldown.json    冷却状态（运行后自动创建）
```

`router.py` 各部分：

| 函数 / 区块 | 作用 |
|---|---|
| `load_state` / `save_state` / `in_cooldown` / `mark_replied` | 冷却持久化 |
| `extract_message` | 解析 lark-cli 事件行，兼容 Feishu envelope 和扁平投影两种 shape |
| `classify` | 调 Claude API，输出 `{"class": ..., "reason": ...}` 单行 JSON |
| `send_redirect` | 调 lark-cli 发送引流回复 |
| `stream_events` | 起 `lark-cli event consume` 子进程，异步读 stdout |
| `handle` | 单条事件的过滤 + 分类 + 回复流程 |
| `main` | 主循环 |

---

## Capability 2：Agent-to-agent 文档协作（agent_collab.py）

### 为什么需要这层

`ops-qa-bot`（内网运维问答机器人）部署在内网，bot 身份只有 tenant scope，
**没有 user-scope 的飞书文档读取能力**。但用户的提问里经常贴飞书文档链接。

lark-copilot 跑在能访问飞书文档的机器上、以 **user 身份**接 DM，正好补这块。
两个 agent 通过飞书 IM 互发结构化 envelope 完成协作 —— 零新增网络通道、
零端口暴露。

### 链路

```
                        ┌────────────────────────────────┐
                        │ 用户在飞书群里 @ops-qa-bot 提问  │
                        │ "这份 feishu doc 怎么处理 OOM？"│
                        └──────────────┬─────────────────┘
                                       │
                                       ▼
                  ┌─────────────────────────────────────┐
A (ops-qa-bot, 内网)   │ Claude agent 决定调用 ask_feishu_doc  │
                  │ → 发结构化 DM 给 lark-copilot 的 user │
                  └──────────────┬──────────────────────┘
                                 │  飞书 IM (text, JSON envelope)
                                 │  {"op":"doc_qa","req_id":"...","doc":"...","q":"..."}
                                 ▼
B (lark-copilot,   ┌──────────────────────────────────────────┐
   有飞书出口)     │ router.py 短路：sender ∈ COLLAB_SENDERS  │
                  │ → agent_collab.handle_doc_qa(evt)        │
                  │   ├─ lark-cli docs +fetch → Markdown     │
                  │   ├─ Claude 单轮 Q&A (DOC_QA_MODEL)      │
                  │   └─ DM 回 ack envelope                  │
                  └────────────────┬─────────────────────────┘
                                   │  ack envelope
                                   ▼
A: rpc.try_deliver 命中 → 唤醒 await 的 Future → tool 返回 answer → agent 整合答案
```

### Wire protocol

请求（A → B，飞书 text DM）：

```json
{"op":"doc_qa","req_id":"<uuid12>","doc":"<feishu-url>","q":"<question>"}
```

回复（B → A）：

```json
{"op":"doc_qa_ack","req_id":"<same>","ok":true,"answer":"..."}
```

或错误：

```json
{"op":"doc_qa_ack","req_id":"<same>","ok":false,"error":"timeout|...|..."}
```

### B 端配置（本仓库）

`.env` 新增三个字段：

| 字段 | 含义 |
|---|---|
| `COLLAB_SENDERS` | 受信任的 peer open_id（ops-qa-bot 的飞书 bot open_id，注意是 bot 不是你自己）。逗号分隔可多个 |
| `DOC_QA_TIMEOUT_SEC` | 一次 doc_qa 的总超时（拉文档 + 推理），默认 55s |

需要的额外 lark-cli scope：
- `docx:document:readonly` —— 让 `lark-cli docs +fetch` 能拉文档
- `drive:drive:readonly`（可选） —— 搜云空间

### A 端配置（ops-qa-bot 仓库）

环境变量 `FEISHU_DOC_PEER_OPEN_ID` 设为 **本仓库（lark-copilot）的 user open_id**
就启用 `ask_feishu_doc` 工具；不设置时 A 的行为完全不变。

代码改动：
- `ops_qa_bot/feishu_doc_tool.py` —— 新增。SDK MCP server + `FeishuDocRPC` 关联回复
- `ops_qa_bot/bot.py` —— `OpsQABot` 接受 `extra_mcp_servers` / `extra_tool_names`
- `ops_qa_bot/feishu_core.py` —— `SessionManager` 透传 bot extras；`FeishuClient` 加 `send_text_to_open_id`
- `ops_qa_bot/ws_server.py` —— 启动时 `init_rpc`、`_on_message` 里短路 peer 回程
- `ops_qa_bot/prompt.py` —— 加「飞书文档协作」节，教 agent 何时调工具、怎么标注来源

### 部署顺序（首次接通）

两侧都跑起来后才能 round-trip。建议步骤：

```
1. B 侧：lark-cli auth login（含 docx scope），dry-run 跑一次 router.py 看自己 DM 自己能不能收到
2. B 侧：拿到 ops-qa-bot 的 bot open_id（飞书后台 → 应用 → 凭证），填到 COLLAB_SENDERS
3. A 侧：拿到 lark-copilot 的 user open_id（你自己），填到 FEISHU_DOC_PEER_OPEN_ID
4. 起 B 的 router.py（DRY_RUN=1 先看 log）
5. 起 A 的 ws_server（已带 ask_feishu_doc）
6. 群里 @ A "请基于这份飞书文档 <url> 总结 OOM 处置流程"
   观察：
   - A 的 log 应出现 `feishu_doc_rpc out: req_id=...`
   - B 的 log 应出现 `collab_in` → `doc_qa_in` → `doc_fetched` → `doc_qa_done`（或 `dry_run_send_ack`）
   - A 的 log 应出现 `feishu_doc_rpc ack: req_id=... ok=True`
7. DRY_RUN=0 放开 B 端真发，端到端联通
```

### 故障排查（协作通道）

| 现象 | 排查方向 |
|---|---|
| A 调工具后超时 | (a) B 没起 / `COLLAB_SENDERS` 没配 A 的 bot open_id (b) DRY_RUN=1（B 不会真回） (c) lark-cli 没装 `docx` scope |
| B 收到 envelope 但 `lark-cli docs +fetch` 失败 | 飞书 user 没有该文档的查看权限；让用户先把文档分享给你 |
| B 的 ack 回复了但 A 当成新提问处理 | A 的 `FEISHU_DOC_PEER_OPEN_ID` 没填或填错（应是 lark-copilot 的 user open_id） |
| 同一个文档反复被拉 | 本期没做缓存。若高频可在 B 侧加 docx_token → markdown 的本地缓存（TTL ~10min） |
| 答案超长被截断 | B 自动按 28KB 截 `answer` 字段并打 `truncated:true`；想要完整内容直接打开原文档 |
| 我手动给 A 的 bot 发消息没反应 | 协议是 try_deliver-then-fallthrough：非 ack 文本会作为正常提问走 QA 流程。如果还是不回，看 A 端日志 `peer text not an ack, treat as manual QA input` 是否出现 |

---

## 后续规划

- 文档起草：基于 lark-doc skill，把 DM 里的需求草拟成文档
- 多 agent 工作流：如会议纪要 → OKR 进展 → 周报自动串联
- 更细的路由策略：按对方部门 / 历史交互分流到不同的群或回复模板
- 文档拉取缓存层 —— 高频协作场景值得加

---

## 故障排查

| 现象 | 排查方向 |
|---|---|
| `consume_exited` 后立即退出 | lark-cli 未登录 或 scope 不足，重跑 `lark-cli auth login` |
| `send_failed` 报 permission denied | 缺 `im:message:send_as_user` scope |
| 分类全是 `parse_fail` | 模型输出非 JSON，检查 `CLASSIFY_MODEL` 是否可用、prompt 是否被改坏 |
| 一直没事件 | 给自己发条 DM 测试；确认 lark-cli 当前账号就是你常用账号（不是 bot 身份） |
| `GROUP_LINK` warning | `.env` 里 `GROUP_LINK` 为空，回复文本里链接会缺失 |

需要更详细的事件 shape，把 `event_in` 那行的 `raw` 字段也打出来看一眼即可。
