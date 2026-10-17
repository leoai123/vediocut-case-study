#!/usr/bin/env python3
"""視覺語意索引：用 CLIP 影像 embedding 做 B-roll 語意檢索（中英文字 → 畫面）。

動機
----
既有的 `shot_index.json` 是人工/關鍵詞標的子鏡頭索引，靠 A-roll 台詞字面
命中 keyword 來配 B-roll。缺點：(1) 需要人工標；(2) 只認得標過的詞，同義/換句話說
就漏掉；(3) 單一 contact_sheet 代表影格常常抓不到關鍵畫面（例：某支素材的 contact
sheet 只拍到地板，真正的主角在 1~6s）。

本模組改用「畫面本身」的視覺語意：對每個 asset 沿時間抽多張代表影格，用 CLIP 影像
編碼器算 embedding，建成可查詢索引。查詢時把文字（中/英）編碼成同一向量空間，做餘弦
相似度排序，回傳 `(assetId, [start,end], score)`——range 對齊到命中影格的來源時窗，
可直接餵給 broll_engine 當候選。

這是「自足新模組」：不改 broll_engine.py / render_p1.py / shot_index.json / edit_decision.json。
索引輸出寫到 analysis/p1/visual_index.json。整合方式見 analysis/p1/visual_retrieval_design.md。

後端
----
本機已裝 torch 2.12 + open_clip 3.3，且 LAION `ViT-B-32/laion2b_s34b_b79k` 權重已快取，
可完全離線跑。此權重為英文 CLIP，故中文查詢先經一層 CN→EN 詞庫（LEXICON）橋接；產品化
換多語 CLIP（SigLIP-2 / jina-clip-v2 / Chinese-CLIP）即可移除詞庫，見設計筆記。

用法
----
  python3 app/visual_index.py build --project myvlog   # 抽幀+算 embedding，寫 Project.visual_index
  python3 app/visual_index.py build --project myvlog --assets IMG_0001,IMG_0002
  python3 app/visual_index.py build --project myvlog --out /tmp/test.json   # 不覆蓋正式索引（測試用）
  python3 app/visual_index.py query --project myvlog "披薩"        # 單一查詢
  python3 app/visual_index.py query --project p1 "sashimi" --topk 5
  python3 app/visual_index.py demo --project p1                # 跑內建台詞查詢並印命中
  python3 app/visual_index.py emit-shotindex --project p1      # 從視覺索引產生 shot_index 相容候選（不覆蓋原檔）

程式介面
--------
  project = project_paths.load("myvlog")
  idx = VisualIndex.load(project.visual_index)
  idx.query("生魚片")  -> [("IMG_0001", [0.0, 3.0], 0.29), ...]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import project_paths                      # python3 app/X.py 直接執行（app/ 在 sys.path）
except ModuleNotFoundError:                   # 被以 app.X 匯入時（如 render_p1 匯入 music_beats）
    from app import project_paths

ROOT = Path(__file__).resolve().parents[1]
CONTACT = ROOT / "analysis" / "contact_sheets"


class VisualIndexError(RuntimeError):
    """索引建置失敗（0 資產 / 0 幀等），訊息要能指路，不可靜默寫出空索引。"""


# ── v1 模型設定（英文 CLIP + LEXICON 橋接；向後相容，預設）────────────────────
# 已離線快取於 ~/.cache/huggingface/hub/models--laion--CLIP-ViT-B-32-laion2B-s34B-b79K
MODEL_NAME = os.environ.get("VISUAL_INDEX_MODEL", "ViT-B-32")
PRETRAINED = os.environ.get("VISUAL_INDEX_PRETRAINED", "laion2b_s34b_b79k")
# 高負載機器預設用 CPU（避免與其他工作搶 MPS/GPU，且僅數十~數百張影格）；可用環境變數改 mps
DEVICE = os.environ.get("VISUAL_INDEX_DEVICE", "cpu")

# ── v2 多語模型設定（治本：中文 query 直接進多語/原生文字塔，移除 LEXICON 橋接）──
# 為何需要 v2：v1 是英文 ViT-B-32，中文得先過 LEXICON（CN→EN 詞庫）。p2 實測 15 段有
# 13 段詞庫零命中→原封中文餵英文 CLIP＝雜訊向量、cos 全擠 0.29~0.33 排序等同隨機、
# 8 段錯配（見 analysis/p2/broll_audit_report.md）。v2 讓中文原生進文字塔即根除。
#
# 兩個可選 v2 後端（皆為「影像塔＋文字塔同一向量空間」）：
#   cnclip : OFA-Sys/chinese-clip-vit-base-patch16 —— 原生中文 CLIP（200M 中文圖文對
#            訓練），單一模型含影像+文字塔，中文檢索最準、分離度最大（首選）。
#   stclip : sentence-transformers/clip-ViT-B-32-multilingual-v1 文字塔
#            + sentence-transformers/clip-ViT-B-32（= OpenAI CLIP ViT-B-32）影像塔。
#            多語蒸餾，可用但偏弱（實測 top1 命中率、分離度均明顯不如 cnclip）。
#
# ⚠ 關鍵：v2 的影像向量空間 ≠ v1 的 LAION 空間，既有 visual_index.json 的影像
# embedding「不可沿用」——實測 multilingual-v1 文字 × LAION 影像 cos 全落 0.04~0.12
# 雜訊帶、等同亂配（見 visual_v2_report.md 選型節）。故 v2 build 一律用 v2 影像塔
# 重算 embedding；為求乾淨對照與省時，可用 --reuse-frames 沿用既有索引的抽幀時間點
# （只重算 embedding、不重新取樣抽幀）。
V2_BACKEND = os.environ.get("VISUAL_INDEX_V2_BACKEND", "cnclip")
CN_CLIP_MODEL = os.environ.get("VISUAL_INDEX_CNCLIP", "OFA-Sys/chinese-clip-vit-base-patch16")
ST_IMAGE_MODEL = os.environ.get("VISUAL_INDEX_ST_IMAGE", "sentence-transformers/clip-ViT-B-32")
ST_TEXT_MODEL = os.environ.get("VISUAL_INDEX_ST_TEXT",
                               "sentence-transformers/clip-ViT-B-32-multilingual-v1")

# ── 抽幀設定 ────────────────────────────────────────────────────────────────
STRIDE_SEC = 4.0     # 目標每 N 秒一張代表影格
MAX_FRAMES = 8       # 每個 asset 上限（長 A-roll 也不會爆量）
MIN_FRAMES = 2
HALF_WIN = 1.5       # 回傳 range 以命中影格時間 ±HALF_WIN 為窗（留 padding，避免切到內容）
FRAME_SCALE = 384    # 抽幀縮圖寬度（CLIP 內部會再 resize/crop，縮小可省 IO 與解碼）
SKIP_START_SEC = 1.0 # 避開每支 clip 開頭這幾秒再取樣（手持開機晃動糊幀；見 p2 a01@0.3 實例）

# ── 中→英 領域詞庫（英文 CLIP 的橋接層；產品化換多語 CLIP 即可整包移除）──────────
# 依 longest-match 子字串比對展開；命中多個時串接成一句英文 prompt。
LEXICON: dict[str, str] = {
    # 食物 · 主角
    "披薩": "pizza", "起司": "cheese", "起士": "cheese", "乳酪": "cheese",
    "生魚片": "sashimi raw fish slices", "刺身": "sashimi raw fish slices",
    "壽司": "sushi", "握壽司": "nigiri sushi", "豆皮壽司": "inari tofu sushi",
    "豆皮": "inari tofu sushi", "飯糰": "rice ball",
    "炸雞翅": "fried chicken wings", "雞翅": "chicken wings", "炸雞": "fried chicken",
    "雞腿": "fried chicken drumstick", "鹹酥雞": "taiwanese fried chicken",
    "鯖魚": "grilled mackerel fish", "魚": "fish", "海鮮": "seafood",
    "熟食": "deli prepared hot food", "便當": "bento lunch box", "餐盒": "food box meal",
    "麵包": "bread", "番茄": "tomato", "蔬菜": "vegetables", "水果": "fruit",
    "肉": "raw meat", "牛肉": "beef", "豬肉": "pork",
    # 賣場 · 場景
    "門口": "store entrance storefront", "入口": "entrance", "外面": "outdoor street",
    "招牌": "store signboard", "看板": "signboard",
    "量販店": "hypermarket wholesale store interior", "賣場": "supermarket interior",
    "大潤發": "supermarket", "好市多": "warehouse store", "超市": "supermarket",
    "貨架": "supermarket shelves of products", "商品": "products on shelves",
    "冷藏": "refrigerated display case", "生鮮": "fresh food section",
    "熟食區": "deli food counter", "首食區": "food court dining area",
    "座位": "restaurant tables and chairs seating", "內用": "dine-in seating area",
    "用餐區": "food court dining area with tables", "吃飯": "people eating at tables",
    "美食街": "food court", "餐廳": "restaurant interior",
    # 動作 · 其他
    "走": "walking through aisle", "走道": "store aisle", "結帳": "checkout counter cashier",
    "推車": "shopping cart", "採購": "grocery shopping", "購物": "shopping",
    "特價": "discount price tag sale", "優惠": "sale promotion price tag",
    "折扣": "discount sale", "便宜": "cheap discount",
    "人潮": "crowd of people", "人": "people",
    "停車場": "parking lot with scooters", "機車": "parked scooters motorcycles",
    "招牌菜": "signature dish",
}

PROMPT_TEMPLATES = ["a photo of {}", "a close-up photo of {}", "a photo of {} in a store", "{}"]

# emit-shotindex 用的標籤 → 英文 prompt（示範「用視覺自動產生 shot_index」）
DEFAULT_LABELS: dict[str, list[str]] = {
    "sashimi":        ["sashimi", "raw fish slices", "packaged sashimi in fridge"],
    "sushi":          ["sushi", "nigiri sushi box", "inari tofu sushi"],
    "pizza":          ["pizza", "cheese pizza slice"],
    "fried_chicken":  ["fried chicken wings", "fried chicken"],
    "fish":           ["grilled mackerel fish", "whole fish"],
    "deli_counter":   ["deli prepared hot food counter", "food tray"],
    "dining_area":    ["restaurant tables and chairs", "food court dining seating"],
    "shelves":        ["supermarket shelves of products", "store aisle"],
    "storefront":     ["store entrance storefront signboard", "outdoor building sign"],
    "checkout":       ["checkout counter cashier"],
    "people":         ["crowd of people shopping"],
}

CJK = re.compile(r"[㐀-鿿豈-﫿]")


# ── 小工具 ──────────────────────────────────────────────────────────────────
def _log(*a):
    print(*a, file=sys.stderr, flush=True)


def ffprobe_duration(path: Path) -> float | None:
    """現查來源檔時長（秒）。取代舊有 source-offsets.csv 依賴——時長本來就是
    ffprobe 隨手查得到的資訊，不必再維護一份額外的離線快取檔。"""
    try:
        # 用 format=duration + default=nk=1:nw=1（與 render_p1.probe_dur 同款）：
        # csv 格式對某些相機檔會多吐一個空欄位（"7.52,"），float() 會炸（p3 實例）
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nk=1:nw=1", str(path)],
            capture_output=True, text=True, timeout=30)
        return float(out.stdout.strip())
    except Exception:
        return None


def sample_times(duration: float) -> list[float]:
    """在 (略過開頭 SKIP_START_SEC 秒, duration] 內取均勻分佈的代表時間點（局部 asset 時間）。

    手持開機常有 0.5~1s 的晃動/對焦糊幀（p2 實測：最初取樣落在 @0.3s 正好糊掉），
    因此樣本窗從 SKIP_START_SEC 開始，而非整段 [0, duration]。極短片（duration 遠小於
    SKIP_START_SEC）仍需要取到樣，故起點退讓到 duration*0.5 為上限，不會把整支片吃光。
    """
    n = max(MIN_FRAMES, min(MAX_FRAMES, round(duration / STRIDE_SEC)))
    lo = min(SKIP_START_SEC, duration * 0.5)
    span = max(duration - lo, 0.01)
    return [round(lo + span * (i + 0.5) / n, 2) for i in range(n)]


def extract_frame(src: Path, t: float, out: Path) -> bool:
    """用 fast-seek（-ss 在 -i 前）抽單張影格，避免整段解碼（對 GB 級檔案也快）。"""
    try:
        r = subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-loglevel", "error",
             "-ss", f"{t:.3f}", "-i", str(src),
             "-frames:v", "1", "-vf", f"scale={FRAME_SCALE}:-2", "-q:v", "4", str(out)],
            capture_output=True, timeout=120)
        return r.returncode == 0 and out.exists() and out.stat().st_size > 0
    except Exception:
        return False


def pack_emb(arr) -> str:
    """float16 + base64，緊湊存放（已 L2-normalize 的向量矩陣）。"""
    return base64.b64encode(arr.astype("<f2").tobytes()).decode("ascii")


def unpack_emb(s: str, dim: int):
    import numpy as np
    return np.frombuffer(base64.b64decode(s), dtype="<f2").astype("float32").reshape(-1, dim)


# ── CLIP 後端（延遲載入，快取模型）──────────────────────────────────────────
_MODEL = None  # (model, preprocess, tokenizer, device)


def _get_model():
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    import torch
    import open_clip
    _log(f"[visual_index] 載入 {MODEL_NAME}/{PRETRAINED} on {DEVICE} ...")
    t0 = time.time()
    model, _, preprocess = open_clip.create_model_and_transforms(
        MODEL_NAME, pretrained=PRETRAINED)
    tokenizer = open_clip.get_tokenizer(MODEL_NAME)
    model.eval().to(DEVICE)
    _log(f"[visual_index] 模型就緒（{time.time()-t0:.1f}s）")
    _MODEL = (model, preprocess, tokenizer, DEVICE)
    return _MODEL


def embed_images(paths: list[Path]):
    """回傳 (N, dim) 的 L2-normalized numpy float32。"""
    import numpy as np
    import torch
    from PIL import Image
    model, preprocess, _, device = _get_model()
    tensors, ok_idx = [], []
    for i, p in enumerate(paths):
        try:
            tensors.append(preprocess(Image.open(p).convert("RGB")))
            ok_idx.append(i)
        except Exception as e:
            _log(f"[visual_index] 略過無法讀取的影格 {p}: {e}")
    if not tensors:
        return np.zeros((0, 512), dtype="float32"), []
    batch = torch.stack(tensors).to(device)
    with torch.no_grad():
        feats = model.encode_image(batch)
        feats = feats / feats.norm(dim=-1, keepdim=True)
    return feats.cpu().float().numpy(), ok_idx


def embed_texts(prompts: list[str]):
    """回傳 (N, dim) 的 L2-normalized numpy float32。"""
    import numpy as np
    import torch
    model, _, tokenizer, device = _get_model()
    toks = tokenizer(prompts).to(device)
    with torch.no_grad():
        feats = model.encode_text(toks)
        feats = feats / feats.norm(dim=-1, keepdim=True)
    return feats.cpu().float().numpy()


# ── v2 多語後端（延遲載入、依 backend 快取）──────────────────────────────────
_V2_MODELS: dict = {}  # backend -> loaded handles


def _get_cnclip():
    h = _V2_MODELS.get("cnclip")
    if h is not None:
        return h
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    import torch
    from transformers import ChineseCLIPModel, ChineseCLIPProcessor
    _log(f"[visual_index] 載入 Chinese-CLIP {CN_CLIP_MODEL} on {DEVICE} ...")
    t0 = time.time()
    model = ChineseCLIPModel.from_pretrained(CN_CLIP_MODEL).eval().to(DEVICE)
    proc = ChineseCLIPProcessor.from_pretrained(CN_CLIP_MODEL)
    _log(f"[visual_index] Chinese-CLIP 就緒（{time.time()-t0:.1f}s）")
    _V2_MODELS["cnclip"] = (model, proc, torch)
    return _V2_MODELS["cnclip"]


def _get_stclip():
    h = _V2_MODELS.get("stclip")
    if h is not None:
        return h
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    from sentence_transformers import SentenceTransformer
    _log(f"[visual_index] 載入 ST 多語 CLIP（img={ST_IMAGE_MODEL}, txt={ST_TEXT_MODEL}）on {DEVICE} ...")
    t0 = time.time()
    img = SentenceTransformer(ST_IMAGE_MODEL, device=DEVICE)
    txt = SentenceTransformer(ST_TEXT_MODEL, device=DEVICE)
    _log(f"[visual_index] ST 多語 CLIP 就緒（{time.time()-t0:.1f}s）")
    _V2_MODELS["stclip"] = (img, txt)
    return _V2_MODELS["stclip"]


def embed_images_v2(paths: list[Path], backend: str | None = None):
    """v2 影像塔 → (N, dim) L2-normalized float32。與 embed_images(v1) 同介面（回 (arr, ok_idx)）。"""
    import numpy as np
    from PIL import Image
    backend = backend or V2_BACKEND
    imgs, ok_idx = [], []
    for i, p in enumerate(paths):
        try:
            imgs.append(Image.open(p).convert("RGB"))
            ok_idx.append(i)
        except Exception as e:
            _log(f"[visual_index] 略過無法讀取的影格 {p}: {e}")
    if not imgs:
        return np.zeros((0, 512), dtype="float32"), []
    if backend == "cnclip":
        model, proc, torch = _get_cnclip()
        outs = []
        for j in range(0, len(imgs), 16):
            inp = proc(images=imgs[j:j + 16], return_tensors="pt").to(DEVICE)
            with torch.no_grad():
                # 手動投影：transformers 5.x 的 get_image_features 回傳未投影的
                # BaseModelOutputWithPooling，這裡直接取 vision pooler → visual_projection。
                pooled = model.vision_model(pixel_values=inp["pixel_values"]).pooler_output
                f = model.visual_projection(pooled)
                f = f / f.norm(dim=-1, keepdim=True)
            outs.append(f.cpu().float().numpy())
        arr = np.concatenate(outs, 0)
    else:  # stclip
        img_model, _ = _get_stclip()
        arr = np.asarray(img_model.encode(imgs, normalize_embeddings=True, batch_size=16,
                                          convert_to_numpy=True, show_progress_bar=False))
    return arr.astype("float32"), ok_idx


def embed_texts_v2(prompts: list[str], backend: str | None = None):
    """v2 文字塔 → (N, dim) L2-normalized float32（中文原生，不經 LEXICON）。"""
    import numpy as np
    backend = backend or V2_BACKEND
    prompts = list(prompts)
    if backend == "cnclip":
        model, proc, torch = _get_cnclip()
        inp = proc(text=prompts, padding=True, return_tensors="pt").to(DEVICE)
        with torch.no_grad():
            # 手動投影：取 BERT [CLS]（last_hidden_state[:,0,:]）→ text_projection，
            # 與 Chinese-CLIP 官方一致（get_text_features 在 transformers 5.x 未投影）。
            out = model.text_model(input_ids=inp["input_ids"],
                                   attention_mask=inp.get("attention_mask"))
            f = model.text_projection(out.last_hidden_state[:, 0, :])
            f = f / f.norm(dim=-1, keepdim=True)
        return f.cpu().float().numpy().astype("float32")
    _, txt_model = _get_stclip()
    return np.asarray(txt_model.encode(prompts, normalize_embeddings=True,
                                       convert_to_numpy=True, show_progress_bar=False)).astype("float32")


def encode_query_v2(text: str, backend: str | None = None):
    """v2：中文/多語 query 直接進文字塔，不經 LEXICON、不套英文模板（治本核心）。"""
    import numpy as np
    v = embed_texts_v2([text.strip()], backend=backend)[0]
    return (v / (np.linalg.norm(v) + 1e-8)).astype("float32")


# ── 中→英 查詢展開 ──────────────────────────────────────────────────────────
def expand_query(text: str) -> tuple[str, list[str]]:
    """把使用者文字轉成英文 CLIP prompt。回傳 (english_phrase, matched_terms)。

    中文：以詞庫 longest-match 掃出片語並串接；沒命中就原樣丟給 CLIP（英文較準）。
    英文：原樣使用。
    """
    text = text.strip()
    if not CJK.search(text):
        return text, []
    hits, matched, i = [], [], 0
    keys = sorted(LEXICON.keys(), key=len, reverse=True)
    remaining = text
    # 依長度優先掃出所有出現的詞
    for k in keys:
        if k in remaining:
            hits.append(LEXICON[k])
            matched.append(k)
            remaining = remaining.replace(k, " ")
    if not hits:
        return text, []
    return ", ".join(dict.fromkeys(hits)), matched


def encode_query(text: str, lang: str = "zh"):
    """把查詢文字（含中→英橋接 + 多 prompt 模板）編碼成單一 L2-normalized 向量。

    lang="en"：英文素材本來就是英文，不必經 LEXICON 橋接（那是 CN→EN 的橋）；
    改走最簡英文關鍵詞抽取（見 extract_keywords_en），濾掉逐字稿口語填詞後再進
    英文 CLIP 文字塔。lang 預設 "zh"，行為與改動前完全一致（呼叫端未傳 lang 時
    走原本的 expand_query 路徑，一個位元都不變）。
    """
    import numpy as np
    if lang == "en":
        english = extract_keywords_en(text)
    else:
        english, _ = expand_query(text)
    prompts = [t.format(english) for t in PROMPT_TEMPLATES]
    embs = embed_texts(prompts)
    v = embs.mean(axis=0)
    return v / (np.linalg.norm(v) + 1e-8)


# ── 英文關鍵詞抽取（v1 CLIP 英文素材專用；不裝 nltk/spacy，手寫停用詞表）──────
# 逐字稿是口語，帶大量填詞（well/like/you know/um）與代名詞/連接詞，整句原封
# 丟給 CLIP 文字塔會稀釋語意。這裡只做最陽春過濾：正則切詞、轉小寫、去停用詞
# 與過短詞，取「實詞」（名詞/動詞/形容詞等內容詞的粗略近似 = 不在停用詞表裡的
# 詞）。不是精準 NLP 詞性判斷，夠用即可。
STOPWORDS_EN = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "to", "of", "in", "on", "at", "for", "with", "and", "or", "but", "if",
    "so", "because", "as", "that", "this", "these", "those", "it", "its",
    "i", "you", "we", "they", "he", "she", "my", "your", "our", "their",
    "his", "her", "them", "me", "us", "do", "does", "did", "have", "has",
    "had", "will", "would", "can", "could", "should", "just", "really",
    "actually", "like", "know", "think", "get", "got", "going", "gonna",
    "well", "okay", "ok", "yeah", "um", "uh", "uhh", "umm", "right",
    "there", "here", "what", "when", "where", "how", "why", "not", "no",
    "yes", "also", "very", "then", "than", "from", "by", "about", "all",
    "some", "one", "out", "up", "into", "over",
}


def extract_keywords_en(text: str) -> str:
    """英文最簡關鍵詞抽取：小寫化＋停用詞過濾＋取實詞（見上方模組註解）。

    回傳以空白接回的關鍵詞字串，直接餵給 PROMPT_TEMPLATES；全被濾光的極端情形
    （罕見，例如整段只有代名詞/助動詞）退回原文，總比丟空字串給 CLIP 好。
    """
    words = re.findall(r"[a-zA-Z']+", text.lower())
    kept, seen, out = [w for w in words if w not in STOPWORDS_EN and len(w) >= 3], set(), []
    for w in kept:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return " ".join(out) if out else text.strip()


# ── 索引物件 ────────────────────────────────────────────────────────────────
class VisualIndex:
    def __init__(self, meta: dict, frames: list[dict], embeddings):
        self.meta = meta
        self.frames = frames                 # 每個 frame: {assetId, t, range, source}
        self.embeddings = embeddings         # numpy (N, dim) L2-normalized
        # 索引自帶「用哪個文字塔查詢」的資訊：v1（英文 CLIP + LEXICON）或 v2（多語直查）。
        # 由 meta 決定 → 載入既有 v1 索引時 modelKind 缺省即 "v1"，向後相容。
        self.kind = meta.get("modelKind", "v1")
        self.backend = meta.get("v2Backend")  # v2 才有：cnclip / stclip

    # -- 建立 --
    @classmethod
    def build(cls, project, assets: list[str] | None = None,
             use_contact_fallback: bool = True, kind: str = "v1",
             backend: str | None = None,
             reuse_frames: list[dict] | None = None) -> "VisualIndex":
        """對 project 的資產抽幀 + 算 embedding。

        kind="v1"（預設）：英文 ViT-B-32/LAION 影像塔，查詢走 LEXICON 橋接（向後相容）。
        kind="v2"：多語/原生中文 CLIP，影像塔依 backend（cnclip / stclip）重算 embedding，
                   查詢中文直接進文字塔、不經 LEXICON（治本）。

        assets 未指定時 = project.list_assets()（直接列舉來源資料夾）。
        reuse_frames 給定時：沿用既有索引的抽幀計畫（相同 assetId/t/range/source），
        只重新抽該時間點的影格並重算 embedding——用於 v1→v2 遷移的「乾淨對照」（frame
        layout 完全一致）且省去重新取樣。

        0 資產 / 0 幀一律 raise VisualIndexError——不靜默寫出空索引還回報成功。
        """
        backend = (backend or V2_BACKEND) if kind == "v2" else None
        tmpdir = Path(tempfile.mkdtemp(prefix="vindex_"))
        frame_paths: list[Path] = []
        frame_meta: list[dict] = []

        if reuse_frames is not None:
            # 沿用既有抽幀計畫：逐幀在相同時間點重抽（mov）或直接用 contact_sheet
            for f in reuse_frames:
                a = f["assetId"]
                if f.get("source") == "contact_sheet":
                    cs = CONTACT / f"{a}.jpg"
                    if cs.exists():
                        frame_paths.append(cs)
                        frame_meta.append(dict(f))
                    else:
                        _log(f"[visual_index] 沿用抽幀：缺 contact_sheet {cs}，略過 {a}")
                    continue
                src = project.asset_path(a)
                if not src.exists():
                    _log(f"[visual_index] 沿用抽幀：來源缺失 {src}，略過 {a}@{f['t']}")
                    continue
                out = tmpdir / f"{a}_{f['t']:.2f}.jpg"
                if extract_frame(src, float(f["t"]), out):
                    frame_paths.append(out)
                    frame_meta.append(dict(f))
            _log(f"[visual_index] 沿用抽幀計畫：{len(frame_paths)}/{len(reuse_frames)} 幀重抽成功")
        else:
            if assets is None:
                assets = project.list_assets()
            if not assets:
                raise VisualIndexError(
                    f"專案 {project.id} 沒有任何來源資產可索引（{project.src} 下找不到 .MOV）")
            for a in assets:
                src = project.asset_path(a)
                dur = ffprobe_duration(src) if src.exists() else None
                got = 0
                if src.exists() and dur:
                    for t in sample_times(dur):
                        out = tmpdir / f"{a}_{t:.2f}.jpg"
                        if extract_frame(src, t, out):
                            frame_paths.append(out)
                            frame_meta.append({
                                "assetId": a, "t": t,
                                "range": [round(max(0.0, t - HALF_WIN), 1),
                                          round(min(dur, t + HALF_WIN), 1)],
                                "source": "mov"})
                            got += 1
                # MOV 抽不到 → 用 contact_sheet 代表影格墊檔（保證每個 asset 至少可查）
                if got == 0 and use_contact_fallback:
                    cs = CONTACT / f"{a}.jpg"
                    if cs.exists():
                        frame_paths.append(cs)
                        mid = (dur or 6.0) / 2.0
                        frame_meta.append({
                            "assetId": a, "t": round(mid, 2),
                            "range": [round(max(0.0, mid - HALF_WIN), 1),
                                      round(mid + HALF_WIN, 1)],
                            "source": "contact_sheet"})
                _log(f"[visual_index] {a}: {got} 幀"
                     + ("（改用 contact_sheet）" if got == 0 else ""))

        if not frame_paths:
            raise VisualIndexError(
                f"專案 {project.id}：一張幀都抽不到"
                f"（來源檔可能損毀/路徑錯誤，且無 contact_sheet 可墊檔）")

        _log(f"[visual_index] 抽幀完成，共 {len(frame_paths)} 張，開始編碼（kind={kind}"
             + (f"/{backend}" if backend else "") + "） ...")
        if kind == "v2":
            embs, ok_idx = embed_images_v2(frame_paths, backend=backend)
            model_name = (CN_CLIP_MODEL if backend == "cnclip"
                          else f"{ST_IMAGE_MODEL} + {ST_TEXT_MODEL}")
        else:
            embs, ok_idx = embed_images(frame_paths)
            model_name = f"{MODEL_NAME}/{PRETRAINED}"
        frame_meta = [frame_meta[i] for i in ok_idx]  # 對齊成功編碼的
        if not frame_meta or embs.shape[0] == 0:
            raise VisualIndexError(
                f"專案 {project.id}：{len(frame_paths)} 張影格全部編碼失敗（見上方 log）")
        meta = {
            "schemaVersion": 1,
            "note": "CLIP 影像 embedding 視覺索引；query(text)->[(assetId,range,score)]。"
                    "與 shot_index.json 平行、可融合或取代，見 visual_retrieval_design.md。",
            "modelKind": kind,
            "v2Backend": backend,
            "model": model_name,
            "device": DEVICE,
            "dim": int(embs.shape[1]) if embs.shape[0] else 512,
            "halfWindowSec": HALF_WIN,
            "strideSec": STRIDE_SEC,
            "maxFramesPerAsset": MAX_FRAMES,
            "lexiconSize": len(LEXICON) if kind == "v1" else 0,
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "frameCount": len(frame_meta),
            "assetCount": len(set(m["assetId"] for m in frame_meta)),
        }
        return cls(meta, frame_meta, embs)

    def save(self, path: Path):
        obj = dict(self.meta)
        obj["frames"] = self.frames
        obj["embeddings_b64_f16"] = pack_emb(self.embeddings)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        _log(f"[visual_index] 已寫出 {path}（{self.meta['frameCount']} 幀 / "
             f"{self.meta['assetCount']} assets）")

    @classmethod
    def load(cls, path: Path) -> "VisualIndex":
        obj = json.loads(path.read_text(encoding="utf-8"))
        dim = obj.get("dim", 512)
        embs = unpack_emb(obj["embeddings_b64_f16"], dim)
        meta = {k: v for k, v in obj.items() if k not in ("frames", "embeddings_b64_f16")}
        return cls(meta, obj["frames"], embs)

    # -- 查詢 --
    def query(self, text: str, topk: int = 5, per_asset: bool = True, lang: str = "zh") -> list[tuple]:
        """文字 → [(assetId, [start,end], score)]，score 為餘弦相似度。

        per_asset=True 時每個 asset 只回最佳影格（合併鄰近幀），較適合 B-roll 配對。
        lang：僅 v1（kind!="v2"）時生效，見 encode_query。v2 索引固定用多語/中文
        文字塔直查，語言無關（encode_query_v2 本就不分語言）。預設 "zh" 保留原行為。
        """
        import numpy as np
        if self.embeddings.shape[0] == 0:
            return []
        # 依索引自身的 kind 選文字塔：v2 中文直查（不經 LEXICON）、v1 走英文橋接/英文抽取。
        q = encode_query_v2(text, backend=self.backend) if self.kind == "v2" else encode_query(text, lang=lang)
        sims = self.embeddings @ q                      # (N,)
        order = np.argsort(-sims)
        results, seen_assets = [], {}
        for i in order:
            m = self.frames[int(i)]
            a = m["assetId"]
            score = float(sims[int(i)])
            if per_asset:
                if a in seen_assets:
                    # 同 asset：若時間接近已收的窗，略過；否則當作另一個子鏡頭
                    if any(abs(m["t"] - t0) < 2 * HALF_WIN for t0 in seen_assets[a]):
                        continue
                seen_assets.setdefault(a, []).append(m["t"])
            results.append((a, m["range"], round(score, 4)))
            if len(results) >= topk:
                break
        return results

    # -- 產生 shot_index 相容結構（示範「用視覺取代人工關鍵詞索引」）--
    def to_shot_index_like(self, labels: dict[str, list[str]] = None,
                           threshold: float = 0.22) -> dict:
        """對每個標籤算所有影格相似度，超過門檻者聚成 subshot，輸出 shot_index 相容 dict。

        注意：僅示範/輸出到獨立檔，不覆蓋既有 shot_index.json。
        """
        import numpy as np
        labels = labels or DEFAULT_LABELS
        # 先算每個 label 的文字向量
        label_vecs = {}
        for tag, prompts in labels.items():
            embs = embed_texts([f"a photo of {p}" for p in prompts])
            v = embs.mean(axis=0)
            label_vecs[tag] = v / (np.linalg.norm(v) + 1e-8)
        shots: dict[str, dict] = {}
        for i, m in enumerate(self.frames):
            emb = self.embeddings[i]
            best_tag, best_s = None, -1.0
            for tag, v in label_vecs.items():
                s = float(emb @ v)
                if s > best_s:
                    best_tag, best_s = tag, s
            if best_s < threshold:
                continue
            a = m["assetId"]
            shots.setdefault(a, {"hasFace": False, "quality": round(0.5 + best_s, 2),
                                 "subshots": []})
            shots[a]["subshots"].append({
                "range": m["range"], "tags": [best_tag],
                "visualScore": round(best_s, 4), "source": "visual"})
        return {"schemaVersion": 2,
                "note": "由 visual_index.py 依 CLIP 視覺相似度自動產生（示範用，未覆蓋人工 shot_index.json）。",
                "shots": shots}


# ── CLI ─────────────────────────────────────────────────────────────────────
# 通用示範查詢：對「目前專案」的索引跑一輪，用來目測檢索品質（沒有標準答案）。
# 想測自己的素材，直接用 `query` 子命令帶入台詞即可。
DEMO_QUERIES = ["店門口招牌", "食物特寫", "內用座位", "街道遠景", "人物自拍", "菜單", "飲料"]


def cmd_build(args, project):
    assets = args.assets.split(",") if args.assets else None
    kind = "v2" if args.model == "v2" else "v1"
    reuse = None
    if args.reuse_frames:
        src_idx = json.loads(Path(args.reuse_frames).read_text(encoding="utf-8"))
        reuse = src_idx["frames"]
        _log(f"[visual_index] --reuse-frames：沿用 {args.reuse_frames} 的 {len(reuse)} 幀計畫")
    idx = VisualIndex.build(project, assets=assets, kind=kind,
                            backend=args.backend, reuse_frames=reuse)
    # v2 預設輸出 visual_index_v2.json（絕不覆蓋既有 visual_index.json）；--out 可覆寫
    if args.out:
        out_path = Path(args.out)
    elif kind == "v2":
        out_path = project.visual_index.with_name("visual_index_v2.json")
    else:
        out_path = project.visual_index
    idx.save(out_path)


def _fmt(results):
    return "  ".join(f"{a}@{rng} ({s:+.3f})" for a, rng, s in results) or "（無）"


def _query_index_path(project, args):
    """CLI 查詢用索引路徑：--index 覆寫 > --model v2 用 visual_index_v2.json > 預設 visual_index.json。"""
    if getattr(args, "index", None):
        return Path(args.index)
    if getattr(args, "model", "v1") == "v2":
        return project.visual_index.with_name("visual_index_v2.json")
    return project.visual_index


def cmd_query(args, project):
    idx = VisualIndex.load(_query_index_path(project, args))
    res = idx.query(args.text, topk=args.topk)
    if idx.kind == "v2":
        print(f"查詢「{args.text}」→ 多語直查（{idx.meta.get('model')}，無 LEXICON）")
    else:
        eng, matched = expand_query(args.text)
        if matched:
            print(f"查詢「{args.text}」→ EN「{eng}」(命中詞: {'/'.join(matched)})")
        else:
            print(f"查詢「{args.text}」→ EN「{eng}」")
    for a, rng, s in res:
        print(f"  {a}  range={rng}  cos={s:+.4f}")


def cmd_demo(args, project):
    idx = VisualIndex.load(_query_index_path(project, args))
    print(f"模型={idx.meta.get('model')}（kind={idx.kind}）  幀數={idx.meta.get('frameCount')}  "
          f"assets={idx.meta.get('assetCount')}\n")
    for text in DEMO_QUERIES:
        res = idx.query(text, topk=args.topk)
        if idx.kind == "v2":
            print(f"● 「{text}」→ 多語直查")
        else:
            eng, _ = expand_query(text)
            print(f"● 「{text}」→ EN「{eng}」")
        for a, rng, s in res:
            print(f"    {a}  range={rng}  cos={s:+.4f}")
        print()


def cmd_emit_shotindex(args, project):
    idx = VisualIndex.load(project.visual_index)
    out = idx.to_shot_index_like(threshold=args.threshold)
    dst = project.analysis / "visual_shot_index.json"
    dst.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"→ 已寫出 {dst}（{len(out['shots'])} assets，未動 shot_index.json）")


def main():
    ap = argparse.ArgumentParser(description="CLIP 視覺語意 B-roll 索引/檢索")
    # --project 掛在共用 parent 上，讓它在子指令前後都能傳（build --project p1 或
    # --project p1 build 皆可）。
    parent = argparse.ArgumentParser(add_help=False)
    project_paths.add_project_arg(parent)
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", parents=[parent], help="抽幀+算 embedding，寫 visual_index.json")
    b.add_argument("--assets", help="逗號分隔的 assetId 子集（預設 project.list_assets() 全部）")
    b.add_argument("--model", choices=["v1", "v2"], default="v1",
                   help="v1=英文 CLIP+LEXICON（預設，向後相容）；v2=多語/原生中文直查（治本）")
    b.add_argument("--backend", choices=["cnclip", "stclip"], default=None,
                   help="v2 後端（預設 VISUAL_INDEX_V2_BACKEND=cnclip）：cnclip=Chinese-CLIP、stclip=多語蒸餾")
    b.add_argument("--reuse-frames", dest="reuse_frames", default=None,
                   help="沿用既有索引的抽幀計畫（相同時間點）只重算 embedding，用於 v1→v2 乾淨對照；"
                        "例：--reuse-frames analysis/p2/visual_index.json")
    b.add_argument("--out", default=None,
                   help="輸出路徑覆寫（v2 預設 visual_index_v2.json，絕不覆蓋既有 visual_index.json）")
    b.set_defaults(func=cmd_build)

    q = sub.add_parser("query", parents=[parent], help="單一文字查詢")
    q.add_argument("text")
    q.add_argument("--topk", type=int, default=5)
    q.add_argument("--model", choices=["v1", "v2"], default="v1",
                   help="v2 時讀 visual_index_v2.json（索引自帶文字塔資訊，會自動用對應 encoder）")
    q.add_argument("--index", default=None, help="索引路徑覆寫（優先於 --model）")
    q.set_defaults(func=cmd_query)

    d = sub.add_parser("demo", parents=[parent], help="跑內建台詞查詢")
    d.add_argument("--topk", type=int, default=3)
    d.add_argument("--model", choices=["v1", "v2"], default="v1")
    d.add_argument("--index", default=None, help="索引路徑覆寫（優先於 --model）")
    d.set_defaults(func=cmd_demo)

    e = sub.add_parser("emit-shotindex", parents=[parent],
                       help="用視覺相似度產生 shot_index 相容候選（獨立檔）")
    e.add_argument("--threshold", type=float, default=0.22)
    e.set_defaults(func=cmd_emit_shotindex)

    args = ap.parse_args()
    try:
        project = project_paths.load(args.project)
        args.func(args, project)
    except (project_paths.ProjectError, VisualIndexError) as e:
        _log(f"[visual_index] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
