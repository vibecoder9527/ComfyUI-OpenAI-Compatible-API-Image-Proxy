# ComfyUI OpenAI-Compatible API Image Proxy
[English](README.md) | [繁體中文](README.zh-TW.md)
將 ComfyUI 的 API 格式轉成 **OpenAI 相容的圖片生成 API**。任何支援 OpenAI `images/generations` / `images/edits` 的前端或 SDK，都可以直接把 Base URL 指向這個服務，用你自己的 ComfyUI workflow 生圖。

## 功能特色

- **OpenAI 相容端點**：`/v1/images/generations`、`/v1/images/edits`、`/v1/models`
- **一個模型 = 一組 workflow**：把 workflow JSON 放進資料夾，檔名就是模型 ID，不需要改程式
- **自動 T2I / I2I 路由**：同一模型可同時提供文生圖與參照圖生圖，依請求是否帶圖自動選擇
- **多張參照圖**：支援 multipart 檔案、base64、data URL、http(s) URL
- **一次多張**：`n` 參數（1–10），每張使用不同 seed
- **兩種回傳格式**：`b64_json`（預設）或 `url`（暫存於記憶體，可自動過期）
- **`size=auto`**：沿用參照圖的比例
- **OpenAI 格式的錯誤回應**：SDK 與前端可直接解析 `error.message`
- **熱重載**：呼叫 `/reload_workflows` 即可重新載入 workflow，不必重啟，並回報每個檔案的載入錯誤

## 運作方式

```
前端 / SDK ──(OpenAI-Compatible API)──▶ 本服務 ──(替換 placeholder)──▶ ComfyUI
                                           │                              │
                                           └──────── 輪詢結果、回傳圖片 ◀──┘
```

1. 依 `model` 與是否帶參照圖，選出對應的 workflow
2. 把 workflow 中的 placeholder（`%prompt%`、`%width%` 等）替換成請求內容
3. 若有參照圖，先上傳到 ComfyUI 的 input 目錄
4. 送進 ComfyUI 佇列，輪詢 `/history` 取得輸出圖片
5. 以 OpenAI 格式回傳

## 快速開始

### 需求

- Python 3.10+
- 可連線的 ComfyUI 服務
- 套件：`fastapi`、`uvicorn`、`httpx`、`python-multipart`（multipart 上傳需要）

### 直接執行

```bash
pip install fastapi uvicorn httpx python-multipart

export COMFYUI_URL=http://127.0.0.1:8188
export WORKFLOW_DIR=./workflows

uvicorn app:app --host 0.0.0.0 --port 8000
```

### Docker Compose 範例

以下只是範例，請依你的環境調整 image、路徑與埠號：

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
      - ./workflows:/app/workflows   # workflow 放在這裡，可用 /reload_workflows 熱重載
```

## 設定（環境變數）

| 變數 | 預設值 | 說明 |
| --- | --- | --- |
| `COMFYUI_URL` | `http://comfyui:8188` | ComfyUI 的位址 |
| `WORKFLOW_DIR` | `/app/workflows` | workflow JSON 所在資料夾 |
| `MAX_N` | `10` | 單次請求最多生成張數 |
| `MIN_SIZE_SIDE` | `64` | `size` 單邊下限 |
| `MAX_SIZE_SIDE` | `4096` | `size` 單邊上限 |
| `AUTO_SIZE_MAX_SIDE` | `1536` | `size=auto` 時，沿用參照圖尺寸的最長邊上限 |
| `GENERATION_TIMEOUT_SECONDS` | `180` | 每張圖的等待上限（秒）；n 張時總上限為此值 × n |
| `MAX_REF_IMAGE_BYTES` | `20971520`（20MB） | 單張參照圖大小上限 |
| `IMAGE_CACHE_MAX_SIZE` | `100` | `url` 模式最多暫存幾張圖 |
| `IMAGE_CACHE_TTL_SECONDS` | `600` | `url` 模式圖片的保存時間（秒） |

## Workflow 設定

### 檔名規則

啟動時會掃描 `WORKFLOW_DIR` 內所有 `.json` 檔：

