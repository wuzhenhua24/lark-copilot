"""
lark-copilot · agent_collab

作为 ops-qa-bot 的"飞书外设"——doc QA worker。

场景：ops-qa-bot 部署在内网，bot 身份没有 user-scope 的飞书文档访问能力；本进程
跑在能访问飞书文档的部署机上，登录 user 持有 docx 读权限。peer agent
（ops-qa-bot）给本仓库的 **lark-copilot bot** 发一条结构化 DM envelope，本进程
用 user OAuth 拉飞书文档、跑 Claude 回答、再用 bot 身份发 ack 回去。

身份分工（同进程内 per-command 切换）：
- 监听事件：`event consume im.message.receive_v1 --as bot`（在 router.py 里）
- 拉文档：`docs +fetch --as user`（user OAuth 持有文档读权限）
- 发 ack：`im +messages-send --as bot`（事件是 bot 收到的，回复也由 bot 发）

为什么用飞书 IM 当传输：
- ops-qa-bot 内网，对外网络出口受限，飞书 IM 是它已经走通的唯一双向通道
- 双方都已经接通飞书，零网络改造
- 鉴权天然：发件人 open_id 就是身份，router 按 sender 白名单过滤

Envelope（单行 JSON 文本消息）:
    请求: {"op":"doc_qa","req_id":"<correlation-id>","doc":"<feishu-url>","q":"<question>"}
    回复: {"op":"doc_qa_ack","req_id":"<same>","ok":true,"answer":"..."}
       或 {"op":"doc_qa_ack","req_id":"<same>","ok":false,"error":"..."}

另外暴露一条 `handle_human_doc_qa`：给 HUMAN_SENDERS 白名单里的真人用户用，
DM 里贴飞书 URL + 自然语言问题就行，答案作为纯文本回。**支持多轮**：
- 每个 chat_id 维护一个 `ChatSession`（一个 ClaudeSDKClient + 文档缓存）
- 后续追问可以省 URL，会复用之前缓存的文档
- 单条消息可以同时贴多个 URL
- `/reset` / `/new` / `重置` / `清空` 任一可清空当前对话
- `SESSION_TTL_MIN`（默认 30 分钟）无活动自动过期
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import time
from dataclasses import dataclass, field, replace
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
    query,
)

LARK_CLI = os.environ.get("LARK_CLI", "lark-cli")
DOC_QA_TIMEOUT_SEC = float(os.environ.get("DOC_QA_TIMEOUT_SEC", "55"))
DRY_RUN = os.environ.get("DRY_RUN", "1") == "1"

# 飞书文本 DM 上限约 30 KB。留 ~2 KB 给 envelope 外壳和 UTF-8 多字节膨胀，
# 整条超限会被飞书拒收或截断送达——后者更糟（A 端收到一坨破损 JSON，
# 一直等到 Future 超时）。所以本地预先按字节截 answer 字段，保证发出去
# 一定是合法 envelope，并加 truncated:true 让 A 端能识别。
MAX_ACK_BYTES = 28000
TRUNCATE_SUFFIX = "\n\n…（答案过长，已截断；建议直接打开原文档查阅完整内容）"

DOC_QA_SYSTEM_PROMPT = """你是文档问答助手。下面会给你一段飞书文档的 Markdown 全文和一个问题。

