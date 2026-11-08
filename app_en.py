from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
import httpx
import json
import asyncio
import os
import re
import uuid
import base64
import random
import struct
import zlib
from typing import Dict, List, Optional, Tuple, Union
from datetime import datetime
from collections import OrderedDict

app = FastAPI()

COMFYUI_URL = os.getenv("COMFYUI_URL", "http://comfyui:8188")

# Image Cache Settings
# Temporary image cache (used by response_format=url)
# key: image_id, value: {"content": bytes, "created": datetime, "content_type": str}
IMAGE_CACHE: OrderedDict[str, dict] = OrderedDict()
IMAGE_CACHE_MAX_SIZE = int(os.getenv("IMAGE_CACHE_MAX_SIZE", "100"))  # Max number of cached images
IMAGE_CACHE_TTL_SECONDS = int(os.getenv("IMAGE_CACHE_TTL_SECONDS", "600"))  # Cache expiry time

# Max size of a reference image in bytes (default: 20MB)
MAX_REF_IMAGE_BYTES = int(os.getenv("MAX_REF_IMAGE_BYTES", str(20 * 1024 * 1024)))

# Generation parameter limits
MAX_N = int(os.getenv("MAX_N", "10"))                                      # Max images per request (OpenAI allows 10)
MIN_SIZE_SIDE = int(os.getenv("MIN_SIZE_SIDE", "64"))                      # Min length of each side of `size`
MAX_SIZE_SIDE = int(os.getenv("MAX_SIZE_SIDE", "4096"))                    # Max length of each side of `size`
AUTO_SIZE_MAX_SIDE = int(os.getenv("AUTO_SIZE_MAX_SIDE", "1536"))          # With size=auto, cap on the longest side when following the reference image
GENERATION_TIMEOUT_SECONDS = int(os.getenv("GENERATION_TIMEOUT_SECONDS", "180"))  # Max wait per image


# ==================== OpenAI-style error responses ====================
# The OpenAI SDK and most frontends read {"error": {"message", "type", "param", "code"}},
# while FastAPI defaults to {"detail": ...}, so every error is converted to the OpenAI format.
def api_error(status_code: int, message: str, param: Optional[str] = None, code: Optional[str] = None) -> HTTPException:
    """Build an HTTPException carrying param / code; the handler below turns it into an OpenAI-style error."""
    return HTTPException(status_code=status_code, detail={"message": message, "param": param, "code": code})


def _error_type(status_code: int) -> str:
    if status_code in (401, 403):
        return "authentication_error"
    if status_code == 429:
        return "rate_limit_error"
    if status_code >= 500:
        return "server_error"
    return "invalid_request_error"