| 檔名 | 模型 ID | 用途 |
| --- | --- | --- |
| `aaa-T2I.json` | `aaa` | 文字生圖 |
| `aaa-I2I.json` | `aaa` | 參照圖生圖 |
| `foo.json` | `foo` | 無後綴，視為只有 T2I |

`aaa-T2I.json` 與 `aaa-I2I.json` 對前端是**同一個模型**，`/v1/models` 只會列出一次 `aaa`。

### 路由規則

| 該模型有的 workflow | 請求有帶參照圖 | 請求沒帶參照圖 |
| --- | --- | --- |
| T2I + I2I | 使用 I2I | 使用 T2I |
| 只有 T2I | 使用 T2I（參照圖被忽略） | 使用 T2I |
| 只有 I2I | 使用 I2I | 使用 I2I，參照圖位置以 1×1 白色圖補上 |

找不到模型回 `404 model_not_found`，沒帶 `model` 回 `400 missing_required_parameter`。

### Placeholder

在 workflow JSON 中寫入下列字串，請求時會被替換：

| Placeholder | 替換成 |
| --- | --- |
| `%prompt%` | 請求的 `prompt`（已做 JSON 跳脫） |
| `%width%` / `%height%` | 由 `size` 解析出的寬、高 |
| `%seed%` | seed（請求有帶 `seed` 就用它，否則隨機；`n > 1` 時依序 +1） |
| `%image%`（等同 `%image1%`） | 第 1 張參照圖上傳到 ComfyUI 後的檔名 |
| `%image2%`、`%image3%` … | 第 2、3 … 張參照圖 |

注意事項：

- Placeholder 必須寫在 **JSON 字串內**（例如 `"seed": "%seed%"`），替換後數值欄位會是字串形式，由 ComfyUI 轉型。
- 參照圖只有在 workflow 真的含有 `%image%` 時才會上傳。
- workflow 需要是 ComfyUI 的 **API 格式**（Save (API Format)）。
- 修改或新增 workflow 後呼叫 `/reload_workflows` 即可生效。

## API

### `POST /v1/images/generations` 與 `POST /v1/images/edits`

兩個路徑（以及不帶 `/v1` 的 `/images/generations`、`/images/edits`）共用同一個處理邏輯，是否使用參照圖只看請求有沒有帶圖。

支援 `application/json` 與 `multipart/form-data`。

| 參數 | 類型 | 預設 | 說明 |
| --- | --- | --- | --- |
| `model` | string | 必填 | 模型 ID（即 workflow 檔名，不含 `-T2I` / `-I2I` 與 `.json`） |
| `prompt` | string | `"beautiful landscape"` | 提示詞 |
| `n` | integer | `1` | 生成張數，1 到 `MAX_N` |
| `size` | string | 見下方 | `"1024x1024"` 格式或 `"auto"` |
| `response_format` | string | `"b64_json"` | `"b64_json"` 或 `"url"` |
| `seed` | integer | 隨機 | 本服務的擴充參數，`n > 1` 時依序 +1 |
| `output_format` | string | — | 只驗證是否為 `png` / `jpeg` / `webp`，**不會轉檔**（見下方說明） |
| `image` / `images` / `reference_image(s)` | 檔案、字串或陣列 | — | 參照圖，見下方說明 |

其他 OpenAI 參數（`quality`、`background`、`moderation`、`style`、`user` 等）目前會被忽略，不會報錯。

**`size`**

- 格式 `"WxH"`，單邊需介於 `MIN_SIZE_SIDE` 與 `MAX_SIZE_SIDE` 之間，否則回 400。
- `"auto"`：沿用第一張參照圖的比例（最長邊不超過 `AUTO_SIZE_MAX_SIDE`，並對齊 16 的倍數）；沒有參照圖時為 1024×1024。
- 未指定時：workflow 有用到參照圖且請求有帶圖 → `auto`，否則 `1024x1024`。

**參照圖**

- 欄位名稱可用：`image`、`images`、`reference_image`、`reference_images`（含 `[]` 結尾的寫法）。
- JSON 模式：字串（base64、data URL、http(s) URL）、陣列，或 `{"b64_json": "..."}` / `{"url": "..."}` / `{"image_url": "..."}` 物件。
- multipart 模式：檔案欄位，可重複多個。
- 格式限 PNG、JPEG、WEBP、GIF、BMP，大小上限 `MAX_REF_IMAGE_BYTES`。

