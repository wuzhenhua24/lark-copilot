"""
lark-copilot · dispatcher

薄薄一层 dispatcher：把飞书 IM 事件流分发到具体的 capability handler。

当前只有一个 handler —— `agent_collab.handle_doc_qa`：接收来自受信任 peer agent
（典型如 ops-qa-bot）的 doc_qa envelope，去飞书拉文档、跑 Claude 答题、再 ack 回去。

身份与限制：
- `im.message.receive_v1` 事件在飞书 Open Platform 是**应用级**的，只支持 `--as bot`
  订阅。没有"以 user 身份订阅自己收到的 DM"这条路径。
- 所以这一侧 router 跑 `--as bot`，监听的是 **lark-copilot bot 收到的 DM**；peer agent
  发 envelope 时也得 DM 这个 bot。
- 文档拉取（`agent_collab.fetch_doc_as_markdown`）继续走 `--as user`，因为 docx 读权限
  挂在登录 user 的 OAuth 上。两个身份在同一进程里 per-command 切换没问题。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator

from dotenv import load_dotenv

import agent_collab

load_dotenv()

LARK_CLI = os.environ.get("LARK_CLI", "lark-cli")

# 受信任的 peer agent open_id（逗号分隔）。只有来自这里的发件人才被路由到
# agent_collab，其它一律忽略——既是安全门，也是噪音过滤。
COLLAB_SENDERS = {
    s for s in os.environ.get("COLLAB_SENDERS", "").split(",") if s.strip()
}


def log(event: str, **fields: object) -> None:
    rec = {"ts": int(time.time()), "event": event, **fields}
    print(json.dumps(rec, ensure_ascii=False), flush=True)


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


# --- main loop ---------------------------------------------------------------

async def stream_events() -> AsyncIterator[str]:
    cmd = [LARK_CLI, "event", "consume", "im.message.receive_v1", "--as", "bot"]
    log("starting", cmd=" ".join(cmd))
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
        return  # group chats are out of scope
    if evt["msg_type"] != "text":
        log("skip_non_text", msg_type=evt["msg_type"])
        return
    sender = evt["sender_id"]
    if not sender:
        log("skip_no_sender")
        return
    if sender not in COLLAB_SENDERS:
        log("skip_untrusted_sender", sender=sender)
        return

    log("collab_in", sender=sender)
    await agent_collab.handle_doc_qa(evt)


async def main() -> None:
    if not COLLAB_SENDERS:
        log("warn_no_collab_senders")
    async for line in stream_events():
        if not line.strip():
            continue
        evt = extract_message(line)
        if not evt:
            log("parse_fail", raw=line[:200])
            continue
        log("event_in", chat_type=evt["chat_type"], msg_type=evt["msg_type"], sender=evt["sender_id"])
        try:
            await handle(evt)
        except Exception as ex:
            log("handler_error", error=str(ex))


if __name__ == "__main__":
    asyncio.run(main())
