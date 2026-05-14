"""
lark-copilot · dm router

First capability of the lark-copilot project. Listens to your incoming
Lark/Feishu DMs via `lark-cli event consume`, classifies each one with Claude
(test-env support question or not), and on a match auto-replies with a redirect
to your group + group bot. Group chats are ignored; cooldown prevents replying
to the same person twice.

Other capabilities (doc drafting, multi-agent workflows) will live alongside
this module as the project grows.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import time
from collections.abc import AsyncIterator
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    TextBlock,
    query,
)
from dotenv import load_dotenv

import agent_collab

load_dotenv()

GROUP_NAME = os.environ.get("GROUP_NAME", "测试环境支持群")
GROUP_BOT_NAME = os.environ.get("GROUP_BOT_NAME", "测试环境助手")
DRY_RUN = os.environ.get("DRY_RUN", "1") == "1"
COOLDOWN_HOURS = float(os.environ.get("COOLDOWN_HOURS", "24"))
LARK_CLI = os.environ.get("LARK_CLI", "lark-cli")
STATE_FILE = Path(os.environ.get("STATE_FILE", ".state/cooldown.json"))

ONLY_SENDERS = {s for s in os.environ.get("ONLY_SENDERS", "").split(",") if s.strip()}
SKIP_SENDERS = {s for s in os.environ.get("SKIP_SENDERS", "").split(",") if s.strip()}

# 协作机器人（ops-qa-bot）的 open_id：来自它的 sender_id 时，跳过 TEST_ENV 分类，
# 走 agent_collab.handle_doc_qa 处理结构化协作请求。多个用逗号分隔。
COLLAB_SENDERS = {
    s for s in os.environ.get("COLLAB_SENDERS", "").split(",") if s.strip()
}

REDIRECT_TEMPLATE = (
    "嗨～测试环境相关的问题，麻烦移步「{group_name}」群里 @{group_bot_name} 直接问哈，"
    "那边有机器人秒回，也比我私聊查得准。\n\n"
    "（这条是我设置的自动回复，私聊这类问题就先不一一回了 🙏）"
)

SYSTEM_PROMPT = """你是一个 DM 分流助手。判断收到的私聊消息是否属于"测试环境维护/支持"类问题。

属于 TEST_ENV 的典型例子：
- 测试环境登不上 / 报错 / 502 / 503 / 数据库连不上 / 接口挂了
- 怎么申请测试环境账号 / 部署 / 域名 / 配置 / 权限
- 某个测试服务挂了 / 重启 / 跑不动 / 数据异常
- 测试数据怎么造 / mock 接口怎么用 / sandbox 怎么开

属于 OTHER 的：
- 私事、闲聊、约饭
- 工作但不涉及测试环境（代码 review、产品讨论、需求对齐、问候）
- 暧昧/不明确的，一律 OTHER（宁可漏一个，不要误判朋友的私聊）

