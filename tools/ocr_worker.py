"""Single-capacity private OCR service; each inference has a killable deadline."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from app.ocr_client import MAX_IMAGE_BYTES, MAX_TEXT_CHARS

TASK_TIMEOUT_SECONDS = 30


def execute_image(payload):
    # All request data and generated crops disappear on success, failure or timeout.
    with tempfile.TemporaryDirectory(prefix="saas-ocr-") as directory:
        path = Path(directory) / "input.png"
        path.write_bytes(payload)
        environment = {**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
                       "MKL_NUM_THREADS": "1"}
        try:
            result = subprocess.run([sys.executable, "-m", "tools.ocr_task", str(path)],
                                    capture_output=True, timeout=TASK_TIMEOUT_SECONDS, env=environment)
        except subprocess.TimeoutExpired:
            return 504, {"error": "timeout"}
        if result.returncode != 0:
            return 422, {"error": "unrecognizable"}
        try:
            text = json.loads(result.stdout)["text"]
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
                raise ValueError()
            return 200, {"text": text}
        except (ValueError, KeyError, TypeError):
            return 503, {"error": "unavailable"}


def create_app():
    service = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    # One event loop owns admission; no waiting queue or overlapping model processes.
    busy = False

    @service.get("/health")
    async def health():
        return {"status": "ok"}

    @service.post("/recognize")
    async def recognize(request: Request):
        nonlocal busy
        if busy:
            return JSONResponse({"error": "busy"}, status_code=429,
                                headers={"Retry-After": "3"})
        if request.headers.get("content-type") != "application/octet-stream":
            return JSONResponse({"error": "unsupported"}, status_code=415)
        busy = True
        try:
            payload = bytearray()
            async with asyncio.timeout(10):
                async for chunk in request.stream():
                    payload.extend(chunk)
                    if len(payload) > MAX_IMAGE_BYTES:
                        return JSONResponse({"error": "too-large"}, status_code=413)
            if not payload:
                return JSONResponse({"error": "empty"}, status_code=422)
            # Shield completion from client disconnect: never release admission while
            # the child still runs. subprocess.run kills & reaps it at the deadline.
            task = asyncio.create_task(run_in_threadpool(execute_image, bytes(payload)))
            try:
                status, body = await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
            return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})
        except TimeoutError:
            return JSONResponse({"error": "upload-timeout"}, status_code=408)
        except Exception:
            return JSONResponse({"error": "unavailable"}, status_code=503)
        finally:
            busy = False

    return service


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_app(), host="0.0.0.0", port=8001, workers=1, access_log=False)