规则：
- 只基于提供的文档回答；文档里没有的信息直接说"文档里没找到"，不要凭常识补
- 答得简洁，3-5 句话覆盖核心；需要步骤就用编号列表
- 如果问题跟文档明显无关，直接说明并停止
- 不要把整段文档抄回来当作答案
"""


def log(event: str, **fields: object) -> None:
    rec = {"ts": int(time.time()), "event": event, **fields}
    print(json.dumps(rec, ensure_ascii=False), flush=True)


# --- envelope parsing --------------------------------------------------------

def parse_envelope(text: str) -> dict | None:
    """text 是否一条合法的请求 envelope。不是就返回 None（不当协作消息处理）。"""
    s = (text or "").strip()
    if not s or not s.startswith("{"):
        return None
    try:
        obj = json.loads(s)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("op") != "doc_qa":
        return None
    if not isinstance(obj.get("req_id"), str):
        return None
    if not isinstance(obj.get("doc"), str) or not isinstance(obj.get("q"), str):
        return None
    return obj


# --- doc fetch via lark-cli --------------------------------------------------

async def fetch_doc_as_markdown(doc_url_or_token: str) -> str:
    """调 lark-cli 把飞书文档导出成 Markdown。

    lark-cli flag schema：
    - --doc <url|token>     文档 URL 或 token（必须 flag 形式，不接受位置参数）
    - --doc-format markdown 文档内容格式（v2 才有，控制实际文档内容；不要跟 --format 混了）
    - --format json         CLI 输出包装层格式，默认就是 json
    - --as user             docx 读权限挂在 user OAuth 上，bot 身份没权限

    v2 输出是 JSON 信封：`.data.document.content` 才是 markdown 正文。
    失败抛 RuntimeError，由上层翻译成 error envelope。
    """
    cmd = [
        LARK_CLI, "docs", "+fetch",
        "--api-version", "v2",
        "--doc", doc_url_or_token,
        "--doc-format", "markdown",
        "--as", "user",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"lark-cli docs +fetch rc={proc.returncode}: "
            f"{stderr.decode(errors='replace')[:300]}"
        )
    try:
        payload = json.loads(stdout.decode(errors="replace"))
        content = payload["data"]["document"]["content"]
    except (json.JSONDecodeError, KeyError, TypeError) as ex:
        raise RuntimeError(
            f"lark-cli docs +fetch 返回结构异常：{type(ex).__name__}: "
            f"{stdout.decode(errors='replace')[:200]}"
        ) from ex
    return content


# --- doc QA via Claude -------------------------------------------------------

async def answer_from_doc(question: str, doc_md: str) -> str:
    """把 Markdown 全文塞进 user message 让 Claude 答。

    单轮 query：不开任何工具（避免 agent 跑去执行别的，doc 已经在 prompt 里了）。
    不指定 model：部署机的 Claude Code 已绑定固定第三方模型，SDK 按它的配置走。
    """
    options = ClaudeAgentOptions(
        system_prompt=DOC_QA_SYSTEM_PROMPT,
        allowed_tools=[],
        max_turns=1,
    )
    user_msg = (
        f"<document>\n{doc_md}\n</document>\n\n"
        f"<question>\n{question}\n</question>"
    )
    out_parts: list[str] = []
    async for msg in query(prompt=user_msg, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    out_parts.append(block.text)
    answer = "".join(out_parts).strip()
    return answer or "（模型未输出文本）"


# --- reply via lark-cli ------------------------------------------------------

def _truncate_answer_if_needed(payload: dict[str, Any]) -> dict[str, Any]:
    """超过 MAX_ACK_BYTES 时按字节截 answer 并打 truncated:true 标。

    只对带 answer 字段（成功响应）做处理；ok:false 的错误 ack 体积自然很小，
    不会触发。截断按 UTF-8 字节算（不是字符）——飞书是按字节限的，按字符截
    在多字节字符场景下不准。errors='ignore' 解码兜底"刚好截在 UTF-8 多字节
    中间"的边界情况，不会抛异常。
    """
    text = json.dumps(payload, ensure_ascii=False)
    if len(text.encode("utf-8")) <= MAX_ACK_BYTES:
        return payload
    answer = payload.get("answer")
    if not isinstance(answer, str):
        return payload  # 没法安全截非 answer 字段，原样发出去让飞书去拒

    # 用"空 answer 的骨架"度量给 answer 留出的预算：req_id / ok / truncated 等
    # 字段加上 JSON 外壳的实际字节数，再扣掉 suffix 自身。
    skeleton = {**payload, "answer": "", "truncated": True}
    skeleton_bytes = len(json.dumps(skeleton, ensure_ascii=False).encode("utf-8"))
    suffix_bytes = len(TRUNCATE_SUFFIX.encode("utf-8"))
    budget = MAX_ACK_BYTES - skeleton_bytes - suffix_bytes - 32  # 32 字节安全冗余
    if budget < 1024:
        # 极端：req_id / error 本身就把空间挤光了。退化到只发个明确的截断标记。
        budget = 1024

    encoded = answer.encode("utf-8")[:budget]
    truncated_answer = encoded.decode("utf-8", errors="ignore") + TRUNCATE_SUFFIX
    new_payload = {**payload, "answer": truncated_answer, "truncated": True}
    log(
        "doc_qa_truncated",
        req_id=payload.get("req_id"),
        original_bytes=len(answer.encode("utf-8")),
        kept_bytes=len(truncated_answer.encode("utf-8")),
    )
    return new_payload


async def send_envelope_reply(chat_id: str, payload: dict[str, Any]) -> None:
    """把 ack envelope 序列化成一条文本 DM 发回原 chat。

    DRY_RUN=1 时只打日志不发，方便单机灰度。
    """
    payload = _truncate_answer_if_needed(payload)
    text = json.dumps(payload, ensure_ascii=False)
    cmd = [
        LARK_CLI, "im", "+messages-send",
        "--as", "bot",
        "--chat-id", chat_id,
        "--text", text,
    ]
    if DRY_RUN:
        log("dry_run_send_ack", chat_id=chat_id, cmd=" ".join(shlex.quote(c) for c in cmd))
        return
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log(
            "send_ack_failed",
            chat_id=chat_id,
            rc=proc.returncode,
            stderr=stderr.decode(errors="replace")[:300],
        )
    else:
        log("send_ack_ok", chat_id=chat_id, req_id=payload.get("req_id"))


# --- orchestrator ------------------------------------------------------------

async def handle_doc_qa(evt: dict) -> None:
    """处理来自 ops-qa-bot 的一条 doc_qa 请求。

    evt 形状跟 router.extract_message 一致：含 chat_id / sender_id / text / 等。
    任一步抛异常都翻译成 error envelope 回给 A，避免 A 那边干等超时。
    """
    text = (evt.get("text") or "").strip()
    env = parse_envelope(text)
    if env is None:
        log("doc_qa_bad_envelope", sender=evt.get("sender_id"), preview=text[:120])
        return
    req_id = env["req_id"]
    doc = env["doc"]
    question = env["q"]
    chat_id = evt.get("chat_id")
    log("doc_qa_in", req_id=req_id, doc=doc, q_preview=question[:80])

    if not chat_id:
        log("doc_qa_no_chat_id", req_id=req_id)
        return

    started = time.time()
    try:
        doc_md = await asyncio.wait_for(
            fetch_doc_as_markdown(doc), timeout=DOC_QA_TIMEOUT_SEC * 0.4
        )
        log("doc_fetched", req_id=req_id, bytes=len(doc_md))
        answer = await asyncio.wait_for(
            answer_from_doc(question, doc_md),
            timeout=DOC_QA_TIMEOUT_SEC * 0.6,
        )
        await send_envelope_reply(
            chat_id,
            {"op": "doc_qa_ack", "req_id": req_id, "ok": True, "answer": answer},
        )
        log(
            "doc_qa_done",
            req_id=req_id,
            took_ms=int((time.time() - started) * 1000),
            answer_len=len(answer),
        )
    except asyncio.TimeoutError:
        await send_envelope_reply(
            chat_id,
            {"op": "doc_qa_ack", "req_id": req_id, "ok": False, "error": "timeout"},
        )
        log("doc_qa_timeout", req_id=req_id, took_ms=int((time.time() - started) * 1000))
    except Exception as ex:  # noqa: BLE001
        await send_envelope_reply(
            chat_id,
            {
                "op": "doc_qa_ack",
                "req_id": req_id,
                "ok": False,
                "error": f"{type(ex).__name__}: {ex}"[:300],
            },
        )
        log("doc_qa_error", req_id=req_id, error=str(ex)[:300])


# --- human mode (plain text in, plain text out, multi-turn) -----------------

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
# 粘 URL 时常见的尾巴标点；URL 里这些字符出现也少见，剔了对 fetch 更友好
URL_TRAILING_PUNCT = ".,;:)]}>'\"'。，；"

# 任一作为单独消息（trim 后等于命令本身）触发会话重置
RESET_COMMANDS = {"/reset", "/new", "重置", "清空"}

SESSION_TTL_SEC = float(os.environ.get("SESSION_TTL_MIN", "30")) * 60

HUMAN_SYSTEM_PROMPT = """你是飞书文档问答助手。用户会陆续给你一篇或多篇飞书文档（以 <document url="..."> 块的形式提供），并基于这些文档跟你多轮提问。

