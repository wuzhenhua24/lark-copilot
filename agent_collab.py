"""
lark-copilot · agent_collab

第二个 capability：作为 ops-qa-bot 的"飞书外设"。

场景：ops-qa-bot 部署在内网，没有 user-scope 的飞书文档访问能力；本进程
跑在能访问飞书文档的机器上，对外以 user 身份接 DM。约定的"协作机器人"
ops-qa-bot 给我发一条结构化 DM（JSON envelope），我去飞书拉文档、跑 Claude
回答、把答案再发回去。

为什么用飞书 IM 当传输：
- ops-qa-bot 内网，对外网络出口受限，飞书 IM 是它已经走通的唯一双向通道
- 双方都已经接通飞书，零网络改造
- 鉴权天然：发件人 open_id 就是身份，路由层按 sender 短路

Envelope（单行 JSON 文本消息）:
    请求: {"op":"doc_qa","req_id":"<correlation-id>","doc":"<feishu-url>","q":"<question>"}
    回复: {"op":"doc_qa_ack","req_id":"<same>","ok":true,"answer":"..."}
       或 {"op":"doc_qa_ack","req_id":"<same>","ok":false,"error":"..."}

router.py 在 `handle()` 入口看到 sender == OPS_QA_BOT_OPEN_ID 时短路到这里，
不走 TEST_ENV 分类。

"""

from __future__ import annotations

import asyncio
import json
import os
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
        "--as", "user",
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
