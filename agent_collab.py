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
DM 里贴飞书 URL + 自然语言问题就行，答案作为纯文本回。复用同一套
fetch + Claude QA 管道，错误也用人话回。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import time
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
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

    lark-cli docs +fetch --api-version v2 支持文档 URL 或 token；--format markdown
    要求底层文档为 docx 类型（飞书新版文档）。失败时抛 RuntimeError，由上层翻译成
    error envelope 返回给 A。
    """
    cmd = [
        LARK_CLI, "docs", "+fetch",
        "--api-version", "v2",
        "--format", "markdown",
        doc_url_or_token,
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
    return stdout.decode(errors="replace")


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


# --- human mode (plain text in, plain text out) -----------------------------

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
# 粘 URL 时常见的尾巴标点；URL 里这些字符出现也少见，剔了对 fetch 更友好
URL_TRAILING_PUNCT = ".,;:)]}>'\"'。，；"

HUMAN_HELP_NO_URL = (
    "没看到飞书文档链接。把文档 URL 和问题一起发过来就行，比如：\n"
    "https://xxx.feishu.cn/docx/XXX 这文档怎么处理 OOM"
)
HUMAN_HELP_NO_QUESTION = (
    "贴了文档但没看到问题。再发一次时把问题也带上，比如：\n"
    "<URL> 这文档怎么处理 OOM"
)


def parse_human_request(text: str) -> tuple[str | None, str | None, str | None]:
    """把一段自然语言切成 (doc_url, question, error)。

    成功：返回 (url, question, None)。
    失败：返回 (None, None, 人话错误提示)，让上层直接发回 chat。
    """
    if not text or not text.strip():
        return None, None, "消息是空的，请贴文档链接 + 问题。"
    m = URL_RE.search(text)
    if not m:
        return None, None, HUMAN_HELP_NO_URL
    doc_url = m.group(0).rstrip(URL_TRAILING_PUNCT)
    # 把第一个 URL 抠掉（不动后面可能出现的 URL，全部留在 question 里
    # 让 Claude 自己判断要不要管），剩下的当问题。
    question = (text[: m.start()] + text[m.end():]).strip()
    if not question:
        return None, None, HUMAN_HELP_NO_QUESTION
    return doc_url, question, None


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


async def handle_human_doc_qa(evt: dict) -> None:
    """处理真人 DM 的 doc QA 请求。

    输入是自然语言（不是 envelope），输出也是自然语言。其它逻辑与 envelope
    版本一致：fetch_doc_as_markdown + answer_from_doc，超时按 40/60 切。
    """
    text = (evt.get("text") or "").strip()
    chat_id = evt.get("chat_id")
    if not chat_id:
        log("human_doc_qa_no_chat_id")
        return

    doc_url, question, err = parse_human_request(text)
    if err:
        log("human_doc_qa_bad_input", preview=text[:120])
        await send_text_reply(chat_id, err)
        return

    log("human_doc_qa_in", doc=doc_url, q_preview=question[:80])
    started = time.time()
    try:
        doc_md = await asyncio.wait_for(
            fetch_doc_as_markdown(doc_url),
            timeout=DOC_QA_TIMEOUT_SEC * 0.4,
        )
        log("doc_fetched", bytes=len(doc_md))
        answer = await asyncio.wait_for(
            answer_from_doc(question, doc_md),
            timeout=DOC_QA_TIMEOUT_SEC * 0.6,
        )
        await send_text_reply(chat_id, answer)
        log(
            "human_doc_qa_done",
            took_ms=int((time.time() - started) * 1000),
            answer_len=len(answer),
        )
    except asyncio.TimeoutError:
        await send_text_reply(
            chat_id,
            "⌛ 超时了：拉文档或推理时间过长（默认 55s）。文档可能太大，或者临时网络抖了一下，可以稍后重试。",
        )
        log("human_doc_qa_timeout", took_ms=int((time.time() - started) * 1000))
    except Exception as ex:  # noqa: BLE001
        await send_text_reply(
            chat_id,
            f"❌ 出错了：{type(ex).__name__}: {str(ex)[:200]}",
        )
        log("human_doc_qa_error", error=str(ex)[:300])