规则：
- 只基于已提供的文档回答；文档里没有的信息直接说"文档里没找到"，不要凭常识补
- 用户的追问通常仍然指当前已提供的文档；除非问题明显跑题，否则按这些文档作答
- 答得简洁。需要步骤就用编号列表
- 不要把整段文档抄回来当作答案
- 同时引用多篇文档时，注明引用了哪一篇（用 url 或简短的来源标识）
"""

# 新会话还没收到任何文档时用这个；轻量友好，鼓励但不强求贴文档。
# 一旦后续真有 <document> 进对话历史，模型也能自然切换到基于文档作答。
CHITCHAT_SYSTEM_PROMPT = """你是一个轻量的飞书机器人助手。主业是飞书文档问答，但用户也可以随便聊两句。

规则：
- 没有 <document> 块时：友好、简短地回应即可。合适时可以提一句"也可以贴飞书文档链接让我读后再问"，但别每条都说
- 出现 <document url="..."> 块后：切到严格文档问答——只基于文档作答，文档没提的直接说"文档里没找到"
- 答得简洁，避免长篇大论
"""


def extract_urls(text: str) -> list[str]:
    """从文本里抠出所有 URL，剔掉常见尾巴标点，保持原顺序、去重。"""
    seen: set[str] = set()
    out: list[str] = []
    for m in URL_RE.finditer(text or ""):
        url = m.group(0).rstrip(URL_TRAILING_PUNCT)
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def strip_urls(text: str) -> str:
    """把所有 URL 从文本里抠掉，留剩余文字。"""
    return URL_RE.sub("", text or "").strip()


# --- per-chat session state --------------------------------------------------

@dataclass
class ChatSession:
    chat_id: str
    client: ClaudeSDKClient | None              # 真正第一次问问题时才 spawn
    doc_cache: dict[str, str] = field(default_factory=dict)         # url -> markdown
    docs_in_client_context: set[str] = field(default_factory=set)   # 已塞进对话历史的 url
    last_active: float = field(default_factory=time.time)

    def is_idle(self, ttl_sec: float) -> bool:
        return (time.time() - self.last_active) > ttl_sec

    def touch(self) -> None:
        self.last_active = time.time()


class SessionManager:
    """per-chat 多轮会话状态。

    并发模型：
    - 每个 chat_id 一把 asyncio.Lock，所有访问该 chat 的代码都得拿它。
    - 不同 chat 的请求真正并行（每个 chat 独立 ClaudeSDKClient，互不阻塞）。
    - 同一 chat 的连续两条消息串行，避免对话历史交错。

    生命周期：
    - 创建：第一次有 user 发消息时（仅加载文档不开 client）
    - 触发 client spawn：用户第一次真正问问题
    - 销毁：用户发 /reset 等命令、或 lazy idle eviction
    - 进程退出：subprocess 由 OS 兜底回收；本期不做优雅 shutdown
    """

    def __init__(self, options: ClaudeAgentOptions, ttl_sec: float):
        self._options = options
        self._ttl = ttl_sec
        self._sessions: dict[str, ChatSession] = {}
        self._chat_locks: dict[str, asyncio.Lock] = {}
        self._dict_lock = asyncio.Lock()

    async def lock_for(self, chat_id: str) -> asyncio.Lock:
        async with self._dict_lock:
            lock = self._chat_locks.get(chat_id)
            if lock is None:
                lock = asyncio.Lock()
                self._chat_locks[chat_id] = lock
            return lock

    async def get_or_create(self, chat_id: str) -> ChatSession:
        """返回 chat_id 的 session；不存在或已过期就新建。

        调用方**必须**先持有 lock_for(chat_id)。
        """
        sess = self._sessions.get(chat_id)
        if sess and sess.is_idle(self._ttl):
            log("session_evict_idle", chat_id=chat_id)
            await self._destroy_locked(chat_id)
            sess = None
        if sess is None:
            sess = ChatSession(chat_id=chat_id, client=None)
            self._sessions[chat_id] = sess
            log("session_created", chat_id=chat_id)
        return sess

    async def ensure_client(
        self, sess: ChatSession, system_prompt: str | None = None,
    ) -> ClaudeSDKClient:
        """第一次真要 query 时再 spawn Claude Code 子进程。

        system_prompt 为 None 时用 SessionManager 自带的默认 options；传值则覆盖
        system_prompt 字段。只在 spawn 那一刻生效，已有 client 的 session 不会重建。
        """
        if sess.client is None:
            opts = self._options
            if system_prompt is not None and system_prompt != opts.system_prompt:
                opts = replace(opts, system_prompt=system_prompt)
            client = ClaudeSDKClient(options=opts)
            await client.connect()
            sess.client = client
            log("session_client_spawned", chat_id=sess.chat_id)
        return sess.client

    async def reset(self, chat_id: str) -> bool:
        """显式销毁。调用方持有 lock_for(chat_id)。返回是否真的销毁了。"""
        return await self._destroy_locked(chat_id)

    async def _destroy_locked(self, chat_id: str) -> bool:
        sess = self._sessions.pop(chat_id, None)
        if sess is None:
            return False
        if sess.client is not None:
            try:
                await sess.client.disconnect()
            except Exception as ex:  # noqa: BLE001
                log("session_client_disconnect_error", chat_id=chat_id, error=str(ex)[:200])
        return True


# 模块级单例，第一次访问才初始化（避免 import 时就读 env / 初始化 SDK options）
_session_manager: SessionManager | None = None


def _get_session_manager() -> SessionManager:
    global _session_manager
    if _session_manager is None:
        options = ClaudeAgentOptions(
            system_prompt=HUMAN_SYSTEM_PROMPT,
            allowed_tools=[],
            max_turns=1,
        )
        _session_manager = SessionManager(options=options, ttl_sec=SESSION_TTL_SEC)
    return _session_manager


async def send_text_reply(chat_id: str, text: str) -> None:
    """以 bot 身份发一条纯文本 DM。"""
    cmd = [
        LARK_CLI, "im", "+messages-send",
        "--as", "bot",
        "--chat-id", chat_id,
        "--text", text,
    ]
    if DRY_RUN:
        log("dry_run_send_text", chat_id=chat_id, cmd=" ".join(shlex.quote(c) for c in cmd))
        return
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log(
            "send_text_failed",
            chat_id=chat_id,
            rc=proc.returncode,
            stderr=stderr.decode(errors="replace")[:300],
        )
    else:
        log("send_text_ok", chat_id=chat_id)


async def _run_client_turn(client: ClaudeSDKClient, user_msg: str) -> str:
    """跑一轮：query 一段 user message + 收一轮 assistant 回复，拼成纯文本。"""
    await client.query(user_msg)
    out: list[str] = []
    async for msg in client.receive_response():
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    out.append(block.text)
    return "".join(out).strip()


async def handle_human_doc_qa(evt: dict) -> None:
    """处理真人 DM 的多轮 doc QA。

    输入：自然语言（可贴 0-N 个飞书 URL + 可选问题）。
    输出：纯文本答案 / 加载确认 / 错误提示。

    流程（持有 per-chat 锁内）：
    1. 文本是重置命令 → reset session、回确认
    2. 抠出新 URL（不在缓存里的），逐个 fetch 进缓存
    3. 没问题、只加载文档 → 回确认就停（client 不开）
    4. 有问题 → 确保 client 已 spawn，把"还没塞进对话历史的文档 + 问题"作为一轮发出去
    5. touch session 续命

    `/reset`、`/new`、`重置`、`清空` 任一作为单独消息时清空当前对话。
    超过 SESSION_TTL_MIN 分钟没有活动会被 lazy evict。
    """
    text = (evt.get("text") or "").strip()
    chat_id = evt.get("chat_id")
    if not chat_id:
        log("human_doc_qa_no_chat_id")
        return

    mgr = _get_session_manager()
    lock = await mgr.lock_for(chat_id)
    async with lock:
        if text in RESET_COMMANDS:
            ok = await mgr.reset(chat_id)
            await send_text_reply(
                chat_id,
                "✅ 已重置对话。下次贴一个飞书文档链接 + 问题开始新一轮。"
                if ok else
                "目前没有活跃对话可以重置。直接贴文档 + 问题就行。",
            )
            log("human_doc_qa_reset", chat_id=chat_id, had_session=ok)
            return

        sess = await mgr.get_or_create(chat_id)
        urls = extract_urls(text)
        new_urls = [u for u in urls if u not in sess.doc_cache]
        question = strip_urls(text)

        if not urls and not question:
            await send_text_reply(chat_id, "消息是空的。请把问题（和文档链接）发过来。")
            return

        # 拉新文档
        if new_urls:
            log("human_doc_qa_fetch", chat_id=chat_id, count=len(new_urls))
            for url in new_urls:
                try:
                    md = await asyncio.wait_for(
                        fetch_doc_as_markdown(url),
                        timeout=DOC_QA_TIMEOUT_SEC * 0.4,
                    )
                except asyncio.TimeoutError:
                    await send_text_reply(chat_id, f"⌛ 拉文档超时：{url}")
                    log("human_doc_qa_fetch_timeout", chat_id=chat_id, url=url)
                    return
                except Exception as ex:  # noqa: BLE001
                    await send_text_reply(
                        chat_id,
                        f"❌ 拉文档失败：{url}\n{type(ex).__name__}: {str(ex)[:200]}",
                    )
                    log("human_doc_qa_fetch_error", chat_id=chat_id, url=url, error=str(ex)[:300])
                    return
                sess.doc_cache[url] = md
            sess.touch()

        # 只加载文档没问题 → 确认即停（不浪费一轮 Claude 推理）
        if not question:
            listing = "\n".join(f"- {u}" for u in new_urls)
            await send_text_reply(
                chat_id,
                f"📄 已加载 {len(new_urls)} 篇文档：\n{listing}\n\n继续把问题发过来即可。",
            )
            log("human_doc_qa_docs_loaded", chat_id=chat_id, loaded=len(new_urls), total=len(sess.doc_cache))
            return

        # 有问题 → 跑 Claude。spawn 时按当前是否已有文档挑 prompt：
        # 已有文档（包括本轮刚拉的）→ 走严格 doc QA；纯闲聊 → 轻量 chitchat。
        # 已 spawn 的 client 不会换 prompt，但两个 prompt 都能 handle 后续 <document>。
        spawn_prompt = HUMAN_SYSTEM_PROMPT if sess.doc_cache else CHITCHAT_SYSTEM_PROMPT
        try:
            client = await mgr.ensure_client(sess, system_prompt=spawn_prompt)
        except Exception as ex:  # noqa: BLE001
            await send_text_reply(
                chat_id,
                f"❌ 启动模型失败：{type(ex).__name__}: {str(ex)[:200]}",
            )
            log("session_client_spawn_failed", chat_id=chat_id, error=str(ex)[:300])
            return

        # 这次需要补发哪些文档到对话历史里
        to_inject = [u for u in sess.doc_cache if u not in sess.docs_in_client_context]
        if sess.doc_cache:
            parts: list[str] = []
            for url in to_inject:
                parts.append(f'<document url="{url}">\n{sess.doc_cache[url]}\n</document>')
            parts.append(f"<question>\n{question}\n</question>")
            user_msg = "\n\n".join(parts)
        else:
            # 闲聊模式：没文档可注入，直接送原文，避免 <question> 包裹让模型误判语境
            user_msg = question

        log(
            "human_doc_qa_in",
            chat_id=chat_id,
            q_preview=question[:80],
            inject_docs=len(to_inject),
            total_docs=len(sess.doc_cache),
            mode="doc_qa" if sess.doc_cache else "chitchat",
        )
        started = time.time()
        try:
            answer = await asyncio.wait_for(
                _run_client_turn(client, user_msg),
                timeout=DOC_QA_TIMEOUT_SEC * 0.6,
            )
        except asyncio.TimeoutError:
            await send_text_reply(
                chat_id,
                "⌛ 推理超时。文档可能太长，或者临时网络抖了一下。可以试着 /reset 重开或简化问题。",
            )
            log("human_doc_qa_timeout", chat_id=chat_id, took_ms=int((time.time() - started) * 1000))
            return
        except Exception as ex:  # noqa: BLE001
            await send_text_reply(chat_id, f"❌ 模型出错：{type(ex).__name__}: {str(ex)[:200]}")
            log("human_doc_qa_error", chat_id=chat_id, error=str(ex)[:300])
            return

        for url in to_inject:
            sess.docs_in_client_context.add(url)
        sess.touch()

        await send_text_reply(chat_id, answer or "（模型未输出文本）")
        log(
            "human_doc_qa_done",
            chat_id=chat_id,
            took_ms=int((time.time() - started) * 1000),
            answer_len=len(answer),
        )
