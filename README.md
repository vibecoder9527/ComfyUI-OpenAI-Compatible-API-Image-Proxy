# ComfyUI OpenAI-Compatible API Image Proxy

[English](README.md) | [繁體中文](README.zh-TW.md)

Converts ComfyUI's API format into an **OpenAI-compatible image generation API**. Any frontend or SDK that supports OpenAI `images/generations` / `images/edits` can simply point its Base URL at this service and generate images with your own ComfyUI workflows.

## Features

- **OpenAI-compatible endpoints**: `/v1/images/generations`, `/v1/images/edits`, `/v1/models`
- **One model = one set of workflows**: drop a workflow JSON into a folder and the file name becomes the model ID, no code changes needed
- **Automatic T2I / I2I routing**: one model can offer both text-to-image and reference-image generation, chosen automatically depending on whether the request carries images
- **Multiple reference images**: multipart files, base64, data URLs, and http(s) URLs
- **Multiple images per request**: the `n` parameter (1–10), each image uses a different seed
- **Two response formats**: `b64_json` (default) or `url` (cached in memory, expires automatically)
- **`size=auto`**: follows the aspect ratio of the reference image
- **OpenAI-style error responses**: SDKs and frontends can parse `error.message` directly
- **Hot reload**: call `/reload_workflows` to reload workflows without restarting, with load errors reported per file

## How it works

```
Frontend / SDK ──(OpenAI-Compatible API)──▶ This service ──(fill placeholders)──▶ ComfyUI
                                              │                                        │
                                              └──── poll for results, return images ◀──┘
```

1. Select the matching workflow based on `model` and whether reference images were sent
2. Replace the placeholders in the workflow (`%prompt%`, `%width%`, etc.) with the request values
3. If there are reference images, upload them to ComfyUI's input directory first
4. Queue the job in ComfyUI and poll `/history` for the output image
5. Return the result in the OpenAI format

## Quick start

### Requirements

- Python 3.10+
- A reachable ComfyUI service
- Packages: `fastapi`, `uvicorn`, `httpx`, `python-multipart` (needed for multipart uploads)

### Run directly

```bash
pip install fastapi uvicorn httpx python-multipart

export COMFYUI_URL=http://127.0.0.1:8188
export WORKFLOW_DIR=./workflows

uvicorn app:app --host 0.0.0.0 --port 8000
```

### Docker Compose example

This is only an example; adjust the image, paths, and port to your environment:

```yaml
services:
  proxy:
    build: .
    ports:
      - "8000:8000"
    environment:
      COMFYUI_URL: http://comfyui:8188
      WORKFLOW_DIR: /app/workflows
    volumes:
      - ./workflows:/app/workflows   # Put workflows here; hot-reload them with /reload_workflows
```

## Configuration (environment variables)

| Variable | Default | Description |
| --- | --- | --- |
| `COMFYUI_URL` | `http://comfyui:8188` | Address of ComfyUI |
| `WORKFLOW_DIR` | `/app/workflows` | Folder containing the workflow JSON files |
| `MAX_N` | `10` | Max number of images per request |
| `MIN_SIZE_SIDE` | `64` | Minimum length of each side of `size` |
| `MAX_SIZE_SIDE` | `4096` | Maximum length of each side of `size` |
| `AUTO_SIZE_MAX_SIDE` | `1536` | With `size=auto`, the cap on the longest side when following the reference image's size |
| `GENERATION_TIMEOUT_SECONDS` | `180` | Max wait per image (seconds); for n images the total limit is this value × n |
| `MAX_REF_IMAGE_BYTES` | `20971520` (20MB) | Max size of a single reference image |
| `IMAGE_CACHE_MAX_SIZE` | `100` | Max number of images cached in `url` mode |
| `IMAGE_CACHE_TTL_SECONDS` | `600` | How long images are kept in `url` mode (seconds) |

## Workflow configuration

### File naming

On startup, every `.json` file in `WORKFLOW_DIR` is scanned:

| File name | Model ID | Purpose |
| --- | --- | --- |
| `aaa-T2I.json` | `aaa` | Text-to-image |
| `aaa-I2I.json` | `aaa` | Reference-image generation |
| `foo.json` | `foo` | No suffix, treated as T2I only |

`aaa-T2I.json` and `aaa-I2I.json` are the **same model** to the frontend; `/v1/models` lists `aaa` only once.

### Routing rules

| Workflows the model has | Request has reference images | Request has no reference images |
| --- | --- | --- |
| T2I + I2I | Uses I2I | Uses T2I |
| T2I only | Uses T2I (reference images are ignored) | Uses T2I |
| I2I only | Uses I2I | Uses I2I, with a 1×1 white image filling the reference slots |