严格输出单行 JSON，不要 markdown，不要解释：
{"class": "TEST_ENV" | "OTHER", "reason": "一句话"}
"""


def log(event: str, **fields: object) -> None:
    rec = {"ts": int(time.time()), "event": event, **fields}
    print(json.dumps(rec, ensure_ascii=False), flush=True)


# --- cooldown persistence ----------------------------------------------------

def load_state() -> dict[str, float]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict[str, float]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False))


_state: dict[str, float] = load_state()


def in_cooldown(sender_id: str) -> bool:
    last = _state.get(sender_id)
    return bool(last) and (time.time() - last) < COOLDOWN_HOURS * 3600


def mark_replied(sender_id: str) -> None:
    _state[sender_id] = time.time()
    save_state(_state)


# --- event parsing -----------------------------------------------------------

def extract_message(evt_line: str) -> dict | None:
    """Pull the fields we care about out of a lark-cli event line.

    Defensive: shape may be the raw Feishu event envelope ({event:{message,sender}})
    or a flattened projection. Tries both.
    """
    try:
        raw = json.loads(evt_line)
    except Exception:
        return None

    inner = raw.get("event") if isinstance(raw.get("event"), dict) else raw
    msg = inner.get("message") or {}
    sender = inner.get("sender") or {}

    sender_id = (
        (sender.get("sender_id") or {}).get("open_id")
        or sender.get("open_id")
        or sender.get("user_id")
    )
    chat_type = msg.get("chat_type") or inner.get("chat_type")
    chat_id = msg.get("chat_id") or inner.get("chat_id")
    msg_type = msg.get("message_type") or inner.get("message_type")
    content_raw = msg.get("content") or inner.get("content")

    text = None
    if msg_type == "text" and content_raw:
        try:
            text = json.loads(content_raw).get("text") if isinstance(content_raw, str) else content_raw.get("text")
        except Exception:
            text = str(content_raw)

    return {
        "sender_id": sender_id,
        "chat_type": chat_type,
        "chat_id": chat_id,
        "msg_type": msg_type,
        "text": text,
        "raw": raw,
    }


# --- classification ----------------------------------------------------------

async def classify(text: str) -> dict:
    # 不指定 model：部署机的 Claude Code 已绑定固定的第三方模型，让 SDK
    # 按它的配置走即可。本仓库内任何地方都不做 per-scenario model 选择。
    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        allowed_tools=[],
        max_turns=1,
    )
    out = ""
    async for msg in query(prompt=f"DM 内容：\n{text}", options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    out += block.text
    try:
        return json.loads(out.strip())
    except Exception:
        return {"class": "OTHER", "reason": f"parse_fail: {out[:120]!r}"}


# --- send reply --------------------------------------------------------------

async def send_redirect(chat_id: str) -> None:
    text = REDIRECT_TEMPLATE.format(
        group_name=GROUP_NAME,
        group_bot_name=GROUP_BOT_NAME,
    )
    cmd = [
        LARK_CLI, "im", "+messages-send",
        "--as", "user",
        "--chat-id", chat_id,
        "--text", text,
    ]
    if DRY_RUN:
        log("dry_run_send", chat_id=chat_id, cmd=" ".join(shlex.quote(c) for c in cmd))
        return
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        log("send_failed", chat_id=chat_id, rc=proc.returncode, stderr=stderr.decode()[:400])
    else:
        log("send_ok", chat_id=chat_id)


# --- main loop ---------------------------------------------------------------

async def stream_events() -> AsyncIterator[str]:
    cmd = [LARK_CLI, "event", "consume", "im.message.receive_v1", "--as", "user"]
    log("starting", cmd=" ".join(cmd), dry_run=DRY_RUN)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                rc = await proc.wait()
                log("consume_exited", rc=rc)
                break
            yield line.decode().rstrip("\n")
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()


async def handle(evt: dict) -> None:
    if evt["chat_type"] != "p2p":
        return  # ignore group chats
    if evt["msg_type"] != "text":
        log("skip_non_text", msg_type=evt["msg_type"])
        return
    sender = evt["sender_id"]
    if not sender:
        log("skip_no_sender")
        return

    # 协作机器人短路：跳过 TEST_ENV 分类、白/黑名单、冷却 —— 这条路径只看 sender。
    # COLLAB_SENDERS 是受信任的 peer agent；它发来的就是结构化 envelope，由
    # agent_collab 自己 parse、自己处理失败回写，路由层不干涉。
    if sender in COLLAB_SENDERS:
        log("collab_in", sender=sender)
        await agent_collab.handle_doc_qa(evt)
        return

    if ONLY_SENDERS and sender not in ONLY_SENDERS:
        log("skip_not_in_allowlist", sender=sender)
        return
    if sender in SKIP_SENDERS:
        log("skip_denylist", sender=sender)
        return
    if in_cooldown(sender):
        log("skip_cooldown", sender=sender)
        return

    text = (evt["text"] or "").strip()
    if not text:
        return

    result = await classify(text)
    log("classified", sender=sender, result=result, preview=text[:80])
    if result.get("class") != "TEST_ENV":
        return

    await send_redirect(evt["chat_id"])
    mark_replied(sender)


async def main() -> None:
    async for line in stream_events():
        if not line.strip():
            continue
        evt = extract_message(line)
        if not evt:
            log("parse_fail", raw=line[:200])
            continue
        # Always log the first few raw events while you verify field shapes
        log("event_in", chat_type=evt["chat_type"], msg_type=evt["msg_type"], sender=evt["sender_id"])
        try:
            await handle(evt)
        except Exception as ex:
            log("handler_error", error=str(ex))


if __name__ == "__main__":
    asyncio.run(main())