**`output_format`**

回應中的 `output_format` 反映 ComfyUI **實際輸出**的格式（依檔頭判斷），`url` 模式的 `Content-Type` 也依此決定。請求的 `output_format` 與實際不符時只會在 log 印出警告，不會轉檔。

**回應**

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

`response_format` 為 `url` 時，`data` 內容為 `{"url": "http://<host>/v1/images/<id>"}`。

### `GET /v1/images/{image_id}`

提供 `url` 模式暫存的圖片（也可用 `/images/{image_id}`）。圖片存在記憶體，超過 `IMAGE_CACHE_TTL_SECONDS` 或超出 `IMAGE_CACHE_MAX_SIZE` 會被清除，重啟服務後也會消失，過期回 404。

### `GET /v1/models`

列出目前載入的模型（也可用 `/models`、`/v1`）。沒有任何 workflow 時回傳空清單。

### `GET` / `POST /reload_workflows`

重新掃描 `WORKFLOW_DIR`。回應範例：

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

`status` 為 `success`（全部成功）、`partial`（有部分檔案出錯）或 `error`（一個模型都沒載入）。

## 使用範例

### curl：文生圖

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

### curl：參照圖生圖（multipart，OpenAI edits 風格）

```bash
curl http://localhost:8000/v1/images/edits \
  -F model=aaa \
  -F prompt="turn this into a watercolor painting" \
  -F size=auto \
  -F image=@input.png
```

### curl：JSON 傳 base64 參照圖

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

## 錯誤格式

所有錯誤都以 OpenAI 的格式回傳：

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

| 狀態碼 | 常見原因 |
| --- | --- |
| 400 | 缺少 `model`、`size` / `n` / `output_format` / `response_format` 值不合法、參照圖無法下載或解碼、格式不支援 |
| 404 | 找不到模型、`url` 模式的圖片已過期、路徑不存在 |
| 413 | 參照圖超過 `MAX_REF_IMAGE_BYTES` |
| 502 | 連不上 ComfyUI、ComfyUI 拒絕該 workflow、參照圖上傳失敗 |
| 504 | 等待 ComfyUI 生成逾時 |
| 500 | 未預期的錯誤（細節請看服務 log） |

## 已知限制

- **沒有身份驗證**：任何人連得到這個服務就能生圖，也能呼叫 `/reload_workflows`。對外開放時請在前面加上反向代理做驗證，或限制在內網。
- **不支援串流**（`stream`、`partial_images`）。
- **不支援`mask`（局部重繪）**、**`/images/variations`**。
- **不做格式轉換**：輸出格式取決於 workflow 的儲存節點。
- **ComfyUI 執行失敗不會立即回報**：服務只會輪詢結果，workflow 執行出錯時會等到逾時才回 504，詳細原因請看 ComfyUI 的 log。
- **`url` 模式的圖片只存在記憶體**：重啟即消失，也不適合多個 worker 並行（每個 process 各有一份快取）。
- **`url` 模式的網址取自請求的 Host**：若在反向代理後面，請確認代理有正確傳遞 Host 與協定資訊（例如 uvicorn 的 `--proxy-headers`），否則回傳的網址可能不正確。

## 疑難排解

| 症狀 | 可能原因與處理 |
| --- | --- |
| `/v1/models` 回傳空清單 | `WORKFLOW_DIR` 不存在或沒有 `.json` 檔；呼叫 `/reload_workflows` 查看 `errors` |
| `404 model_not_found` | `model` 要等於 workflow 檔名去掉 `-T2I` / `-I2I` 與 `.json` 的部分，注意大小寫 |
| `502 ComfyUI rejected the workflow` | workflow 不是 API 格式、缺少自訂節點或模型檔；錯誤訊息內有 ComfyUI 的原始回應 |
| 一直等到 `504` | ComfyUI 執行失敗或佇列太長；檢查 ComfyUI log，或調高 `GENERATION_TIMEOUT_SECONDS` |
| 帶了參照圖但沒效果 | 該模型只有 T2I workflow，或 workflow 內沒有 `%image%` placeholder |
| `url` 模式的網址連不到 | 反向代理沒有傳遞正確的 Host / 協定，見「已知限制」 |