def _error_response(status_code: int, message: str, param=None, code=None, headers=None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": _error_type(status_code), "param": param, "code": code}},
        headers=headers,
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    detail = exc.detail
    if isinstance(detail, dict) and "message" in detail:
        return _error_response(exc.status_code, str(detail["message"]), detail.get("param"), detail.get("code"), exc.headers)
    return _error_response(exc.status_code, str(detail), headers=exc.headers)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    print(f"❌ Unhandled exception: {type(exc).__name__}: {exc}")
    return _error_response(500, "Internal server error")
# ============================================================

# ==================== Model-to-workflow configuration ====================
# Automatically scans every .json file in the /app/workflows directory.
#
# Naming convention:
#   aaa-T2I.json  text-to-image workflow
#   aaa-I2I.json  reference-image (image-to-image) workflow
# Both are exposed to the frontend as the same model "aaa"; the program routes
# automatically depending on whether the request carries reference images.
# Legacy files without a -T2I / -I2I suffix (e.g. foo.json) use the file name as the
# model ID and are treated as T2I only.
#
# Placeholders available inside a workflow:
#   %prompt% %width% %height% %seed%
#   %image%   1st reference image (the file name after uploading to ComfyUI, for the image field of a LoadImage node)
#   %image2%  2nd reference image, and so on (%image1% is the same as %image%)
WORKFLOW_DIR = os.getenv("WORKFLOW_DIR", "/app/workflows")

# Structure: { model_id: {"T2I": workflow_dict, "I2I": workflow_dict} } (a model may have only one of the two)
preloaded_workflows: Dict[str, Dict[str, dict]] = {}

WORKFLOW_SUFFIX_RE = re.compile(r"^(?P<model>.+)-(?P<kind>T2I|I2I)$", re.IGNORECASE)


def parse_workflow_name(stem: str) -> Tuple[str, str]:
    """Split a file name (without .json) into (model_id, kind). No suffix means T2I."""
    m = WORKFLOW_SUFFIX_RE.match(stem)
    if m:
        return m.group("model"), m.group("kind").upper()
    return stem, "T2I"


def load_workflows() -> Tuple[int, List[dict]]:
    """
    Reload all workflows.
    Returns (number of models, list of errors); each error is {"file", "error", ...},
    so callers (e.g. /reload_workflows) can return the errors to the frontend
    instead of only printing them to the log.
    """
    global preloaded_workflows
    loaded: Dict[str, Dict[str, dict]] = {}
    errors: List[dict] = []
    if os.path.exists(WORKFLOW_DIR) and os.path.isdir(WORKFLOW_DIR):
        for filename in sorted(os.listdir(WORKFLOW_DIR)):
            if not filename.endswith(".json"):
                continue
            stem = filename[:-5]  # strip .json
            model_id, kind = parse_workflow_name(stem)
            wf_path = os.path.join(WORKFLOW_DIR, filename)
            try:
                with open(wf_path, "r", encoding="utf-8") as f:
                    wf = json.load(f)
            except json.JSONDecodeError as e:
                print(f"❌ Failed to load workflow {model_id}/{kind} ({wf_path}): {e}")
                errors.append({
                    "file": filename,
                    "model": model_id,
                    "kind": kind,
                    "error": f"Invalid JSON: {e.msg}",
                    "line": e.lineno,
                    "column": e.colno,
                })
                continue
            except Exception as e:
                print(f"❌ Failed to load workflow {model_id}/{kind} ({wf_path}): {e}")
                errors.append({
                    "file": filename,
                    "model": model_id,
                    "kind": kind,
                    "error": f"{type(e).__name__}: {e}",
                })
                continue
            variants = loaded.setdefault(model_id, {})
            if kind in variants:
                msg = f"Duplicate {kind} workflow for model {model_id}; the later file ({filename}) overrides the earlier one"
                print(f"⚠️ {msg}")
                errors.append({"file": filename, "model": model_id, "kind": kind, "error": msg})
            variants[kind] = wf
            print(f"✅ Loaded {kind} workflow for model {model_id}: {wf_path}")
    else:
        msg = f"WORKFLOW_DIR {WORKFLOW_DIR} does not exist"
        print(f"⚠️ Warning: {msg}")
        errors.append({"file": None, "error": msg})
    preloaded_workflows = loaded
    return len(preloaded_workflows), errors

# Load once at startup
load_workflows()
# ============================================================


def select_workflow(model: Optional[str], has_image: bool) -> Tuple[dict, str]:
    """
    Pick a workflow based on the model and whether reference images were sent;
    returns (workflow, description).

    - Both variants exist: with image -> I2I, without image -> T2I
    - T2I only: always T2I; reference images are ignored
    - I2I only: always I2I; if there is no image, the caller fills in a 1x1 blank image
    - Model missing or not found: raises a 400 / 404 error
    """
    variants = preloaded_workflows.get(model) if model else None
    if variants:
        if has_image and "I2I" in variants:
            return variants["I2I"], f"{model} (I2I)"
        if "T2I" in variants:
            note = f"{model} (T2I, reference images ignored)" if has_image else f"{model} (T2I)"
            return variants["T2I"], note
        note = f"{model} (I2I only, using reference image)" if has_image else f"{model} (I2I only, using 1x1 blank image)"
        return variants["I2I"], note
    if model:
        raise api_error(404, f"Model '{model}' not found (no matching workflow loaded)", param="model", code="model_not_found")
    raise api_error(400, "'model' is required", param="model", code="missing_required_parameter")


# ==================== Reference image handling ====================
def make_blank_png(r: int = 255, g: int = 255, b: int = 255) -> bytes:
    """Generate a 1x1 solid-color PNG with the standard library (white by default)."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)  # 1x1, 8-bit, RGB
    raw = b"\x00" + bytes([r, g, b])  # filter byte + 1 pixel
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


BLANK_PNG = make_blank_png()
BLANK_FILENAME = "proxy_blank_1x1.png"  # Fixed name, overwritten on upload, so it doesn't pile up in ComfyUI's input directory

IMAGE_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "bmp": "image/bmp",
}


def sniff_ext(data: bytes) -> Optional[str]:
    """Detect the image format from the file header; returns None if unrecognized."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data.startswith(b"BM"):
        return "bmp"
    return None


def image_size(data: bytes) -> Optional[Tuple[int, int]]:
    """Read the image (width, height) from the file header using only the standard library; None if it can't be determined."""
    try:
        if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
            return struct.unpack(">II", data[16:24])
        if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
            return struct.unpack("<HH", data[6:10])
        if data.startswith(b"BM") and len(data) >= 26:
            w, h = struct.unpack("<ii", data[18:26])
            return abs(w), abs(h)
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            chunk = data[12:16]
            if chunk == b"VP8X" and len(data) >= 30:
                return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
            if chunk == b"VP8L" and len(data) >= 25:
                b = data[21:25]
                return 1 + (((b[1] & 0x3F) << 8) | b[0]), 1 + (((b[3] & 0x0F) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
            if chunk == b"VP8 " and len(data) >= 30:
                w, h = struct.unpack("<HH", data[26:30])
                return w & 0x3FFF, h & 0x3FFF
            return None
        if data.startswith(b"\xff\xd8\xff"):
            i, n = 2, len(data)
            while i + 9 < n:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker == 0xFF:  # padding byte
                    i += 1
                    continue
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD9:  # markers without a length field
                    i += 2
                    continue
                seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):  # SOFn
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                i += 2 + seg_len
    except Exception:
        pass
    return None


# ==================== Request parameter validation ====================
SIZE_RE = re.compile(r"^(\d+)x(\d+)$")


def parse_size(size: str) -> Tuple[int, int]:
    """Parse 'WxH'; raises 400 on a bad format or an out-of-range value."""
    m = SIZE_RE.match(size)
    if not m:
        raise api_error(400, f"Invalid size '{size}'. Use 'WIDTHxHEIGHT' (e.g. '1024x1024') or 'auto'.", param="size", code="invalid_value")
    width, height = int(m.group(1)), int(m.group(2))
    if not (MIN_SIZE_SIDE <= width <= MAX_SIZE_SIDE and MIN_SIZE_SIDE <= height <= MAX_SIZE_SIDE):
        raise api_error(400, f"size must be between {MIN_SIZE_SIDE}x{MIN_SIZE_SIDE} and {MAX_SIZE_SIDE}x{MAX_SIZE_SIDE}", param="size", code="invalid_value")
    return width, height


def auto_size(ref_dims: Optional[Tuple[int, int]]) -> Tuple[int, int]:
    """size=auto: follow the reference image's aspect ratio (longest side capped at AUTO_SIZE_MAX_SIDE, snapped to multiples of 16); otherwise 1024x1024."""
    if not ref_dims or min(ref_dims) <= 0:
        return 1024, 1024
    w, h = ref_dims
    scale = min(1.0, AUTO_SIZE_MAX_SIDE / max(w, h))
    snap = lambda v: max(MIN_SIZE_SIDE, round(v * scale / 16) * 16)
    return snap(w), snap(h)


def parse_n(value) -> int:
    """Parse n (number of images); defaults to 1."""
    if value is None or value == "":
        return 1
    try:
        n = int(value)
    except (ValueError, TypeError):
        raise api_error(400, "n must be an integer", param="n", code="invalid_type")
    if not 1 <= n <= MAX_N:
        raise api_error(400, f"n must be between 1 and {MAX_N}", param="n", code="invalid_value")
    return n


def parse_output_format(value) -> Optional[str]:
    """Parse output_format (png / jpeg / webp); returns None if not specified."""
    if value is None or value == "":
        return None
    fmt = str(value).strip().lower()
    if fmt == "jpg":
        fmt = "jpeg"
    if fmt not in ("png", "jpeg", "webp"):
        raise api_error(400, "output_format must be one of 'png', 'jpeg', 'webp'", param="output_format", code="invalid_value")
    return fmt
# ============================================================


# Field names a frontend may use for reference images
IMAGE_FIELD_NAMES = (
    "image", "image[]",
    "images", "images[]",
    "reference_image", "reference_image[]",
    "reference_images", "reference_images[]",
)

RefItem = Union[bytes, str]  # bytes: file uploaded via multipart; str: base64 / data URL / http(s) URL


async def _append_ref(refs: List[RefItem], item) -> None:
    """Flatten a single field value (file, string, dict, list) into refs; empty values are skipped."""
    if item is None:
        return
    if isinstance(item, (list, tuple)):
        for sub in item:
            await _append_ref(refs, sub)
        return
    if isinstance(item, StarletteUploadFile):
        data = await item.read()
        if data:
            refs.append(data)
        return
    if isinstance(item, dict):
        # Accepts {"b64_json": "..."} / {"url": "..."} / {"image_url": "..."} / {"image_url": {"url": "..."}}
        item = item.get("b64_json") or item.get("url") or item.get("image_url")
        if isinstance(item, dict):
            item = item.get("url")
    if isinstance(item, str) and item.strip():
        refs.append(item.strip())


async def parse_request(request: Request) -> Tuple[dict, List[RefItem]]:
    """
    Supports both:
      - application/json: parameters in the JSON body, reference images in fields such as image / images
        (base64, data URL or URL; a string or an array)
      - multipart/form-data: OpenAI /images/edits style, reference images as file fields named image (one or more)
    Returns (params dict, list of reference images)
    """
    content_type = request.headers.get("content-type", "").lower()
    refs: List[RefItem] = []

    if content_type.startswith("multipart/form-data") or content_type.startswith("application/x-www-form-urlencoded"):
        form = await request.form()  # multipart requires python-multipart to be installed
        params = {k: form.get(k) for k in form.keys() if k not in IMAGE_FIELD_NAMES}
        for key in IMAGE_FIELD_NAMES:
            for item in form.getlist(key):
                await _append_ref(refs, item)
    else:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Request body must be valid JSON or multipart/form-data")
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="JSON body must be an object")
        params = body
        for key in IMAGE_FIELD_NAMES:
            await _append_ref(refs, body.get(key))

    return params, refs


async def resolve_ref_image(item: RefItem, client: httpx.AsyncClient) -> bytes:
    """Turn a reference image item into image bytes (downloading or base64-decoding if needed) and validate its format and size."""
    if isinstance(item, bytes):
        data = item
    elif item.startswith(("http://", "https://")):
        try:
            r = await client.get(item, follow_redirects=True, timeout=30)
            r.raise_for_status()
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to download reference image: {e}")
        data = r.content
    else:
        b64 = item.partition(",")[2] if item.startswith("data:") else item
        b64 = "".join(b64.split())
        b64 += "=" * (-len(b64) % 4)
        try:
            data = base64.b64decode(b64)
        except Exception:
            raise HTTPException(status_code=400, detail="Reference image is not valid base64")

    if not data:
        raise HTTPException(status_code=400, detail="Reference image is empty")
    if len(data) > MAX_REF_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail=f"Reference image exceeds {MAX_REF_IMAGE_BYTES} bytes")
    if sniff_ext(data) is None:
        raise HTTPException(status_code=400, detail="Reference image must be PNG, JPEG, WEBP, GIF or BMP")
    return data