An unknown model returns `404 model_not_found`; a missing `model` returns `400 missing_required_parameter`.

### Placeholders

Write the following strings in the workflow JSON; they are replaced at request time:

| Placeholder | Replaced with |
| --- | --- |
| `%prompt%` | The request's `prompt` (JSON-escaped) |
| `%width%` / `%height%` | Width and height parsed from `size` |
| `%seed%` | The seed (the request's `seed` if given, otherwise random; incremented by 1 per image when `n > 1`) |
| `%image%` (same as `%image1%`) | File name of the 1st reference image after uploading to ComfyUI |
| `%image2%`, `%image3%` … | The 2nd, 3rd … reference images |

Example (excerpt, workflow exported in API format):

```json
{
  "6": { "class_type": "CLIPTextEncode", "inputs": { "text": "%prompt%", "clip": ["4", 1] } },
  "3": { "class_type": "KSampler", "inputs": { "seed": "%seed%", "steps": 20 } },
  "5": { "class_type": "EmptyLatentImage", "inputs": { "width": "%width%", "height": "%height%", "batch_size": 1 } },
  "10": { "class_type": "LoadImage", "inputs": { "image": "%image%" } }
}
```

Notes:

- Placeholders must be written **inside a JSON string** (e.g. `"seed": "%seed%"`); after replacement, numeric fields are in string form and ComfyUI converts them.
- Reference images are only uploaded if the workflow actually contains `%image%`.
- The workflow must be in ComfyUI's **API format** (Save (API Format)).
- After modifying or adding a workflow, call `/reload_workflows` to apply it.

## API

### `POST /v1/images/generations` and `POST /v1/images/edits`

Both paths (as well as `/images/generations` and `/images/edits` without `/v1`) share the same handler; whether reference images are used depends only on whether the request carries images.

Both `application/json` and `multipart/form-data` are supported.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `model` | string | required | Model ID (the workflow file name without `-T2I` / `-I2I` and `.json`) |
| `prompt` | string | `"beautiful landscape"` | The prompt |
| `n` | integer | `1` | Number of images, from 1 to `MAX_N` |
| `size` | string | see below | `"1024x1024"` format or `"auto"` |
| `response_format` | string | `"b64_json"` | `"b64_json"` or `"url"` |
| `seed` | integer | random | Extension parameter of this service; incremented by 1 per image when `n > 1` |
| `output_format` | string | — | Only validated as `png` / `jpeg` / `webp`; **no conversion is done** (see below) |
| `image` / `images` / `reference_image(s)` | file, string, or array | — | Reference images, see below |

Other OpenAI parameters (`quality`, `background`, `moderation`, `style`, `user`, etc.) are currently ignored and do not cause errors.

**`size`**

- Format `"WxH"`; each side must be between `MIN_SIZE_SIDE` and `MAX_SIZE_SIDE`, otherwise a 400 is returned.
- `"auto"`: follows the aspect ratio of the first reference image (longest side capped at `AUTO_SIZE_MAX_SIDE`, snapped to multiples of 16); 1024×1024 when there is no reference image.
- When omitted: if the workflow uses a reference image and the request carries one → `auto`, otherwise `1024x1024`.

**Reference images**

- Accepted field names: `image`, `images`, `reference_image`, `reference_images` (including the `[]`-suffixed forms).
- JSON mode: a string (base64, data URL, http(s) URL), an array, or a `{"b64_json": "..."}` / `{"url": "..."}` / `{"image_url": "..."}` object.
- multipart mode: file fields, which may be repeated.
- Formats are limited to PNG, JPEG, WEBP, GIF, and BMP, with a size limit of `MAX_REF_IMAGE_BYTES`.

**`output_format`**

The `output_format` in the response reflects the format ComfyUI **actually produced** (detected from the file header), and the `Content-Type` in `url` mode is determined the same way. If the requested `output_format` differs from the actual one, only a warning is printed to the log; no conversion is done.

**Response**

```json
{
  "created": 1731052800,
  "data": [
    { "b64_json": "iVBORw0KGgo..." }
  ],
  "output_format": "png",
  "size": "1024x1024"
}
```

When `response_format` is `url`, each item in `data` is `{"url": "http://<host>/v1/images/<id>"}`.

### `GET /v1/images/{image_id}`

Serves images cached in `url` mode (`/images/{image_id}` also works). Images are stored in memory; they are removed once they exceed `IMAGE_CACHE_TTL_SECONDS` or the cache grows beyond `IMAGE_CACHE_MAX_SIZE`, and they disappear when the service restarts. Expired images return 404.

### `GET /v1/models`

Lists the currently loaded models (`/models` and `/v1` also work). Returns an empty list when no workflows are loaded.

### `GET` / `POST /reload_workflows`

Rescans `WORKFLOW_DIR`. Example response:

```json
{
  "status": "partial",
  "message": "Reloaded 2 model(s), but 1 issue(s) were found; check errors",
  "models": ["aaa", "foo"],
  "workflows": { "aaa": ["I2I", "T2I"], "foo": ["T2I"] },
  "errors": [
    { "file": "bad-T2I.json", "model": "bad", "kind": "T2I", "error": "Invalid JSON: ...", "line": 12, "column": 5 }
  ]
}
```

`status` is `success` (everything loaded), `partial` (some files had errors), or `error` (no model loaded at all).

## Usage examples

### curl: text-to-image

```bash
curl http://localhost:8000/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{
    "model": "aaa",
    "prompt": "a cat astronaut, studio lighting",
    "size": "1024x1024",
    "n": 2
  }'
```

### curl: reference-image generation (multipart, OpenAI edits style)

```bash
curl http://localhost:8000/v1/images/edits \
  -F model=aaa \
  -F prompt="turn this into a watercolor painting" \
  -F size=auto \
  -F image=@input.png
```

### curl: base64 reference image in JSON

```bash
curl http://localhost:8000/v1/images/edits \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"aaa\",\"prompt\":\"make it night\",\"image\":\"$(base64 -w0 input.png)\"}"
```

### OpenAI Python SDK

```python
import base64
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="not-needed")

result = client.images.generate(
    model="aaa",
    prompt="a cat astronaut, studio lighting",
    size="1024x1024",
    n=2,
)
for item in result.data:
    with open("out.png", "wb") as f:
        f.write(base64.b64decode(item.b64_json))
```

## Error format

All errors are returned in the OpenAI format:

```json
{
  "error": {
    "message": "Model 'zzz' not found (no matching workflow loaded)",
    "type": "invalid_request_error",
    "param": "model",
    "code": "model_not_found"
  }
}
```

| Status | Common causes |
| --- | --- |
| 400 | Missing `model`, invalid `size` / `n` / `output_format` / `response_format` value, reference image can't be downloaded or decoded, unsupported format |
| 404 | Model not found, `url`-mode image expired, path does not exist |
| 413 | Reference image exceeds `MAX_REF_IMAGE_BYTES` |
| 502 | Can't reach ComfyUI, ComfyUI rejected the workflow, reference image upload failed |
| 504 | Timed out waiting for ComfyUI to generate |
| 500 | Unexpected error (see the service log for details) |

## Known limitations

- **No authentication**: anyone who can reach this service can generate images and call `/reload_workflows`. When exposing it publicly, put a reverse proxy with authentication in front of it, or restrict it to an internal network.
- **No streaming** (`stream`, `partial_images`).
- **No `mask` (inpainting)** and **no `/images/variations`**.
- **No format conversion**: the output format depends on the workflow's save node.
- **ComfyUI execution failures are not reported immediately**: the service only polls for results, so if a workflow fails during execution it waits until the timeout and then returns 504; check ComfyUI's log for the actual cause.
- **`url`-mode images live only in memory**: they vanish on restart, and it is not suitable for multiple workers (each process has its own cache).
- **`url`-mode URLs are derived from the request's Host**: behind a reverse proxy, make sure the proxy forwards the Host and protocol information correctly (e.g. uvicorn's `--proxy-headers`), otherwise the returned URLs may be wrong.

## Troubleshooting

| Symptom | Possible cause and fix |
| --- | --- |
| `/v1/models` returns an empty list | `WORKFLOW_DIR` doesn't exist or has no `.json` files; call `/reload_workflows` and check `errors` |
| `404 model_not_found` | `model` must equal the workflow file name minus `-T2I` / `-I2I` and `.json`; mind the case |
| `502 ComfyUI rejected the workflow` | The workflow isn't in API format, or a custom node or model file is missing; the error message contains ComfyUI's original response |
| Always waits until `504` | ComfyUI failed to run the workflow or the queue is too long; check ComfyUI's log, or raise `GENERATION_TIMEOUT_SECONDS` |
| Reference image sent but has no effect | The model only has a T2I workflow, or the workflow has no `%image%` placeholder |
| `url`-mode URLs are unreachable | The reverse proxy isn't forwarding the correct Host / protocol, see "Known limitations" |
