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

# 臨時圖片快取設定（用於 response_format=url）
# key: image_id, value: {"content": bytes, "created": datetime, "content_type": str}
IMAGE_CACHE: OrderedDict[str, dict] = OrderedDict()
IMAGE_CACHE_MAX_SIZE = int(os.getenv("IMAGE_CACHE_MAX_SIZE", "100"))  # 最多保留幾張
IMAGE_CACHE_TTL_SECONDS = int(os.getenv("IMAGE_CACHE_TTL_SECONDS", "600")) # Cache expire time

# 參照圖片大小上限（bytes），預設 20MB
MAX_REF_IMAGE_BYTES = int(os.getenv("MAX_REF_IMAGE_BYTES", str(20 * 1024 * 1024)))

# 生成參數限制
MAX_N = int(os.getenv("MAX_N", "10"))                                      # 單次請求最多張數
MIN_SIZE_SIDE = int(os.getenv("MIN_SIZE_SIDE", "64"))                      # size 單邊下限
MAX_SIZE_SIDE = int(os.getenv("MAX_SIZE_SIDE", "4096"))                    # size 單邊上限
AUTO_SIZE_MAX_SIDE = int(os.getenv("AUTO_SIZE_MAX_SIDE", "1536"))          # size=auto 時，沿用參照圖尺寸的最長邊上限
GENERATION_TIMEOUT_SECONDS = int(os.getenv("GENERATION_TIMEOUT_SECONDS", "180"))  # 每張圖的等待上限


# ==================== OpenAI 格式的錯誤回應 ====================
# OpenAI SDK 與多數前端會讀 {"error": {"message", "type", "param", "code"}}，
# 而 FastAPI 預設是 {"detail": ...}，所以統一轉成 OpenAI 格式。
def api_error(status_code: int, message: str, param: Optional[str] = None, code: Optional[str] = None) -> HTTPException:
    """建立帶有 param / code 的 HTTPException，由下方 handler 轉成 OpenAI 錯誤格式"""
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
    print(f"❌ 未處理的例外: {type(exc).__name__}: {exc}")
    return _error_response(500, "Internal server error")
# ============================================================

# ==================== 模型對應 Workflow 配置 ====================
# 自動掃描 /app/workflows 目錄中的所有 .json 檔案
#
# 命名規則：
#   aaa-T2I.json  文字生圖 workflow
#   aaa-I2I.json  參照圖生圖 workflow
# 兩者對前端都視為同一個模型 "aaa"，由程式依請求是否帶參照圖片自動路由。
# 沒有 -T2I / -I2I 後綴的舊式檔案（例如 foo.json），模型 ID 為檔名本身，視為只有 T2I。
#
# Workflows 內可用的 placeholder：
#   %prompt% %width% %height% %seed%
#   %image%   第 1 張參照圖（上傳到 ComfyUI 後的檔名，填在 LoadImage 節點的 image 欄位）
#   %image2%  第 2 張參照圖，依此類推（%image1% 等同 %image%）
WORKFLOW_DIR = os.getenv("WORKFLOW_DIR", "/app/workflows")

# 結構：{ model_id: {"T2I": workflow_dict, "I2I": workflow_dict} }（兩種不一定都有）
preloaded_workflows: Dict[str, Dict[str, dict]] = {}

WORKFLOW_SUFFIX_RE = re.compile(r"^(?P<model>.+)-(?P<kind>T2I|I2I)$", re.IGNORECASE)


def parse_workflow_name(stem: str) -> Tuple[str, str]:
    """把檔名（不含 .json）拆成 (model_id, kind)。無後綴視為 T2I。"""
    m = WORKFLOW_SUFFIX_RE.match(stem)
    if m:
        return m.group("model"), m.group("kind").upper()
    return stem, "T2I"


