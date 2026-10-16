# SPDX-License-Identifier: Apache-2.0
"""MiniMax H3 V2 video client. No local inference, no automatic POST retries.

Official contracts: docs/minimax-video.md. submit/query are usable independently
by a relay; the CLI can also wait and download for local sprite post-processing.
"""
from __future__ import annotations

import argparse
import base64
import json
import http.client
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from PIL import Image

MODELS = ("MiniMax-H3", "MiniMax-H3-Max")
RATIOS = ("adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16")


class MiniMaxError(RuntimeError):
    """Safe diagnostic: never includes credentials, media or signed URLs."""


class TaskFailed(MiniMaxError):
    def __init__(self, status: str):
        self.status = status
        super().__init__(f"MiniMax task {status}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Do not forward API credentials to redirected hosts.


def image_content(path: Path, role: str) -> dict:
    if path.stat().st_size > 30 * 1024 * 1024:
        raise MiniMaxError("image exceeds 30 MB")
    with Image.open(path) as image:
        if image.format not in {"PNG", "JPEG", "WEBP"}:
            raise MiniMaxError("local images must be PNG, JPEG or WEBP")
        w, h = image.size
        if not (256 <= w <= 5760 and 256 <= h <= 5760 and 0.4 <= w / h <= 2.5):
            raise MiniMaxError("image dimensions must be 256–5760 px and aspect ratio 0.4–2.5; pad sprites first")
        mime = {"PNG": "png", "JPEG": "jpeg", "WEBP": "webp"}[image.format]
        image.verify()
    return {"type": "image_url", "role": role,
            "image_url": {"url": f"data:image/{mime};base64," + base64.b64encode(path.read_bytes()).decode()}}


def build_payload(*, prompt: str, image: Path | None = None, last_frame: Path | None = None,
                  model: str = "MiniMax-H3", duration: int = 5,
                  resolution: str = "768P", ratio: str = "adaptive") -> dict:
    if not prompt.strip() or len(prompt) > 7000:
        raise MiniMaxError("prompt must contain 1–7000 characters")
    if model not in MODELS or ratio not in RATIOS:
        raise MiniMaxError("unsupported model or ratio")
    minimum = 4 if model == "MiniMax-H3" else 5
    allowed = {"768P", "2K"} if model == "MiniMax-H3" else {"480P", "768P"}
    if not minimum <= duration <= 15 or resolution not in allowed:
        raise MiniMaxError("duration or resolution is not supported by the selected model")
    if image is None and last_frame is None and ratio == "adaptive":
        raise MiniMaxError("text-to-video requires an explicit --ratio, e.g. 16:9")
    if (image or last_frame) and ratio != "adaptive":
        raise MiniMaxError("first/last-frame input requires --ratio adaptive")
    content = [{"type": "text", "text": prompt}]
    for path, role in [(image, "first_frame"), (last_frame, "last_frame")]:
        if path is not None:
            content.append(image_content(Path(path), role))
    payload = dict(model=model, content=content, duration=duration, resolution=resolution, ratio=ratio)
    if len(json.dumps(payload).encode()) > 64 * 1024 * 1024:
        raise MiniMaxError("request exceeds 64 MB")
    return payload


class Client:
    def __init__(self, *, api_key: str | None = None, base_url: str | None = None):
        self._key = api_key or os.environ.get("MINIMAX_API_KEY", "")
        self.base_url = (base_url or os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.io")).rstrip("/")
        url = urlsplit(self.base_url)
        if not self._key:
            raise MiniMaxError("set MINIMAX_API_KEY (pay-as-you-go video API)")
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise MiniMaxError("MINIMAX_BASE_URL must be an HTTPS API origin without credentials or query")
        self._opener = urllib.request.build_opener(NoRedirect)

    def _request(self, path: str, payload: dict | None = None, *, timeout: float = 60) -> dict:
        request = urllib.request.Request(self.base_url + path,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={"Authorization": "Bearer " + self._key, "Content-Type": "application/json"})
        try:
            with self._opener.open(request, timeout=timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            raise MiniMaxError(f"MiniMax HTTP {exc.code}; no automatic resubmission") from None
        except (OSError, ValueError, http.client.HTTPException):
            raise MiniMaxError("MiniMax transport/JSON failure; submission outcome may be unknown; do not blindly resubmit") from None
        if not isinstance(result, dict) or result.get("error"):
            raise MiniMaxError("MiniMax returned an invalid or error response")
        return result

    def submit(self, payload: dict) -> str:
        print("MiniMax video uses MINIMAX_API_KEY and is billed per call.", file=sys.stderr)
        result = self._request("/v2/video_generation", payload)
        return valid_task_id(result.get("task_id"))

    def query(self, task_id: str, *, timeout: float = 60) -> dict:
        result = self._request("/v2/query/video_generation/" + valid_task_id(task_id), timeout=timeout)
        task = result.get("task")
        if not isinstance(task, dict) or task.get("status") not in {"queued", "running", "succeeded", "failed", "cancelled"}:
            raise MiniMaxError("MiniMax returned an invalid task")
        return task


def valid_task_id(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise MiniMaxError("invalid or missing task_id")
    return value


def download(url: str, out: Path, *, timeout: float) -> int:
    if urlsplit(url).scheme != "https":
        raise MiniMaxError("video result must have an HTTPS download URL")
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        # Separate opener: API Authorization is never sent to the CDN.
        with urllib.request.build_opener(NoRedirect).open(url, timeout=min(timeout, 60)) as response:
            expected = getattr(response, "headers", {}).get("Content-Length")
            with tempfile.NamedTemporaryFile(dir=out.parent, delete=False) as target:
                temporary = Path(target.name)
                deadline = time.monotonic() + timeout
                total = 0
                while True:
                    chunk = response.read(64 * 1024)
                    if time.monotonic() > deadline:
                        raise MiniMaxError("video download timed out")
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > 512 * 1024 * 1024:
                        raise MiniMaxError("video download exceeds 512 MB")
                    target.write(chunk)
                if expected is not None and total != int(expected):
                    raise MiniMaxError("video download was truncated; resume the existing task")
        with temporary.open("rb") as handle:
            if handle.read(12)[4:8] != b"ftyp":
                raise MiniMaxError("download is not an MP4 (missing ftyp)")
        temporary.replace(out)
        return total
    except (OSError, ValueError, http.client.HTTPException):
        raise MiniMaxError("video download failed; resume the existing task") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def wait_for_video(client: Client, task_id: str, out: Path, *, timeout: float = 900, poll_interval: float = 5) -> dict:
    if timeout <= 0 or poll_interval <= 0:
        raise MiniMaxError("timeout and poll interval must be positive")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = client.query(task_id, timeout=min(60, max(0.01, deadline - time.monotonic())))
        status = task["status"]
        if status in {"failed", "cancelled"}:
            raise TaskFailed(status)
        if status == "succeeded":
            url = (task.get("content") or {}).get("url")
            remaining = deadline - time.monotonic()
            if not isinstance(url, str) or remaining <= 0:
                raise MiniMaxError("completed task missing URL or local deadline exceeded; resume later")
            size = download(url, out, timeout=remaining)
            return dict(provider="minimax", task_id=task_id, status=status, out=str(out), bytes=size)
        time.sleep(min(poll_interval, max(0, deadline - time.monotonic())))
    raise MiniMaxError("local polling timeout; task may still finish")


def add_arguments(p: argparse.ArgumentParser) -> None:
    prompt = p.add_mutually_exclusive_group(required=True)
    prompt.add_argument("--prompt")
    prompt.add_argument("--prompt-file", type=Path)
    p.add_argument("--image", type=Path)
    p.add_argument("--last-frame", type=Path)
    p.add_argument("--model", choices=MODELS, default=MODELS[0])
    p.add_argument("--duration", type=int, default=5)
    p.add_argument("--resolution", choices=("480P", "768P", "2K"), default="768P")
    p.add_argument("--ratio", choices=RATIOS, default="adaptive")
    p.add_argument("--submit-only", action="store_true", help="return task_id without polling or downloading")
    _output_arguments(p)


def _output_arguments(p):
    p.add_argument("--out", type=Path)
    p.add_argument("--report", type=Path)
    p.add_argument("--timeout", type=float, default=900)
    p.add_argument("--poll-interval", type=float, default=5)


def add_resume_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--task-id", required=True)
    _output_arguments(p)


def add_status_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--task-id", required=True)


def _publish(result: dict, report: Path | None):
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if report:
        from sprite_gen.spec.runio import atomic_write_text
        atomic_write_text(report, text + "\n")
    print(text)


def _execute(*, task_id=None, out=None, report=None, timeout=900, poll_interval=5, submit_only=False, **kwargs):
    if not submit_only and out is None:
        raise MiniMaxError("--out is required unless --submit-only is used")
    if timeout <= 0 or poll_interval <= 0:
        raise MiniMaxError("timeout and poll interval must be positive")
    if out is not None and out.exists():
        raise MiniMaxError("output exists; choose a new --out path")
    client = Client()
    if task_id is None:
        prompt_file = kwargs.pop("prompt_file", None)
        if prompt_file:
            kwargs["prompt"] = prompt_file.read_text(encoding="utf-8")
        task_id = client.submit(build_payload(**kwargs))
    valid_task_id(task_id)
    result = dict(provider="minimax", task_id=task_id, status="submitted")
    print(f"task_id={task_id}; resume with sprite-gen minimax-video-resume --task-id {task_id} --out clip.mp4", file=sys.stderr)
    _publish(result, report)  # Persist accepted ID before polling/download can fail.
    if not submit_only:
        try:
            result = wait_for_video(client, task_id, out, timeout=timeout, poll_interval=poll_interval)
        except MiniMaxError as exc:
            result.update(status=exc.status if isinstance(exc, TaskFailed) else "unfinished", error=str(exc))
            _publish(result, report)
            if isinstance(exc, TaskFailed):
                raise MiniMaxError(f"{exc}; task_id={task_id}; inspect the failed task before a new submission") from None
            raise MiniMaxError(f"{exc}; task_id={task_id}; use minimax-video-resume, do not resubmit") from None
        _publish(result, report)
    return 0


def run(**kwargs) -> int:
    try:
        return _execute(**kwargs)
    except (MiniMaxError, OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from None


def run_resume(**kwargs) -> int:
    return run(**kwargs)


def run_status(*, task_id: str) -> int:
    try:
        task = Client().query(task_id)
        # URLs are temporary; callers needing a URL can use Client.query directly.
        _publish(dict(provider="minimax", task_id=task_id, status=task["status"]), None)
        return 0
    except MiniMaxError as exc:
        raise SystemExit(str(exc)) from None
