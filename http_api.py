"""
lark-copilot · http_api

给同内网的 peer agent（另一只飞书机器人）**直接 HTTP 调用**的 doc QA 接口。

为什么用 HTTP 而不是 agent_collab 里那条 envelope-over-IM 中继：
- 两只 bot 部署在同一内部测试环境，彼此 HTTP 可达，不需要飞书当 broker
- 同步请求/响应：POST 进去直接拿 answer，没有 req_id 关联、不用 listen 回复 DM
- 结构化 JSON 响应，富文本/图片场景比塞进一条 ~30KB 文本 DM 更好扩展，
  也没有 IM 的字节上限，answer 不需要截断

复用 agent_collab 里协议无关的核心：
- fetch_doc_as_markdown：拉飞书文档（--as user OAuth）
- answer_from_docs：单轮 doc QA，模型按需看图（--as user 拉图字节）
跑模型仍走部署机 Claude Code 绑定的固定第三方模型。

身份：本进程**不订阅**飞书事件（那是 router.py 的活），只拉文档 + 跑模型 + 拉图，
全程 --as user，不需要 bot 身份。可以和 router.py 同机各跑一个 systemd unit，
互不影响。

鉴权：HTTP_API_TOKEN 设了就要求 `Authorization: Bearer <token>`；没设则放行并在
启动时打 warning——内网测试方便，上线务必配上。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import time
from uuid import uuid4

from dotenv import load_dotenv

# 必须先 load_dotenv 再 import agent_collab：后者模块级就锁定 DOC_FETCH_TIMEOUT_SEC
# 等全局常量，import 早于 load_dotenv 就只拿到默认值。
load_dotenv()

import agent_collab  # noqa: E402  — 故意放在 load_dotenv 之后

from fastapi import Depends, FastAPI, Header, HTTPException  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from pydantic import BaseModel, Field, field_validator  # noqa: E402

HTTP_API_TOKEN = os.environ.get("HTTP_API_TOKEN", "").strip()
HTTP_HOST = os.environ.get("HTTP_HOST", "127.0.0.1")
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8800"))


def log(event: str, **fields: object) -> None:
    rec = {"ts": int(time.time()), "event": event, **fields}
    print(json.dumps(rec, ensure_ascii=False), flush=True)


# --- auth --------------------------------------------------------------------

async def require_auth(authorization: str | None = Header(default=None)) -> None:
    """HTTP_API_TOKEN 未设则放行（内网测试，启动时已 warn）；设了就校验 Bearer。"""
    if not HTTP_API_TOKEN:
        return
    expected = f"Bearer {HTTP_API_TOKEN}"
    # 常量时间比较，避免 token 被计时侧信道试探。
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


# --- request / response ------------------------------------------------------

class DocQARequest(BaseModel):
    docs: list[str] = Field(..., min_length=1, description="飞书文档 URL 或 token，至少一篇")
    q: str = Field(..., min_length=1, description="基于这些文档要问的问题")
    req_id: str | None = Field(default=None, description="可选，调用方自带的关联 id，便于串日志")

    @field_validator("docs")
    @classmethod
    def _strip_and_require_nonblank(cls, v: list[str]) -> list[str]:
        cleaned = [d.strip() for d in v if isinstance(d, str) and d.strip()]
        if not cleaned:
            raise ValueError("docs 至少要有一个非空 URL/token")
        return cleaned


app = FastAPI(title="lark-copilot doc QA", version="0.1.0")


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.post("/doc_qa", response_model=None)
async def doc_qa(req: DocQARequest, _auth: None = Depends(require_auth)) -> JSONResponse | dict:
    """拉文档 → 跑单轮 doc QA（模型按需看图）→ 同步返回 answer。

    任一步超时/异常都翻成 {ok:false, error}，并带相应 HTTP 状态码，让调用方
    不用干等。answer 是 markdown 文本。
    """
    req_id = req.req_id or uuid4().hex[:12]
    started = time.time()
    log("http_doc_qa_in", req_id=req_id, docs=req.docs, doc_count=len(req.docs), q_preview=req.q[:80])

    try:
        # 并发拉所有文档；DOC_FETCH_TIMEOUT_SEC 给整体兜底（每篇内部已并行拉
        # markdown+xml，外层 gather 再让多篇彼此并行）。
        fetched = await asyncio.wait_for(
            asyncio.gather(*(agent_collab.fetch_doc_as_markdown(u) for u in req.docs)),
            timeout=agent_collab.DOC_FETCH_TIMEOUT_SEC,
        )
        triples = [(u, md, imgs) for u, (md, imgs) in zip(req.docs, fetched)]
        log(
            "http_doc_fetched",
            req_id=req_id,
            doc_count=len(triples),
            total_bytes=sum(len(md) for _, md, _ in triples),
            img_count=sum(len(imgs) for *_, imgs in triples),
        )
        answer = await asyncio.wait_for(
            agent_collab.answer_from_docs(req.q, triples, req_id=req_id),
            timeout=agent_collab.INFERENCE_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        took = int((time.time() - started) * 1000)
        log("http_doc_qa_timeout", req_id=req_id, took_ms=took)
        return JSONResponse(
            status_code=504,
            content={"ok": False, "req_id": req_id, "error": "timeout", "took_ms": took},
        )
    except Exception as ex:  # noqa: BLE001
        took = int((time.time() - started) * 1000)
        log("http_doc_qa_error", req_id=req_id, error=str(ex)[:300], took_ms=took)
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "req_id": req_id,
                "error": f"{type(ex).__name__}: {ex}"[:300],
                "took_ms": took,
            },
        )

    took = int((time.time() - started) * 1000)
    log("http_doc_qa_done", req_id=req_id, took_ms=took, answer_len=len(answer))
    return {"ok": True, "req_id": req_id, "answer": answer, "took_ms": took}


def main() -> None:
    import uvicorn

    if not HTTP_API_TOKEN:
        log("warn_no_http_token", msg="HTTP_API_TOKEN 未设置 —— /doc_qa 当前无鉴权，仅限可信内网")
    log("http_starting", host=HTTP_HOST, port=HTTP_PORT)
    # log_config=None：不让 uvicorn 接管 logging，保持我们自己的 NDJSON 一行一条
    uvicorn.run(app, host=HTTP_HOST, port=HTTP_PORT, log_config=None)


if __name__ == "__main__":
    main()