def load_workflows() -> Tuple[int, List[dict]]:
    """
    重新載入所有 workflow。
    回傳 (模型數量, 錯誤清單)；錯誤清單每項為 {"file", "error", ...}，
    讓呼叫端（例如 /reload_workflows）可以把錯誤一併回傳給前端，而不是只印在 log。
    """
    global preloaded_workflows
    loaded: Dict[str, Dict[str, dict]] = {}
    errors: List[dict] = []
    if os.path.exists(WORKFLOW_DIR) and os.path.isdir(WORKFLOW_DIR):
        for filename in sorted(os.listdir(WORKFLOW_DIR)):
            if not filename.endswith(".json"):
                continue
            stem = filename[:-5]  # 移除 .json
            model_id, kind = parse_workflow_name(stem)
            wf_path = os.path.join(WORKFLOW_DIR, filename)
            try:
                with open(wf_path, "r", encoding="utf-8") as f:
                    wf = json.load(f)
            except json.JSONDecodeError as e:
                print(f"❌ 載入 workflow 失敗 {model_id}/{kind} ({wf_path}): {e}")
                errors.append({
                    "file": filename,
                    "model": model_id,
                    "kind": kind,
                    "error": f"JSON 格式錯誤: {e.msg}",
                    "line": e.lineno,
                    "column": e.colno,
                })
                continue
            except Exception as e:
                print(f"❌ 載入 workflow 失敗 {model_id}/{kind} ({wf_path}): {e}")
                errors.append({
                    "file": filename,
                    "model": model_id,
                    "kind": kind,
                    "error": f"{type(e).__name__}: {e}",
                })
                continue
            variants = loaded.setdefault(model_id, {})
            if kind in variants:
                msg = f"模型 {model_id} 的 {kind} workflow 重複，後者 ({filename}) 覆蓋前者"
                print(f"⚠️ {msg}")
                errors.append({"file": filename, "model": model_id, "kind": kind, "error": msg})
            variants[kind] = wf
            print(f"✅ 已載入模型 {model_id} 的 {kind} workflow: {wf_path}")
    else:
        msg = f"WORKFLOW_DIR {WORKFLOW_DIR} 不存在"
        print(f"⚠️ 警告: {msg}")
        errors.append({"file": None, "error": msg})
    preloaded_workflows = loaded
    return len(preloaded_workflows), errors

# 啟動時載入一次
load_workflows()
# ============================================================


def select_workflow(model: Optional[str], has_image: bool) -> Tuple[dict, str]:
    """
    依模型與是否帶參照圖片選擇 workflow，回傳 (workflow, 說明文字)。

    - 兩種都有：有圖 → I2I，沒圖 → T2I
    - 只有 T2I：一律用 T2I，參照圖會被忽略
    - 只有 I2I：一律用 I2I，沒圖時由呼叫端以 1x1 空白圖補位
    - 未指定或找不到模型：回傳 400 / 404 錯誤
    """
    variants = preloaded_workflows.get(model) if model else None
    if variants:
        if has_image and "I2I" in variants:
            return variants["I2I"], f"{model} (I2I)"
        if "T2I" in variants:
            note = f"{model} (T2I，已忽略參照圖)" if has_image else f"{model} (T2I)"
            return variants["T2I"], note
        note = f"{model} (僅 I2I，使用參照圖)" if has_image else f"{model} (僅 I2I，使用 1x1 空白圖)"
        return variants["I2I"], note
    if model:
        raise api_error(404, f"Model '{model}' not found (no matching workflow loaded)", param="model", code="model_not_found")
    raise api_error(400, "'model' is required", param="model", code="missing_required_parameter")


# ==================== 參照圖片處理 ====================
def make_blank_png(r: int = 255, g: int = 255, b: int = 255) -> bytes:
    """用標準函式庫產生 1x1 純色 PNG（預設白色）"""
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
BLANK_FILENAME = "proxy_blank_1x1.png"  # 固定檔名並覆蓋上傳，避免在 ComfyUI input 目錄累積

IMAGE_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "bmp": "image/bmp",
}


def sniff_ext(data: bytes) -> Optional[str]:
    """依檔頭判斷圖片格式，無法辨識回傳 None"""
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
    """用標準函式庫從檔頭讀出圖片 (寬, 高)，無法判斷回傳 None"""
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
                if marker == 0xFF:  # 填充位元組
                    i += 1
                    continue
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD9:  # 沒有長度欄位的 marker
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


# ==================== 請求參數驗證 ====================
SIZE_RE = re.compile(r"^(\d+)x(\d+)$")


def parse_size(size: str) -> Tuple[int, int]:
    """解析 'WxH'；格式錯誤或超出範圍回傳 400"""
    m = SIZE_RE.match(size)
    if not m:
        raise api_error(400, f"Invalid size '{size}'. Use 'WIDTHxHEIGHT' (e.g. '1024x1024') or 'auto'.", param="size", code="invalid_value")
    width, height = int(m.group(1)), int(m.group(2))
    if not (MIN_SIZE_SIDE <= width <= MAX_SIZE_SIDE and MIN_SIZE_SIDE <= height <= MAX_SIZE_SIDE):
        raise api_error(400, f"size must be between {MIN_SIZE_SIDE}x{MIN_SIZE_SIDE} and {MAX_SIZE_SIDE}x{MAX_SIZE_SIDE}", param="size", code="invalid_value")
    return width, height