async def upload_to_comfyui(client: httpx.AsyncClient, image_bytes: bytes, filename: Optional[str] = None) -> str:
    """Upload an image to ComfyUI's input directory; returns the file name to put into a LoadImage node."""
    ext = sniff_ext(image_bytes) or "png"
    if filename is None:
        filename = f"ref_{uuid.uuid4().hex}.{ext}"
    try:
        resp = await client.post(
            f"{COMFYUI_URL}/upload/image",
            files={"image": (filename, image_bytes, IMAGE_MIME.get(ext, "image/png"))},
            data={"type": "input", "overwrite": "true"},
        )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Failed to reach ComfyUI for image upload: {e}")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"ComfyUI image upload failed: {resp.text[:300]}")
    info = resp.json()
    name = info.get("name", filename)
    subfolder = info.get("subfolder", "")
    return f"{subfolder}/{name}" if subfolder else name


IMAGE_PLACEHOLDER_RE = re.compile(r"%image(\d*)%")


async def fill_image_placeholders(wf_str: str, refs: List[RefItem], client: httpx.AsyncClient) -> str:
    """
    Replace %image% / %imageN% in the workflow string with the uploaded file names.
    Images are only uploaded if the workflow actually uses them; positions with no
    image from the frontend are filled with a 1x1 blank image.
    """
    indices = sorted({int(m.group(1) or 1) for m in IMAGE_PLACEHOLDER_RE.finditer(wf_str)})
    if not indices:
        return wf_str

    mapping: Dict[int, str] = {}
    for idx in indices:
        if 1 <= idx <= len(refs):
            data = await resolve_ref_image(refs[idx - 1], client)
            mapping[idx] = await upload_to_comfyui(client, data)
        else:
            mapping[idx] = await upload_to_comfyui(client, BLANK_PNG, filename=BLANK_FILENAME)

    def repl(m: re.Match) -> str:
        return json.dumps(mapping[int(m.group(1) or 1)])[1:-1]

    return IMAGE_PLACEHOLDER_RE.sub(repl, wf_str)
