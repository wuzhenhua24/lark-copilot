"""
lark-copilot · dispatcher

薄薄一层 dispatcher：把飞书 IM 事件流分发到具体的 capability handler。

两条路径，按发件人白名单分流：
- `COLLAB_SENDERS` → `agent_collab.handle_doc_qa`（envelope in / envelope out）
- `HUMAN_SENDERS`  → `agent_collab.handle_human_doc_qa`（自然语言 in / 纯文本 out）
- 不在任一白名单的发件人一律丢，作为安全门 + 噪音过滤

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
import sys
import time
from collections.abc import AsyncIterator

from dotenv import load_dotenv

# 必须先 load_dotenv 再 import agent_collab：agent_collab 模块级就会读 DRY_RUN 等
# 环境变量并锁定全局常量，import 时机比 load_dotenv 早就只能拿到默认值。
load_dotenv()

import agent_collab  # noqa: E402  — 故意放在 load_dotenv 之后

# Windows 控制台默认 cp936/cp1252，NDJSON 里的中文会乱码 / Mac & Linux 默认就是 UTF-8。
# 入口处把 stdout/stderr 强制成 UTF-8，跨平台行为一致。Python 3.7+ 的 TextIOWrapper
# 支持 reconfigure；在还没写过任何东西时调用是安全的（这里就在 import 之后、log 之前）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

LARK_CLI = os.environ.get("LARK_CLI", "lark-cli")

# 受信任的 peer agent open_id（逗号分隔）。这些发件人发的是结构化 envelope，
# 走 agent_collab.handle_doc_qa（envelope in/out）。
COLLAB_SENDERS = {
    s for s in os.environ.get("COLLAB_SENDERS", "").split(",") if s.strip()
}

# 真人用户 open_id 白名单（逗号分隔）。这些发件人发自然语言（飞书 URL +
# 问题），走 agent_collab.handle_human_doc_qa（plain text in/out）。
# 不在 COLLAB_SENDERS / HUMAN_SENDERS 任一名单里的发件人一律丢，既是安全门
# 也是噪音过滤。
HUMAN_SENDERS = {
    s for s in os.environ.get("HUMAN_SENDERS", "").split(",") if s.strip()
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

    # lark-cli 给的是扁平 projection：sender_id 直接是顶层字符串。
    # 飞书原始事件 envelope 则把它放在 sender.sender_id.open_id。两种都兜底。
    flat_sender = inner.get("sender_id")
    sender_id = (
        (sender.get("sender_id") or {}).get("open_id")
        or sender.get("open_id")
        or sender.get("user_id")
        or (flat_sender if isinstance(flat_sender, str) else None)
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
    # stdin=PIPE 而非默认 inherit：lark-cli `event consume` 把 stdin EOF 当退出信号
    # （为 AI subprocess caller 设计的优雅停机）。从 Python subprocess inherit 下来的
    # stdin 在某些场景下会被 lark-cli 立刻判 EOF → rc=3 退出。给它一个我们永远不写
    # 也不关的 PIPE，等价于 shell 里的 `< <(tail -f /dev/null)`，停机走 SIGTERM。
    # stderr 透传到父进程，既方便看 lark-cli 的告警，也避免 PIPE 没人读时阻塞。
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=None,
    )
    assert proc.stdout
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                rc = await proc.wait()
                log("consume_exited", rc=rc)
                break
            # rstrip() 而非 rstrip("\n")：Windows 下 lark-cli 输出可能是 CRLF，
            # 只剥 \n 会留下 \r 污染下游字符串比较（白名单匹配等）。
            yield line.decode().rstrip()
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
    if sender in COLLAB_SENDERS:
        log("collab_in", sender=sender)
        await agent_collab.handle_doc_qa(evt)
        return
    if sender in HUMAN_SENDERS:
        log("human_in", sender=sender)
        await agent_collab.handle_human_doc_qa(evt)
        return

    log("skip_untrusted_sender", sender=sender)


async def main() -> None:
    if not COLLAB_SENDERS and not HUMAN_SENDERS:
        log("warn_no_senders_configured")
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