def auto_size(ref_dims: Optional[Tuple[int, int]]) -> Tuple[int, int]:
    """size=auto：有參照圖就沿用其比例（最長邊不超過 AUTO_SIZE_MAX_SIDE，對齊 16 的倍數），否則 1024x1024"""
    if not ref_dims or min(ref_dims) <= 0:
        return 1024, 1024
    w, h = ref_dims
    scale = min(1.0, AUTO_SIZE_MAX_SIDE / max(w, h))
    snap = lambda v: max(MIN_SIZE_SIDE, round(v * scale / 16) * 16)
    return snap(w), snap(h)


def parse_n(value) -> int:
    """解析 n（張數），預設 1"""
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
    """解析 output_format（png / jpeg / webp）；未指定回傳 None"""
    if value is None or value == "":
        return None
    fmt = str(value).strip().lower()
    if fmt == "jpg":
        fmt = "jpeg"
    if fmt not in ("png", "jpeg", "webp"):
        raise api_error(400, "output_format must be one of 'png', 'jpeg', 'webp'", param="output_format", code="invalid_value")
    return fmt
# ============================================================


# 前端可能使用的參照圖欄位名稱
IMAGE_FIELD_NAMES = (
    "image", "image[]",
    "images", "images[]",
    "reference_image", "reference_image[]",
    "reference_images", "reference_images[]",
)

RefItem = Union[bytes, str]  # bytes: multipart 上傳的檔案；str: base64 / data URL / http(s) URL


async def _append_ref(refs: List[RefItem], item) -> None:
    """把單一欄位值（檔案、字串、dict、list）攤平加入 refs，空值會被略過"""
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
        # 容許 {"b64_json": "..."} / {"url": "..."} / {"image_url": "..."} / {"image_url": {"url": "..."}}
        item = item.get("b64_json") or item.get("url") or item.get("image_url")
        if isinstance(item, dict):
            item = item.get("url")
    if isinstance(item, str) and item.strip():
        refs.append(item.strip())


async def parse_request(request: Request) -> Tuple[dict, List[RefItem]]:
    """
    同時支援：
      - application/json：參數放在 JSON，參照圖放在 image / images 等欄位（base64、data URL 或 URL，可為字串或陣列）
      - multipart/form-data：OpenAI /images/edits 風格，參照圖為檔案欄位 image（可多個）
    回傳 (參數 dict, 參照圖清單)
    """
    content_type = request.headers.get("content-type", "").lower()
    refs: List[RefItem] = []

    if content_type.startswith("multipart/form-data") or content_type.startswith("application/x-www-form-urlencoded"):
        form = await request.form()  # multipart 需要安裝 python-multipart
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
    """把參照圖項目轉成圖片 bytes（必要時下載或 base64 解碼），並檢查格式與大小"""
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
    """上傳圖片到 ComfyUI 的 input 目錄，回傳可填入 LoadImage 的檔名"""
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
    把 workflow 字串中的 %image% / %imageN% 換成上傳後的檔名。
    只有 workflow 真的用到才會上傳；前端圖片不足的位置以 1x1 空白圖補上。
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
    """清除過期或超量的快取圖片"""
    now = datetime.utcnow()
    # 先清過期
    expired_keys = [
        k for k, v in IMAGE_CACHE.items()
        if (now - v["created"]).total_seconds() > IMAGE_CACHE_TTL_SECONDS
    ]
    for k in expired_keys:
        IMAGE_CACHE.pop(k, None)

    # 再清超量（FIFO）
    while len(IMAGE_CACHE) > IMAGE_CACHE_MAX_SIZE:
        IMAGE_CACHE.popitem(last=False)


def store_image(image_bytes: bytes, content_type: str = "image/png") -> str:
    """把圖片存入快取，回傳 image_id"""
    cleanup_image_cache()
    image_id = str(uuid.uuid4())
    IMAGE_CACHE[image_id] = {
        "content": image_bytes,
        "created": datetime.utcnow(),
        "content_type": content_type,
    }
    return image_id


# ==================== 提供圖片的端點（給 response_format=url 使用） ====================
@app.get("/v1/images/{image_id}")
@app.get("/images/{image_id}")
async def get_image(image_id: str):
    cleanup_image_cache()
    item = IMAGE_CACHE.get(image_id)
    if not item:
        raise HTTPException(status_code=404, detail="Image not found or expired")
    return Response(content=item["content"], media_type=item["content_type"])