# ============================================================


def cleanup_image_cache():
    """Remove expired or excess cached images."""
    now = datetime.utcnow()
    # Remove expired entries first
    expired_keys = [
        k for k, v in IMAGE_CACHE.items()
        if (now - v["created"]).total_seconds() > IMAGE_CACHE_TTL_SECONDS
    ]
    for k in expired_keys:
        IMAGE_CACHE.pop(k, None)

    # Then trim the excess (FIFO)
    while len(IMAGE_CACHE) > IMAGE_CACHE_MAX_SIZE:
        IMAGE_CACHE.popitem(last=False)


def store_image(image_bytes: bytes, content_type: str = "image/png") -> str:
    """Store an image in the cache and return its image_id."""
    cleanup_image_cache()
    image_id = str(uuid.uuid4())
    IMAGE_CACHE[image_id] = {
        "content": image_bytes,
        "created": datetime.utcnow(),
        "content_type": content_type,
    }
    return image_id


# ==================== Endpoint serving images (for response_format=url) ====================
@app.get("/v1/images/{image_id}")
@app.get("/images/{image_id}")
async def get_image(image_id: str):
    cleanup_image_cache()
    item = IMAGE_CACHE.get(image_id)
    if not item:
        raise HTTPException(status_code=404, detail="Image not found or expired")
    return Response(content=item["content"], media_type=item["content_type"])


