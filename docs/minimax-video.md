# MiniMax video (fork extension)

This fork adds a cloud MiniMax H3 V2 client. No model weights, GPU or local inference
are needed. Video generation consumes **pay-as-you-go MiniMax API credit**; it does
not use a ChatGPT, Grok, or MiniMax chat subscription login. Existing Grok commands
are unchanged. No automatic fallback between providers occurs.

## Authentication

Set `MINIMAX_API_KEY` in your environment/secret manager. Do not commit it or pass
it in command arguments. The default API origin is `https://api.minimax.io`;
`MINIMAX_BASE_URL` can select a trusted HTTPS endpoint that implements **the same
V2 contract**, not an arbitrary OpenAI-compatible endpoint.

## Generate locally

```sh
sprite-gen minimax-video --image canvas.png --prompt 'Fixed camera. The character walks in place.' \
  --model MiniMax-H3 --duration 5 --resolution 768P --out walk.mp4 --report walk.task.json

# Pin both endpoints (optional; verify the actual loop visually).
sprite-gen minimax-video --image canvas.png --last-frame canvas.png \
  --prompt 'A seamless idle breathing motion, fixed camera.' --out idle.mp4 --report idle.task.json

# Text-only generation needs an explicit aspect ratio.
sprite-gen minimax-video --prompt 'Clouds passing over mountains.' --ratio 16:9 --out clouds.mp4
```

Local input supports PNG/JPEG/WebP, at most 30 MB per file, dimensions 256–5760 px,
width/height ratio 0.4–2.5. Pad small sprites with `video-canvas` first. The first
version supports text-only, first-frame, last-frame and first+last-frame inputs;
reference video/audio and video editing are not exposed.

Documented model constraints (checked 2026-10-16):

| Model | Duration | Resolution |
|---|---|---|
| MiniMax-H3 (default) | 4–15 seconds | 768P, 2K |
| MiniMax-H3-Max | 5–15 seconds | 480P, 768P |

These are provider documentation constraints, not locally benchmarked quality,
latency or availability guarantees. No real paid generation has been tested yet.

## Lightweight server / local processing

```sh
# Submit only: returns task_id, no polling, download or post-processing.
sprite-gen minimax-video --image canvas.png --prompt 'Walk in place.' \
  --submit-only --report task.json

sprite-gen minimax-video-status --task-id TASK_ID

# Run on the trusted machine holding the key; never creates another paid job.
sprite-gen minimax-video-resume --task-id TASK_ID --out walk.mp4 --report task.json
```

The importable `Client.submit(payload)` and `Client.query(task_id)` can be wrapped
by a password-protected relay. `query` returns the provider task, including its
expiring `content.url` on success, so the relay can give that URL to an authorized
caller for direct download. The status CLI deliberately prints only ID/status.
API credentials stay on the server; the download uses a separate client with no
Authorization header. A password-protected web service itself is **not** included
in this patch.

Use the downloaded MP4 with the existing local `video-frames` and `video-loop`
commands (see [video pipeline](video-pipeline.md)). The Grok `video-set` batch
command is not rerouted to MiniMax by this patch.

## Failure and restart behavior

The accepted task ID is printed to stderr immediately and saved to `--report`
before polling. A POST is attempted once: automatically repeating a timed-out
submission could buy the video twice. If no ID was received, check the MiniMax
console before submitting again. If an ID exists, use `minimax-video-resume`.

Polling or download failure retains the ID. A timeout is local and does not cancel
the provider job. The provider supports querying the last seven days; result URLs
expire. Resume requests a fresh task result and downloads again without generating.
Output is staged in a temporary file and published only after an MP4 `ftyp` check;
this is container identification, not full codec/content quality validation.
Existing CLI output files are rejected before generation. API keys, uploaded media
and signed URLs are excluded from CLI reports and error messages.

## Official sources

- [Create V2 video task](https://platform.minimax.io/docs/api-reference/video-generation-v2-create)
- [Query V2 task](https://platform.minimax.io/docs/api-reference/video-generation-v2-query)

Verification uses mocked official response shapes, not live credentials.