# ==================== /v1/models 端點 ====================
@app.get("/v1/models")
@app.get("/models")
@app.get("/v1")
async def list_models():
    models = []

    # 從預載入的 workflows 動態生成（同一模型的 T2I / I2I 只會出現一次）
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

# ==================== Reload Workflow 端點 ====================
@app.post("/reload_workflows")
@app.get("/reload_workflows")
async def reload_workflows():
    """重新載入 /app/workflow 目錄中的所有 workflow JSON"""
    count, errors = load_workflows()
    if errors:
        status = "partial" if count > 0 else "error"
        message = f"已重新載入 {count} 個模型，但有 {len(errors)} 個問題，請檢查 errors"
    else:
        status = "success"
        message = f"已重新載入 {count} 個模型"
    return {
        "status": status,
        "message": message,
        "models": list(preloaded_workflows.keys()),
        "workflows": {m: sorted(v.keys()) for m, v in preloaded_workflows.items()},
        "errors": errors,
    }
# ============================================================

async def poll_for_image(prompt_id: str, timeout: int = 180) -> bytes:
    """輪詢 ComfyUI，回傳原始圖片 bytes"""
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
    # 支援 response_format: "url" 或 "b64_json"
    # 預設維持原本行為 b64_json，避免破壞既有呼叫端
    response_format = body.get("response_format", "b64_json")
    if response_format not in ("url", "b64_json"):
        raise api_error(400, "response_format must be 'url' or 'b64_json'", param="response_format", code="invalid_value")

    n = parse_n(body.get("n"))
    requested_format = parse_output_format(body.get("output_format"))

    # === 根據 model 與是否帶參照圖選擇 workflow（T2I / I2I） ===
    workflow, wf_note = select_workflow(model, has_image=bool(refs))
    print(f"使用 workflow: {wf_note}，參照圖數量: {len(refs)}，n={n}")

    wf_str = json.dumps(workflow)
    # workflow 真的有用到 %image% 才會用到參照圖（T2I workflow 會忽略它）
    uses_ref = bool(refs) and bool(IMAGE_PLACEHOLDER_RE.search(wf_str))

    # size：未指定時，有用到參照圖 → auto（沿用參照圖尺寸），否則 1024x1024
    size_param = str(body.get("size") or "").strip().lower()
    if not size_param:
        size_param = "auto" if uses_ref else "1024x1024"
    width = height = None
    if size_param != "auto":
        width, height = parse_size(size_param)  # 格式錯誤直接回 400，不必先下載參照圖

    # 取得 seed：優先使用呼叫端傳入的，沒有就真正隨機
    seed = body.get("seed")
    if seed is None:
        seed = random.randint(0, 2**32 - 1)
    else:
        try:
            seed = int(seed) % (2**32)
        except (ValueError, TypeError):
            seed = random.randint(0, 2**32 - 1)

    async with httpx.AsyncClient(timeout=30) as client:
        # size=auto：沿用第一張參照圖的比例；解析後存回 refs[0]，避免後面重複下載
        if width is None:
            ref_dims = None
            if uses_ref:
                refs[0] = await resolve_ref_image(refs[0], client)
                ref_dims = image_size(refs[0])
            width, height = auto_size(ref_dims)

        # 替換 workflow 參數（placeholder 邏輯）
        # 先處理圖片（只上傳一次，n 張共用），prompt 放最後替換，避免 prompt 內容被當成 placeholder 再次替換
        wf_str = await fill_image_placeholders(wf_str, refs, client)
        wf_str = wf_str.replace("%width%", str(width))
        wf_str = wf_str.replace("%height%", str(height))
        escaped_prompt = json.dumps(prompt)[1:-1]

        # 一次把 n 個任務排進 ComfyUI 佇列，每張使用不同 seed（seed, seed+1, ...）
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

    # 組裝回傳內容：依實際檔頭決定格式與 content-type（ComfyUI 輸出不一定是 png）
    base = str(request.base_url).rstrip("/")  # 用當前請求的 base_url 組出可對外存取的 URL
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
        print(f"⚠️ 請求 output_format={requested_format}，但 workflow 實際輸出 {output_format}（proxy 不做轉檔）")

    return {
        "created": int(datetime.now().timestamp()),
        "data": data,
        "output_format": output_format,
        "size": f"{width}x{height}",
    }