# ==================== /v1/models endpoint ====================
@app.get("/v1/models")
@app.get("/models")
@app.get("/v1")
async def list_models():
    models = []

    # Generated dynamically from the preloaded workflows (a model's T2I / I2I variants appear only once)
    for model_id in preloaded_workflows.keys():
        models.append({
            "id": model_id,
            "object": "model",
            "created": int(datetime.now().timestamp()),
            "owned_by": "comfyui-proxy",
            "capabilities": {"image_generation": True}
        })

    return {
        "object": "list",
        "data": models
    }
# ============================================================

# ==================== Reload workflows endpoint ====================
@app.post("/reload_workflows")
@app.get("/reload_workflows")
async def reload_workflows():
    """Reload every workflow JSON in the /app/workflow directory."""
    count, errors = load_workflows()
    if errors:
        status = "partial" if count > 0 else "error"
        message = f"Reloaded {count} model(s), but {len(errors)} issue(s) were found; check errors"
    else:
        status = "success"
        message = f"Reloaded {count} model(s)"
    return {
        "status": status,
        "message": message,
        "models": list(preloaded_workflows.keys()),
        "workflows": {m: sorted(v.keys()) for m, v in preloaded_workflows.items()},
        "errors": errors,
    }
# ============================================================

async def poll_for_image(prompt_id: str, timeout: int = 180) -> bytes:
    """Poll ComfyUI and return the raw image bytes."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        for _ in range(timeout):
            await asyncio.sleep(1)
            resp = await client.get(f"{COMFYUI_URL}/history/{prompt_id}")
            if resp.status_code != 200:
                continue
            history = resp.json()
            if prompt_id in history and history[prompt_id].get("outputs"):
                outputs = history[prompt_id]["outputs"]
                for node_id, output in outputs.items():
                    if "images" in output:
                        img = output["images"][0]
                        img_resp = await client.get(
                            f"{COMFYUI_URL}/view",
                            params={
                                "filename": img["filename"],
                                "subfolder": img.get("subfolder", ""),
                                "type": img.get("type", "output")
                            }
                        )
                        if img_resp.status_code == 200:
                            return img_resp.content
    raise TimeoutError("ComfyUI generation timeout")


@app.post("/images/generations")
@app.post("/v1/images/generations")
@app.post("/images/edits")
@app.post("/v1/images/edits")
async def generate_image(request: Request):
    body, refs = await parse_request(request)
    prompt = str(body.get("prompt", "beautiful landscape"))
    model = body.get("model")
    # Supports response_format: "url" or "b64_json"
    # Defaults to b64_json (the original behavior) so existing callers keep working
    response_format = body.get("response_format", "b64_json")
    if response_format not in ("url", "b64_json"):
        raise api_error(400, "response_format must be 'url' or 'b64_json'", param="response_format", code="invalid_value")

    n = parse_n(body.get("n"))
    requested_format = parse_output_format(body.get("output_format"))

    # === Pick the workflow (T2I / I2I) based on the model and whether reference images were sent ===
    workflow, wf_note = select_workflow(model, has_image=bool(refs))
    print(f"Using workflow: {wf_note}, reference images: {len(refs)}, n={n}")

    wf_str = json.dumps(workflow)
    # Reference images are only used if the workflow actually contains %image% (a T2I workflow ignores them)
    uses_ref = bool(refs) and bool(IMAGE_PLACEHOLDER_RE.search(wf_str))

    # size: when omitted, use auto (follow the reference image's size) if a reference image is used, otherwise 1024x1024
    size_param = str(body.get("size") or "").strip().lower()
    if not size_param:
        size_param = "auto" if uses_ref else "1024x1024"
    width = height = None
    if size_param != "auto":
        width, height = parse_size(size_param)  # Bad format returns 400 right away, before downloading any reference image

    # Seed: use the caller's value if given, otherwise a truly random one
    seed = body.get("seed")
    if seed is None:
        seed = random.randint(0, 2**32 - 1)
    else:
        try:
            seed = int(seed) % (2**32)
        except (ValueError, TypeError):
            seed = random.randint(0, 2**32 - 1)

    async with httpx.AsyncClient(timeout=30) as client:
        # size=auto: follow the first reference image's aspect ratio; the resolved bytes are stored back
        # into refs[0] so the image isn't downloaded again later
        if width is None:
            ref_dims = None
            if uses_ref:
                refs[0] = await resolve_ref_image(refs[0], client)
                ref_dims = image_size(refs[0])
            width, height = auto_size(ref_dims)

        # Substitute workflow parameters (placeholder logic)
        # Images first (uploaded once and shared by all n images); the prompt is substituted last so that
        # prompt text can't be mistaken for a placeholder and replaced again
        wf_str = await fill_image_placeholders(wf_str, refs, client)
        wf_str = wf_str.replace("%width%", str(width))
        wf_str = wf_str.replace("%height%", str(height))
        escaped_prompt = json.dumps(prompt)[1:-1]

        # Queue all n jobs in ComfyUI at once, each with a different seed (seed, seed+1, ...)
        prompt_ids: List[str] = []
        for i in range(n):
            seed_i = (seed + i) % (2**32)
            one_str = wf_str.replace("%seed%", str(seed_i)).replace("%prompt%", escaped_prompt)
            resolved_workflow = json.loads(one_str)
            try:
                queue_resp = await client.post(f"{COMFYUI_URL}/prompt", json={"prompt": resolved_workflow})
            except httpx.HTTPError as e:
                raise api_error(502, f"Failed to reach ComfyUI: {e}", code="upstream_unreachable")
            if queue_resp.status_code != 200:
                raise api_error(502, f"ComfyUI rejected the workflow: {queue_resp.text[:500]}", code="upstream_error")
            prompt_ids.append(queue_resp.json()["prompt_id"])

        try:
            images: List[bytes] = list(await asyncio.gather(*(
                poll_for_image(pid, timeout=GENERATION_TIMEOUT_SECONDS * n) for pid in prompt_ids
            )))
        except TimeoutError:
            raise api_error(504, "ComfyUI generation timeout", code="timeout")

    # Build the response: format and content-type follow the actual file header (ComfyUI output isn't always PNG)
    base = str(request.base_url).rstrip("/")  # Build a publicly reachable URL from the current request's base_url
    data = []
    ext = "png"
    for image_bytes in images:
        ext = sniff_ext(image_bytes) or "png"
        if response_format == "url":
            image_id = store_image(image_bytes, content_type=IMAGE_MIME.get(ext, "image/png"))
            data.append({"url": f"{base}/v1/images/{image_id}"})
        else:
            data.append({"b64_json": base64.b64encode(image_bytes).decode("utf-8")})

    output_format = "jpeg" if ext == "jpg" else ext
    if requested_format and requested_format != output_format:
        print(f"⚠️ Requested output_format={requested_format}, but the workflow actually produced {output_format} (the proxy does not convert formats)")

    return {
        "created": int(datetime.now().timestamp()),
        "data": data,
        "output_format": output_format,
        "size": f"{width}x{height}",
    }
