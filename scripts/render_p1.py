#!/usr/bin/env python3
"""逐字稿優先渲染：edit_decision.json → 成片（含 padding 與 B-roll cutaway）。

流程：
  1. 依 aroll 逐段從來源 MOV 切片（切點自動加 padding：往後延到靜音、不吃下一句，
     避免尾字被切），依 manifest 的 target.resolution（預設 1920x1080）/30fps 編碼 → concat 成連續 A-roll（保留原聲）
  2. 若有 broll：以「影像替換、A-roll 旁白繼續」方式，把指定時間窗的畫面換成 B-roll
  3. 由 transcript.json 逐段字幕、重映射到最終時間軸
  4. 套主音樂（loudnorm + sidechain ducking + limiter）並燒字幕
  5. 輸出 output/<project>/final.mp4 + final.srt（檔名慣例見 app/project_paths.py）

渲染分兩種：
  - 預設「多 pass」：把合成拆成多支 ≤2 個 filter 節點的簡單 ffmpeg（base/B-roll 切片 concat
    + 字幕 + 音訊 + mux），抗高負載——規避 ffmpeg 7.1 在系統忙碌時 filtergraph -60 逾時。
  - --single：單一巨型 filtergraph 一次合成，較快但高負載易逾時（低負載時可用）。

用法：
  python3 scripts/render_p1.py --project p1            # 完整成片（多 pass，抗高負載）
  python3 scripts/render_p1.py --project p1 --single   # 單 pass（快，低負載時用）
  python3 scripts/render_p1.py --project p1 --radio    # 只出 A-roll radio edit（閉眼測試，無 B-roll/音樂/字幕）
  python3 scripts/render_p1.py --project p1 --no-broll # A-roll + 音樂 + 字幕，但不套 B-roll
  python3 scripts/render_p1.py --project p1 --no-tail-refine # 關閉尾切點能量谷精修（切點只信 whisper 時間碼）
  未帶 --project 時，走 app/project_paths.load() 的解析順序（VEDIOCUT_PROJ > current_project 指標 > 唯一專案）。
"""
import argparse
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))   # 讓 `python3 scripts/render_p1.py` 也能 import app.*
from app.music_beats import beats_on_timeline, slugify, snap_to_beats  # noqa: E402
import app.project_paths as project_paths  # noqa: E402  單一真相來源：路徑一律經此模組解析

# 專案路徑一律綁到 app.project_paths 解析出的 Project 物件（見 main() 開頭的 _bind_project）。
# 這裡先宣告成模組級名稱，供全檔案各函式讀取；實際值在 argparse 解析完 --project 後才填入，
# 藉此讓 render 邏輯保持不變，同時徹底移除舊的 VEDIOCUT_PROJ/VEDIOCUT_SRC 環境變數直讀。
PROJ = SRC = WORK = OUT = EDL = TRANSCRIPT = CONFIG = BEATS_DIR = SEG = None


def _bind_project(proj: "project_paths.Project") -> None:
    """把本檔案的模組級路徑常數綁到 Project 物件（單一真相來源，見 app/project_paths.py）。"""
    global PROJ, SRC, WORK, OUT, EDL, TRANSCRIPT, CONFIG, BEATS_DIR, SEG, SUB_STYLE
    PROJ = proj.id
    SRC = proj.src
    WORK = proj.work
    OUT = proj.output
    EDL = proj.edl
    TRANSCRIPT = proj.transcript
    CONFIG = proj.config             # 唯讀：由別的代理管理，這支只用 .get 讀
    BEATS_DIR = proj.beats_dir       # 唯讀：music_beats.py 產出的拍點 JSON
    SEG = WORK / "segments"
    SUB_STYLE = _sub_style(proj.language)  # 字幕字型依素材語言而定，見 _sub_style()
    _bind_video_profile(proj)              # 解析度/編碼參數依專案 target 而定，見該函式


LEAD_PAD = 0.12   # 起點往前留（不吃前一句）
TAIL_PAD = 0.55   # 尾端往後留（不吃下一句），修正 whisper 尾字被切
# 尾切點能量谷精修（tail-refine）參數；見 refine_tail。--no-tail-refine 可整體關閉。
TAIL_REFINE = True          # 由 main() 依 --no-tail-refine 覆寫
_TAIL_CACHE = {}            # (assetId, round(so,3)) -> (eff_out, log_msg)，記憶體內快取
_SRC_DUR = {}               # assetId -> 素材長度（秒）；padding 的硬邊界，見 pad_clip
REFINE_QUIET_DROP = 12.0    # 谷需比窗內語音峰值低 ≥ 此 dB 才算「夠安靜」
REFINE_ONSET_DROP = 6.0     # 高於 peak-此值且持續 → 視為下一詞起音
REFINE_ONSET_MIN = 5        # 起音需持續的短窗數（×REFINE_HOP 秒）
REFINE_ONSET_BACKOFF = 0.06 # 切點與偵測到的下一詞起音至少保留的秒數
REFINE_MIN_GAP = 3          # 合格能量谷最短長度（×REFINE_HOP 秒）
REFINE_EDGE_MARGIN = 0.08   # 切點與搜尋窗硬上界至少保留的秒數（防未偵到的近界起音）
REFINE_VALLEY_BAND = 2.5    # 取谷時容許高於谷底的 dB 帶（偏好晚一點的谷→多留 padding）
REFINE_MIN_TAIL = 0.08      # 谷需在 so+此值之後（不切進尾字）
REFINE_HOP = 0.010          # RMS 短窗跳距（秒）≈ 能量包絡解析度
REFINE_WIN = 0.025          # RMS 短窗長度（秒）
REFINE_SR = 16000           # 能量探測取樣率（單聲道）
REFINE_SMOOTH = 5           # 包絡移動平均點數（×REFINE_HOP 秒）
# ── 字幕時序（2026-07-14 使用者確認的 S1+S2a 修法）────────────────────────────
SUB_DELAY = 0.25        # S2a：whisper 標籤系統性早於實際語音 ~0.3s，字幕起點統一延後補償
SUB_TAIL_GRACE = 0.15   # S1：字幕上界=名義 sourceOut+此值——padding 是給語音衰減的，
                        #     不是給下一句字幕的（防「內容已剪掉、字幕仍顯示」的幽靈字幕）
SUB_MIN_DUR = 0.25      # 字幕最短顯示時長（過短不收錄）
# S3：字幕最長顯示時長上限。whisper 會把句尾的長沉默併進同一個 segment——p8 曾有
# 一句 10 個字的話掛了 17.4 秒（講者說完後畫面是無聲的動作）。畫面要留白
# （daily 模板刻意保留呼吸感），但字幕不該賴著不走。上限取「字數 × 0.45s + 1.2s」：
# 一般語速約 0.2-0.25 s/字，這個係數是慢速的兩倍，正常說話絕不會被截斷——p8 的開場
# 白 25 字 8.5 秒（0.34 s/字）就在上限內不受影響，只有夾帶長沉默的才會被收掉。
SUB_MAX_PER_CHAR = 0.45
SUB_MAX_BASE = 1.2
# ── 接點呼吸空間（S3a/S3b + 微接縫）──────────────────────────────────────────
TAIL_MIN_FLOOR = 0.15   # S3a：尾端至少留 so+此值（原 0.05 造成爆接），仍受下一詞起音 cap 保護
HEAD_LEAD = 0.15        # S3b：起音前保證的安靜 lead-in 秒數（能量法，取代盲信標籤的 +0.03 地板）
HEAD_MAX_EXT = 0.35     # S3b：頭端最多往前撈的秒數（quiet-walk 保證不吃上一句語音）
XFADE_IN = 0.06         # 切片頭音訊淡入（軟化接縫、去 click；不改時長不動時間軸）
XFADE_OUT = 0.08        # 切片尾音訊淡出
CACHE_VER = "v2"        # 切片快取版本：音訊淡入出改變切片內容，升版讓舊快取失效
CACHE_DUR_TOL = 0.3   # 切片快取時長容差（秒）；超過視為 ffmpeg 曾靜默截斷，需重切
RUN_TIMEOUT = 900     # subprocess 逾時上限（秒）；避免卡死的 ffmpeg 無限等待
ASSET_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")   # 防路徑穿越：assetId 只允許英數字/_/-
# drawtext 需要實際存在的 CJK 字型檔（libass 字幕走 fontconfig，drawtext 不吃名稱，只吃檔案）
CJK_FONT = "/System/Library/Fonts/STHeiti Medium.ttc"
# ── 影像輸出規格 ────────────────────────────────────────────────────────────
# VF/VENC 是模組級常數（多處 ffmpeg 指令共用），實際值在 _bind_video_profile() 依
# 專案 manifest 的 target.resolution 決定。歷史坑（2026-07-18，p4 實測發現）：
# 這兩個常數原本寫死 1280x720 + preset veryfast + crf 22（p1 時代的快速預覽參數），
# 於是 1080p 素材一律被降到 720p（像素只剩 44%）、且 manifest 明明有 resolution
# 欄位卻從未被讀取——使用者看到的成片畫質遠低於素材本身。
PREVIEW_W, PREVIEW_H = 1280, 720          # --preview 快速模式（原本的預設值）
DEFAULT_W, DEFAULT_H = 1920, 1080         # manifest 未指定 resolution 時的預設
SUB_FONTSIZE_BASE = 18                    # 字幕級數基準（對應 720p，其他解析度按高度等比）
VIDEO_W = VIDEO_H = None                  # 由 _bind_video_profile() 填入
PREVIEW_MODE = False                      # 由 main() 依 --preview 覆寫（須在綁定前設定）
VF = None
VENC = None


def _video_filter(w: int, h: int) -> str:
    """縮放到目標解析度並補黑邊（保持原始長寬比），統一 30fps / yuv420p。"""
    return (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p")


def _video_encoder(h: int, preview: bool) -> list:
    """x264 參數。preview=快速預覽（veryfast/crf22，原歷史行為）；否則品質優先。

    非預覽模式用 preset medium + crf 19（視覺接近無損），maxrate/bufsize 依解析度
    放大——1080p 的高動態畫面（草地、奔跑）在 5000k 上限下會出現明顯壓縮痕跡。
    """
    if preview:
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                "-maxrate", "5000k", "-bufsize", "7000k"]
    maxrate, bufsize = ("16000k", "24000k") if h >= 1080 else ("8000k", "12000k")
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "19",
            "-maxrate", maxrate, "-bufsize", bufsize]


def _bind_video_profile(proj: "project_paths.Project") -> None:
    """依專案 manifest 的 target.resolution（例 "1920x1080"）決定輸出規格。

    --preview 一律強制 720p 快速參數（覆寫 manifest），供改 EDL 後快速確認節奏用。
    字幕級數依高度等比縮放：libass 的 FontSize 以影片高度為基準，沿用 720p 的 18
    會讓 1080p 成片的字變小。
    """
    global VIDEO_W, VIDEO_H, VF, VENC, SUB_STYLE
    if PREVIEW_MODE:
        VIDEO_W, VIDEO_H = PREVIEW_W, PREVIEW_H
    else:
        spec = str((proj.manifest.get("target") or {}).get("resolution") or "")
        m = re.fullmatch(r"(\d{2,5})x(\d{2,5})", spec.strip().lower())
        VIDEO_W, VIDEO_H = (int(m.group(1)), int(m.group(2))) if m else (DEFAULT_W, DEFAULT_H)
    VF = _video_filter(VIDEO_W, VIDEO_H)
    VENC = _video_encoder(VIDEO_H, PREVIEW_MODE)
    size = max(10, round(SUB_FONTSIZE_BASE * VIDEO_H / PREVIEW_H))
    SUB_STYLE = _sub_style(proj.language, fontsize=size)
    mode = "預覽" if PREVIEW_MODE else "品質"
    print(f"  影像規格：{VIDEO_W}x{VIDEO_H}／{mode}模式"
          f"（{'veryfast/crf22' if PREVIEW_MODE else 'medium/crf19'}）字幕級數 {size}")


def timeout_for(duration):
    """整片級 pass（字幕燒錄、音訊混音）的逾時：固定值會隨片長失效。

    RUN_TIMEOUT=900s 對 4-5 分鐘的片綽綽有餘，但字幕燒錄是把整支片用 x264 medium
    重編一次——p8 的 20 分鐘片在這一 pass 就撞上 900s 逾時。逾時的用途是抓卡死，
    不是編碼時間預算，所以取「基礎值 + 6×片長」：1080p30 medium 實測約 1-2× 實時，
    加上 libass 疊字大約再一倍，這個倍率仍有 6-10 倍餘裕，卡死照樣抓得到。
    """
    return int(max(RUN_TIMEOUT, RUN_TIMEOUT + 6 * max(0.0, duration)))


def run(cmd, timeout=RUN_TIMEOUT):
    """執行 subprocess；逾時（預設 900s）丟清楚錯誤並附上是哪支指令，避免靜默卡死。"""
    try:
        subprocess.run(cmd, check=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"指令逾時（>{timeout}s），可能已卡死：{' '.join(str(x) for x in cmd)}") from e


def _assert_safe_asset_id(asset_id):
    """assetId 會直接拼進檔案路徑（SRC / f"{assetId}.MOV"），只允許英數字/_/- 防路徑穿越。"""
    if not ASSET_ID_RE.fullmatch(str(asset_id)):
        raise ValueError(f"不安全的 assetId（只允許英數字/_/-）：{asset_id!r}")


def probe_dur(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nk=1:nw=1", str(path)], capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def assert_dur(path, expected, label, tol=2.0):
    """驗證產出時長 ≈ 預期；否則丟錯（避免高負載下 ffmpeg 靜默截斷後還當成功輸出）。"""
    d = probe_dur(path)
    if abs(d - expected) > tol:
        raise RuntimeError(
            f"{label} 時長異常：{d:.1f}s（預期 ~{expected:.1f}s）—— "
            f"多半是系統高負載讓 ffmpeg 靜默截斷。請等 load 降下來再重跑。")


def load_segments_by_asset():
    d = {}
    if TRANSCRIPT.exists():
        for s in json.loads(TRANSCRIPT.read_text(encoding="utf-8")).get("segments", []):
            d.setdefault(s["assetId"], []).append(s)
    for k in d:
        d[k].sort(key=lambda s: s["sourceStart"])
    return d


# ── 精修依賴缺失追蹤（避免靜默降級被忽略）──────────────────────────────────
# 背景（2026-07-18 p4 真實事故）：系統 python3 沒裝 numpy、只有 .venv 有，
# refine_tail/refine_head 的 try/except 把 ModuleNotFoundError 當一般錯誤吞掉，
# 逐段印一行「精修略過」就滑過去，最後照樣輸出「看起來成功」的成片——但切點
# 能量精修（防尾字被剪的核心防線）整支影片全部沒跑。
# 這裡把兩種情況分開：
#   1. 依賴缺失（ModuleNotFoundError）：全域性問題，每段都會發生 → 只在第一次
#      印醒目警告（不逐段洗版），並在 render 結尾印品質降級聲明，讓人不可能錯過。
#   2. 該段真的判定不需精修（_compute_tail_refine / _compute_head_refine 正常
#      跑完，只是 base 已足或找不到合格安靜段）：這是預期行為，維持現有輸出，
#      不算降級、不計入下面的追蹤集合。
_DEP_MISSING_WARNED = False    # 是否已印過「缺模組」的醒目警告（一次執行只印一次）
_DEP_MISSING_MODULES = set()   # 缺失的模組名（理論上可能不只 numpy）
_DEP_DEGRADED_CLIPS = set()    # 因缺模組而被跳過精修的 clip id（tail/head 任一命中即算）
_REFINE_SEEN_CLIPS = set()     # 所有跑過精修入口（refine_tail/refine_head）的 clip id，供結尾算分母


def _reexec_hint_cmd(venv_py):
    """組出「改用 .venv/bin/python 重跑」的可直接複製指令，帶上使用者實際下的參數
    （若 --project 是靠自動解析、沒明寫在指令列，補上目前已綁定的 PROJ 值）。"""
    argv = list(sys.argv[1:])
    if "--project" not in argv and PROJ:
        argv = ["--project", PROJ] + argv
    return " ".join([str(venv_py), "scripts/render_p1.py", *argv])


def _note_dep_missing(exc, clip_id):
    """記錄一次「缺依賴導致精修被跳過」事件：第一次醒目示警（含指路 .venv），
    其後同一次執行只計數、不重複洗版（見 refine_tail/refine_head 的呼叫點）。"""
    global _DEP_MISSING_WARNED
    mod = getattr(exc, "name", None) or str(exc)
    _DEP_MISSING_MODULES.add(mod)
    _DEP_DEGRADED_CLIPS.add(clip_id)
    if not _DEP_MISSING_WARNED:
        _DEP_MISSING_WARNED = True
        venv_py = ROOT / ".venv" / "bin" / "python"
        print("", file=sys.stderr)
        print("!" * 70, file=sys.stderr)
        print(f"⚠⚠⚠ 切點能量精修無法執行：缺少模組「{mod}」（ModuleNotFoundError） ⚠⚠⚠",
              file=sys.stderr)
        print("    本次渲染的切點將只信 whisper 時間碼，尾字被剪風險未經防護！",
              file=sys.stderr)
        if venv_py.exists():
            print(f"    → 請改用 .venv/bin/python 重跑：{_reexec_hint_cmd(venv_py)}",
                  file=sys.stderr)
        else:
            print(f"    → 找不到 {venv_py}；請安裝 {mod}（及其他 refine 依賴）後再渲染。",
                  file=sys.stderr)
        print("!" * 70, file=sys.stderr)
        print("", file=sys.stderr)


def dep_degradation_summary():
    """render 結尾呼叫：若有段落因缺依賴被跳過精修，回傳醒目的品質降級聲明字串；
    完全沒發生降級則回傳 None（不誤報，見任務要求：有依賴時結尾不可印這行）。"""
    if not _DEP_DEGRADED_CLIPS:
        return None
    n = len(_DEP_DEGRADED_CLIPS)
    m = len(_REFINE_SEEN_CLIPS) or n
    mods = "、".join(sorted(_DEP_MISSING_MODULES))
    return (f"⚠ 本次渲染有 {n}/{m} 段未經切點能量精修（缺 {mods}），"
            f"尾字被剪風險未經防護。請改用 .venv/bin/python 重跑本指令。")


# ── 尾切點能量谷精修（tail-refine）───────────────────────────────────────────
# 根治：whisper 常把相鄰兩句標成「零間隙緊貼」，pad_clip 的天花板 next_start-0.05
# 把尾端餘裕壓到只剩 0.05s（< 尾字自然衰減 0.15~0.3s），尾字被剪（實例 a12「好吃」
# 的「吃」被切）。這裡改用「實際音訊能量」而非盲信 whisper 時間碼：在尾字之後找
# 「夠安靜的能量谷」當切點，容許輕微越過（本來就不準的）下一句標籤，硬上限
# next_start+0.35；找不到合格谷 → 維持現行 base_out（零風險 fallback）。
# 策略（實測驗證）：只把「被壓縮的切點」往後延伸到能量谷（加 padding、防切尾字）；
# 否則維持 base_out——僅當 base 明顯吃進偵測到的下一詞起音時才回收，絕不把切點縮進
# 尾字內（避免像 a13「31元」被切成「30」的反向誤傷）。

def _tail_envelope(asset_id, lo, hi):
    """抽 [lo,hi] 單聲道 16k PCM，回 (times, db_env)：REFINE_WIN 短窗 RMS→dBFS，
    再做 REFINE_SMOOTH 點移動平均平滑。抽不到足夠音訊回 (None, None)。"""
    import numpy as np
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{lo:.3f}", "-t", f"{hi - lo:.3f}",
         "-i", str(SRC / f"{asset_id}.MOV"), "-map", "0:a:0",
         "-ac", "1", "-ar", str(REFINE_SR), "-f", "s16le", "-"],
        capture_output=True, timeout=60)
    x = np.frombuffer(proc.stdout, dtype="<i2").astype(np.float64)
    ws = int(REFINE_WIN * REFINE_SR); hs = int(REFINE_HOP * REFINE_SR)
    if x.size < ws:
        return None, None
    n = 1 + (x.size - ws) // hs
    csum = np.concatenate(([0.0], np.cumsum(x * x)))
    starts = np.arange(n) * hs
    rms = np.sqrt((csum[starts + ws] - csum[starts]) / ws)
    db = 20.0 * np.log10(np.maximum(rms, 1e-9) / 32768.0)
    if n >= REFINE_SMOOTH:
        db = np.convolve(db, np.ones(REFINE_SMOOTH) / REFINE_SMOOTH, mode="same")
    return lo + (starts + ws / 2.0) / REFINE_SR, db


def _compute_tail_refine(asset_id, so, next_start, base_out):
    """尾切點精修核心，回傳 (eff_out, reason_msg)。演算法：
      1. 搜尋窗 [so-0.20, min(so+TAIL_PAD, next_start+0.35)]；分析另取左文脈 so-0.60
         抓尾字語音位準、右前瞻 +0.20 看到剛越界的下一詞起音。
      2. speech_ref = 窗內平滑包絡峰值；quiet_thr = peak-12dB、onset_thr = peak-6dB。
      3. 偵測下一詞起音（跟在安靜幀後、持續高於 onset_thr 的上升）→ 定切點上界 cap。
      4. 取 cap 前「最後一段合格能量谷」中「晚且深、且過了 so+MIN_TAIL」的點為谷點。
      5. 谷點 > base → 延伸（加 padding）；否則維持 base，只有 base 吃進起音才回收。"""
    import numpy as np
    w_lo = max(0.0, so - 0.20)
    w_hi = min(so + TAIL_PAD,
               (next_start + 0.35) if next_start is not None else so + TAIL_PAD)
    if w_hi <= w_lo + 0.10:
        return base_out, f"窗太小，維持 {base_out:.2f}"
    t, e = _tail_envelope(asset_id, max(0.0, so - 0.60), w_hi + 0.20)
    if t is None:
        return base_out, f"無音訊，維持 {base_out:.2f}"
    speech_ref = float(e.max())
    quiet_thr = speech_ref - REFINE_QUIET_DROP
    onset_thr = speech_ref - REFINE_ONSET_DROP
    lo = int(np.searchsorted(t, w_lo))
    # 下一詞起音：跟在一個「安靜幀」之後、第一個持續高於 onset_thr 的上升
    onset_idx = None; seen_quiet = False
    for i in range(lo, len(t)):
        if e[i] < quiet_thr:
            seen_quiet = True
        elif (seen_quiet and e[i] >= onset_thr
              and float(np.mean(e[i:i + REFINE_ONSET_MIN])) >= onset_thr):
            onset_idx = i; break
    cap_t = (min(w_hi, float(t[onset_idx]) - REFINE_ONSET_BACKOFF)
             if onset_idx is not None else w_hi)
    cap_t = min(cap_t, w_hi - REFINE_EDGE_MARGIN)
    # S3a 尾端呼吸地板：至少留 so+TAIL_MIN_FLOOR（原 0.05 造成接點爆接），
    # 但不超過起音 cap——下一詞真的貼很近時，安全優先於呼吸。
    floor_t = max(base_out, min(so + TAIL_MIN_FLOOR, cap_t))
    hi2 = int(np.searchsorted(t, cap_t))
    if hi2 <= lo:
        return floor_t, (f"停頓被下一詞卡死，維持 {floor_t:.2f}" if floor_t <= base_out + 0.01
                         else f"{base_out:.2f}→{floor_t:.2f}（呼吸地板）")
    # [lo,hi2) 內最後一段合格谷（連續 e<=quiet_thr 且長度 >= REFINE_MIN_GAP）
    quiet = e[lo:hi2] <= quiet_thr
    runs = []; s = None
    for k in range(len(quiet)):
        if quiet[k] and s is None:
            s = k
        elif not quiet[k] and s is not None:
            runs.append((s, k)); s = None
    if s is not None:
        runs.append((s, len(quiet)))
    runs = [r for r in runs if r[1] - r[0] >= REFINE_MIN_GAP]
    if not runs:
        return floor_t, f"無合格谷，維持 {floor_t:.2f}"
    rs, r_end = runs[-1]
    sub = e[lo + rs:lo + r_end]
    band = float(sub.min()) + REFINE_VALLEY_BAND
    # 谷區間中「離下一詞最近、仍在谷底 band 內、且過了尾字 so+MIN_TAIL」的點：
    # 晚而深 → 最大化 padding 又不切進尾字
    cand = [lo + rs + k for k in range(len(sub))
            if sub[k] <= band and t[lo + rs + k] >= so + REFINE_MIN_TAIL]
    if not cand:
        return floor_t, f"谷落在尾字內，維持 {floor_t:.2f}"
    vloc = cand[-1]
    valley_t = min(float(t[vloc]), w_hi); valley_db = float(e[vloc])
    onset_t = float(t[onset_idx]) if onset_idx is not None else None
    if valley_t > base_out + 0.02:                       # 延伸：把被壓縮的切點加回 padding
        out = max(round(valley_t, 3), floor_t)
        return out, f"{base_out:.2f}→{out:.2f}（谷 {valley_db:.0f}dB）"
    if onset_t is not None and base_out > onset_t - REFINE_ONSET_BACKOFF:   # base 吃到下詞→回收
        out = max(round(valley_t, 3), min(so + 0.05, cap_t))   # 回收案例維持舊護欄（cap 已保護）
        return out, f"{base_out:.2f}→{out:.2f}（回收，谷 {valley_db:.0f}dB）"
    return floor_t, (f"base 已足（谷 {valley_t:.2f}），維持 {floor_t:.2f}" if floor_t <= base_out + 0.01
                     else f"{base_out:.2f}→{floor_t:.2f}（呼吸地板，谷 {valley_t:.2f}）")


def refine_tail(asset_id, so, next_start, base_out, clip_id="clip"):
    """尾切點能量谷精修入口：用實際音訊能量把 base_out（盲信 whisper 時間碼算出的
    切點）對到尾字後的能量谷。找不到合格谷 / 出任何錯 → 回 base_out（零風險 fallback，
    謹守 eff_out ≥ so + 0.05）。以 (asset, so) 為鍵做記憶體內快取；每段首次計算印一行
    tail-refine log。--no-tail-refine 會令 TAIL_REFINE=False，此函式直接回 base_out。"""
    if not TAIL_REFINE:
        return base_out
    _REFINE_SEEN_CLIPS.add(clip_id)
    key = (asset_id, round(so, 3))
    if key not in _TAIL_CACHE:
        try:
            eff_out, msg = _compute_tail_refine(asset_id, so, next_start, base_out)
        except ModuleNotFoundError as exc:   # 依賴缺失：全域性問題，需醒目示警＋結尾聲明
            eff_out, msg = base_out, f"精修略過（缺模組 {exc.name}），維持 {base_out:.2f}"
            _note_dep_missing(exc, clip_id)
        except Exception as exc:   # 缺 numpy 以外的 ffmpeg / 音訊等問題，安全退回（零風險 fallback）
            eff_out, msg = base_out, f"精修略過（{type(exc).__name__}），維持 {base_out:.2f}"
        eff_out = max(eff_out, so + 0.05)   # 護欄：切點不早於 so+0.05
        _TAIL_CACHE[key] = (eff_out, msg)
        print(f"  tail-refine: {clip_id} {msg}")
    return _TAIL_CACHE[key][0]


_HEAD_CACHE = {}


def _compute_head_refine(asset_id, si, base_in):
    """S3b 頭端能量 lead-in：找 si 附近的實際語音起音，往回收集安靜段，讓起音前
    保證 ≥HEAD_LEAD 秒呼吸（quiet-walk 遇到上一句語音能量即停，不吃前句尾巴）。
    根因：零間隙標籤下舊地板 prev_end+0.03 == 從標籤內開始，頭端呼吸≈0（爆接）。"""
    import numpy as np
    t, e = _tail_envelope(asset_id, max(0.0, si - 0.8), si + 0.6)
    if t is None:
        return base_in, f"無音訊，維持 {base_in:.2f}"
    peak = float(e.max())
    quiet_thr = peak - REFINE_QUIET_DROP
    onset_thr = peak - REFINE_ONSET_DROP
    lo = int(np.searchsorted(t, max(0.0, si - 0.4)))
    # F1：起音偵測需「先見安靜幀」前置（對照 _compute_tail_refine 183-203 行的 seen_quiet）。
    # 從連續語音中間切出的片段（hook 全是、一般段也常見）掃描窗 [si-0.4, si+0.6] 開頭即
    # 全在語音中；若無此前置，e[i] >= onset_thr 第一幀就觸發 → 誤把掃描起點當「起音」
    # （回傳 si-0.4 的垃圾值，effIn 平白多留 0.5s+）。改為：先遇到 e[i]<quiet_thr 的安靜幀，
    # 其後的持續上升才算起音。
    onset_idx = None; seen_quiet = False
    for i in range(lo, len(t)):
        if e[i] < quiet_thr:
            seen_quiet = True
        elif (seen_quiet and e[i] >= onset_thr
              and float(np.mean(e[i:i + REFINE_ONSET_MIN])) >= onset_thr):
            onset_idx = i
            break
    if onset_idx is None:
        # 整窗都沒有安靜幀 → 從連續語音中間切出，無從判定乾淨起音，維持 base_in（不精修）
        if not seen_quiet:
            return base_in, f"連續語音無安靜窗，維持 {base_in:.2f}"
        return base_in, f"無起音，維持 {base_in:.2f}"
    onset_t = float(t[onset_idx])
    # 從起音往回走安靜幀：lead_lo 是「可安全往前撈」的最早點
    j = onset_idx - 1
    lead_lo = onset_t
    while j >= 0 and onset_t - float(t[j]) <= HEAD_MAX_EXT and e[j] <= quiet_thr:
        lead_lo = float(t[j]); j -= 1
    # F2：quiet-walk 後若可用安靜段不足（起音前緊貼的都不是安靜幀，lead_lo+0.02 已越過
    # onset-0.05）→ 維持 base_in，絕不在無真實安靜的情況下往回撈（否則直接吃進前一句語音）。
    if lead_lo + 0.02 > onset_t - 0.05:
        return base_in, f"起音前無足夠安靜（起音 {onset_t:.2f}），維持 {base_in:.2f}"
    eff = max(0.0, lead_lo + 0.02, onset_t - HEAD_MAX_EXT)
    eff = min(eff, max(0.0, onset_t - HEAD_LEAD))        # 目標：起音前 ≥HEAD_LEAD
    eff = min(eff, max(0.0, onset_t - 0.05))             # 護欄：至少留 0.05，絕不切進起音
    if abs(eff - base_in) < 0.02:
        return base_in, f"維持 {base_in:.2f}（起音 {onset_t:.2f}）"
    return round(eff, 3), f"{base_in:.2f}→{eff:.2f}（起音 {onset_t:.2f}）"


def refine_head(asset_id, si, base_in, clip_id="clip", is_hook=False):
    """頭端 lead-in 精修入口：同 refine_tail 的快取/fallback 模式，--no-tail-refine 一併關閉。
    F4：hook 段（精華片頭）只允許向前修剪（punch-in 乾脆），禁止任何向後延伸。
    is_hook 併入快取鍵：hook 與非 hook 可能共用同一 (asset,si)（p2 h01/a12、h03/a24），
    不分區會互相污染。"""
    if not TAIL_REFINE:
        return base_in
    _REFINE_SEEN_CLIPS.add(clip_id)
    key = (asset_id, round(si, 3), bool(is_hook))
    if key not in _HEAD_CACHE:
        try:
            eff_in, msg = _compute_head_refine(asset_id, si, base_in)
        except ModuleNotFoundError as exc:   # 依賴缺失：全域性問題，需醒目示警＋結尾聲明
            eff_in, msg = base_in, f"精修略過（缺模組 {exc.name}），維持 {base_in:.2f}"
            _note_dep_missing(exc, clip_id)
        except Exception as exc:   # 缺 numpy 以外的問題，安全退回（零風險 fallback）
            eff_in, msg = base_in, f"精修略過（{type(exc).__name__}），維持 {base_in:.2f}"
        # F4：hook 只允許 eff_in ≥ base_in（修掉頭部死噪音 OK），禁止向後延伸——片頭要 punch-in。
        if is_hook and eff_in < base_in - 1e-9:
            msg = f"hook punch-in：鉗回 base {base_in:.2f}（禁向後延伸；原精修 {msg}）"
            eff_in = base_in
        _HEAD_CACHE[key] = (eff_in, msg)
        print(f"  head-refine: {clip_id} {msg}")
    return _HEAD_CACHE[key][0]


def _preflight_sources(aroll, broll):
    """開跑前檢查每支來源素材是否真的讀得到，讀不到就立刻中止並列出清單。

    iCloud「最佳化 Mac 儲存空間」會把本機資料清掉、留下 SF_DATALESS 佔位檔：st_size
    仍是原始大小（存在性檢查全部通過），但 st_blocks=0、讀取回 0 bytes。ffmpeg 遇到
    這種檔案不會報錯而是無限等待——p8 就是在渲染跑了一小時之後才卡死在一支 B-roll
    來源上，前面的工都白做。與其讓它在最後一哩路上吊死，不如開跑前 stat 一輪。
    """
    dataless, missing = [], []
    for c in list(aroll) + list(broll):
        a = c["assetId"]
        p = SRC / f"{a}.MOV"
        if not p.exists():
            missing.append((c.get("id") or c.get("overAroll"), a)); continue
        st = p.stat()
        if st.st_blocks == 0 and st.st_size > 0:
            dataless.append((c.get("id") or c.get("overAroll"), a, st.st_size))
    if not dataless and not missing:
        return
    print("", file=sys.stderr)
    if missing:
        print("  ✗ 來源素材不存在：", file=sys.stderr)
        for cid, a in missing:
            print(f"      [{cid}] {a}.MOV", file=sys.stderr)
    if dataless:
        print("  ✗ 來源素材是佔位檔（實佔 0 block，讀取會無限等待）：", file=sys.stderr)
        seen = set()
        for cid, a, size in dataless:
            if a in seen:
                continue
            seen.add(a)
            users = [x for x, y, _ in dataless if y == a]
            print(f"      {a}.MOV（標示 {size/1e6:.1f}MB，實佔 0）→ 用於 {', '.join(users)}",
                  file=sys.stderr)
        print("    修法：在「照片」下載原始檔，或把這些片段從 EDL 移除後重跑。", file=sys.stderr)
    raise SystemExit(2)


def pad_clip(clip, segs_by_asset):
    """回傳加了 padding 的 (effIn, effOut)：往後延到靜音、不吃相鄰語句。
    尾端切點經 refine_tail（能量谷精修）根治零間隙標籤把尾字剪掉的問題；
    頭端經 refine_head 保證起音前的安靜 lead-in（接點呼吸空間）。"""
    a = clip["assetId"]
    si, so = clip["sourceIn"], clip["sourceOut"]
    segs = segs_by_asset.get(a, [])
    # 尾端：找 so 之後最近的 segment 起點作為天花板（避免吃到下一句）
    next_starts = [s["sourceStart"] for s in segs if s["sourceStart"] >= so - 0.05]
    next_start = min(next_starts) if next_starts else None
    ceil_out = (next_start - 0.05) if next_start is not None else so + TAIL_PAD
    base_out = min(so + TAIL_PAD, max(so + 0.05, ceil_out))
    # 素材邊界天花板：選段落在素材最末端時，so + TAIL_PAD 會超出檔案長度，ffmpeg 只能
    # 切到 EOF → 切片比預期短，被 assert_dur 攔下並誤報成「系統高負載」。這裡先鉗制。
    src_dur = _SRC_DUR.get(a)
    if src_dur is None:
        src_dur = _SRC_DUR[a] = probe_dur(SRC / f"{a}.MOV")
    if base_out > src_dur - 0.02:
        clamped = max(so + 0.05, src_dur - 0.02)
        if clamped < base_out - 1e-9:
            print(f"  ! 素材邊界鉗制：{clip.get('id', 'clip')} 來源 {a} "
                  f"base_out {base_out:.3f}→{clamped:.3f}（素材僅 {src_dur:.2f}s）",
                  file=sys.stderr)
            base_out = clamped
    # 能量谷精修：把切點對到尾字後的能量谷（容許輕微越過不準的下一句標籤，硬上限
    # next_start+0.35）；找不到合格谷 → 維持 base_out。--no-tail-refine 可關。
    eff_out = refine_tail(a, so, next_start, base_out, clip.get("id", "clip"))
    # 起點：標籤地板（不吃上一句）之後，再用能量法保證起音前的安靜 lead-in
    prev_ends = [s["sourceEnd"] for s in segs if s["sourceEnd"] <= si + 0.05]
    floor_in = max(prev_ends) + 0.03 if prev_ends else si - LEAD_PAD
    base_in = max(0.0, si - LEAD_PAD, floor_in)
    is_hook = clip.get("chapter") == "hook"
    eff_in = refine_head(a, si, base_in, clip.get("id", "clip"), is_hook=is_hook)
    # 精修可能把 eff_out 推到 base_out 之後（硬上限 next_start+0.35），同樣不得越過素材邊界
    eff_out = min(eff_out, max(eff_in + 0.05, src_dur - 0.02))
    return round(eff_in, 3), round(eff_out, 3)


def cut_aroll(aroll, segs_by_asset):
    """切每個 A-roll 片段。內容定址快取：檔名由 clip id+來源+時間碼決定，
    因此重排順序不重切、只有改了 in/out 或換片才重切（利於剪輯介面快速重渲染）。

    快取命中時仍會 ffprobe 實測時長比對（容差 CACHE_DUR_TOL）：曾在系統高負載時
    ffmpeg 靜默截斷，壞切片留在快取又被當成「已存在」沿用，害後續全部跟著壞。
    不符就刪掉重切一次；重切後仍不符 → raise 清楚錯誤。"""
    SEG.mkdir(parents=True, exist_ok=True)
    concat = WORK / "concat.txt"
    lines, placed = [], []
    t = 0.0
    prev_asset = None       # F3：相鄰同來源重疊守衛用——記住前一段的 (assetId, eff_out)
    prev_eff_out = None
    for c in aroll:
        asset_id = c["assetId"]
        _assert_safe_asset_id(asset_id)
        eff_in, eff_out = pad_clip(c, segs_by_asset)
        # F3：相鄰同來源重疊守衛（最後防線）。相鄰兩段剪自同一來源檔、且尾端精修把前段
        # eff_out 延過邊界、頭端精修又把後段 eff_in 拉回邊界前時，來源區間重疊→同一段話播
        # 兩次。把後段起點鉗到前段終點 +0.02s 之後，確保來源區間不重疊。任何精修回歸都由此擋住。
        if prev_asset == asset_id and eff_in < prev_eff_out + 0.02:
            new_in = round(prev_eff_out + 0.02, 3)
            print(f"  ! 相鄰同來源重疊守衛：{c.get('id', 'clip')} 來源 {asset_id} "
                  f"eff_in {eff_in:.3f}→{new_in:.3f}"
                  f"（鉗制 {new_in - eff_in:.3f}s，防同段話重播）", file=sys.stderr)
            eff_in = new_in
        dur = round(eff_out - eff_in, 3)
        key = f"{c.get('id', 'clip')}_{asset_id}_{eff_in:.3f}-{eff_out:.3f}_{CACHE_VER}"
        seg = SEG / f"{key}.mp4"

        def _cut():
            # 切片頭尾加音訊微淡入出：接點軟化、去 click（不改時長、不動時間軸）
            fade = (f"afade=t=in:st=0:d={XFADE_IN},"
                    f"afade=t=out:st={max(0.0, dur - XFADE_OUT):.3f}:d={XFADE_OUT}")
            run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-ss", str(eff_in), "-t", str(dur), "-i", str(SRC / f"{asset_id}.MOV"),
                 "-map", "0:v:0", "-map", "0:a:0?", "-vf", VF, *VENC,
                 "-af", fade,
                 "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
                 "-movflags", "+faststart", str(seg)])

        if seg.exists() and seg.stat().st_size > 0 and abs(probe_dur(seg) - dur) > CACHE_DUR_TOL:
            print(f"  ! 快取切片時長異常（{probe_dur(seg):.2f}s ≠ 預期 {dur:.2f}s），"
                  f"刪除重切：{seg.name}", file=sys.stderr)
            seg.unlink()
        if not (seg.exists() and seg.stat().st_size > 0):
            _cut()
            assert_dur(seg, dur, f"cut_aroll 切片 {seg.name}", tol=CACHE_DUR_TOL)
        lines.append(f"file '{seg}'")
        placed.append({"timelineStart": t, "clip": c, "effIn": eff_in,
                       "effOut": eff_out, "dur": dur})
        t += dur
        prev_asset, prev_eff_out = asset_id, eff_out   # F3：供下一段重疊守衛參照
    concat.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return concat, round(t, 3), placed


def broll_windows(placed, brolls):
    """計算每個 B-roll 在最終時間軸上的窗 [t0,t1]（供 overlay 使用），去重疊。"""
    id2place = {p["clip"]["id"]: p for p in placed}
    wins = []
    for b in brolls:
        p = id2place.get(b["overAroll"])
        if not p:
            print(f"  ! broll 指向不存在的 aroll {b['overAroll']}，略過", file=sys.stderr)
            continue
        _assert_safe_asset_id(b["assetId"])
        t0 = p["timelineStart"] + b.get("startInClip", 0.0)
        t1 = min(t0 + b["duration"], p["timelineStart"] + p["dur"] - 0.1)
        if t1 - t0 < 0.5:
            continue
        wins.append({"t0": round(t0, 3), "t1": round(t1, 3),
                     "assetId": b["assetId"], "sourceIn": b["sourceIn"]})
    wins.sort(key=lambda w: w["t0"])
    merged = []
    for w in wins:
        if merged and w["t0"] < merged[-1]["t1"]:
            continue
        merged.append(w)
    return merged


def build_srt(placed):
    def ts(sec):
        h = int(sec // 3600); m = int((sec % 3600) // 60)
        s = int(sec % 60); ms = int(round((sec - int(sec)) * 1000))
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    segs_by_asset = load_segments_by_asset()
    cues = []
    for p in placed:
        c = p["clip"]; ei, eo, t0 = p["effIn"], p["effOut"], p["timelineStart"]
        si_nom, so_nom = c["sourceIn"], c["sourceOut"]
        # S1：字幕上界用「名義切點+GRACE」而非 padded effOut——尾端 padding 是給語音
        # 衰減的，不是給下一句字幕的；零間隙標籤下 effOut 會掃進已剪掉的下一句（幽靈字幕）
        sub_hi = min(eo, so_nom + SUB_TAIL_GRACE)
        for s in segs_by_asset.get(c["assetId"], []):
            mid = (s["sourceStart"] + s["sourceEnd"]) / 2.0
            if not (si_nom - 0.05 <= mid <= so_nom + 0.05):
                continue                       # 段落主體必須落在名義切點內（防半句幽靈）
            a = max(s["sourceStart"], ei); b = min(s["sourceEnd"], sub_hi)
            if b - a < SUB_MIN_DUR:
                continue
            a_tl = t0 + (a - ei); b_tl = t0 + (b - ei)
            # S2a：whisper 標籤系統性早於實際語音 ~0.3s → 字幕起點統一延後補償
            a_tl = min(a_tl + SUB_DELAY, b_tl - SUB_MIN_DUR)
            # S3：夾帶長沉默的 segment 會讓字幕賴在畫面上，依字數上限收掉尾巴
            txt = s["text"].strip()
            cap = len(txt) * SUB_MAX_PER_CHAR + SUB_MAX_BASE
            b_tl = min(b_tl, a_tl + max(SUB_MIN_DUR, cap))
            cues.append((max(t0, a_tl), b_tl, txt))
    cues.sort()
    lines = []
    for i, (a, b, text) in enumerate(cues, 1):
        if text:
            lines += [str(i), f"{ts(a)} --> {ts(b)}", text, ""]
    srt = OUT / project_paths.FINAL_SRT
    srt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return srt


def build_music_bed(cues, total_dur):
    """多首音樂段落切換：每個 cue 一首歌裁到 [fromSec,toSec) 長度、加淡入淡出，
    concat 成精確 total_dur 秒的音樂床。無 cues 回傳 None（改用單曲 loop）。"""
    if not cues:
        return None
    parts = []
    for i, cue in enumerate(cues):
        f = SRC / cue["file"]
        seg_dur = round(cue.get("toSec", total_dur) - cue.get("fromSec", 0.0), 3)
        if seg_dur <= 0 or not f.exists():
            continue
        out = WORK / f"music_{i}.wav"
        fade_out_st = max(0.0, seg_dur - 0.8)
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-stream_loop", "-1", "-i", str(f), "-t", str(seg_dur),
             "-af", f"afade=t=in:st=0:d=0.8,afade=t=out:st={fade_out_st}:d=0.8",
             "-ar", "48000", "-ac", "2", str(out)])
        parts.append(out)
    if not parts:
        return None
    listf = WORK / "music_bed_concat.txt"
    listf.write_text("\n".join(f"file '{p}'" for p in parts) + "\n", encoding="utf-8")
    bed = WORK / "music_bed.wav"
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", str(listf),
         "-t", str(total_dur), "-ar", "48000", "-ac", "2", str(bed)])
    return bed


def _sub_style(lang: str, fontsize: int = SUB_FONTSIZE_BASE) -> str:
    """字幕字型依素材語言而定：zh 沿用 PingFang TC；en 及其他語言用 Helvetica Neue
    （西文安全預設，PingFang TC 的西文字形不如專用西文字體）。
    fontsize 由 _bind_video_profile() 依輸出高度等比算出（基準 18 對應 720p）；
    MarginV 同步等比，否則 1080p 下字幕會離底邊過近。顏色/描邊等其他樣式值不變。"""
    font = "PingFang TC" if lang == "zh" else "Helvetica Neue"
    margin = max(20, round(30 * fontsize / SUB_FONTSIZE_BASE))
    outline = max(2, round(2 * fontsize / SUB_FONTSIZE_BASE))
    return (f"force_style='FontName={font},FontSize={fontsize},PrimaryColour=&H00FFFFFF,"
            f"OutlineColour=&H90000000,BorderStyle=1,Outline={outline},Shadow=0,MarginV={margin}'")


SUB_STYLE = _sub_style("zh")  # 模組載入時的預設值；實際值在 _bind_project() 依專案語言覆寫


def _hook_drawtext(hook_end):
    """「精選搶先看」浮水印。字級與邊距依輸出高度等比（基準值對應 720p），
    否則 1080p 成片的浮水印會相對變小、位置偏上。"""
    k = (VIDEO_H or PREVIEW_H) / PREVIEW_H
    return (f"drawtext=fontfile='{CJK_FONT}':text='精選搶先看':fontcolor=white:"
            f"fontsize={round(30 * k)}:box=1:boxcolor=black@0.55:"
            f"boxborderw={round(12 * k)}:x={round(40 * k)}:y={round(36 * k)}:"
            f"enable='between(t,0,{hook_end:.2f})'")


def resolve_music_file(raw_music):
    """沒有多段 cues 時，決定單曲配樂：
      EDL "music": false             → 無配樂
      EDL "music": {"file": "x.mp3"} → 該檔；不存在就明確失敗（指定了卻找不到，不該默默拿掉）
      未指定 file                     → 素材資料夾的第一首 mp3；一首都沒有 → 無配樂
    過去未指定時一律回退到開發者自己的一首配樂檔名——別人的素材資料夾沒有那首歌，渲染就在
    最後一步因找不到檔案失敗；也沒有任何寫法能得到「無配樂」的成片。"""
    if raw_music is False:
        return None
    name = raw_music.get("file") if isinstance(raw_music, dict) else None
    if name:
        p = SRC / name
        if not p.exists():
            raise SystemExit(f"[render] EDL 指定的配樂不存在：{p}\n"
                             f"         檔名須與素材資料夾內的 mp3 完全一致；不要配樂請寫 \"music\": false")
        return p
    mp3s = sorted(SRC.glob("*.mp3"))
    return mp3s[0] if mp3s else None


def _audio_mix_run(picture, bed, music_file, dur, out):
    """audio-only：旁白 + 音樂 → loudnorm/ducking/limiter。無影像 → filtergraph 輕量、抗高負載。
    bed 與 music_file 皆無時只處理人聲（響度正規化＋限幅）。"""
    if bed:
        ainputs = ["-i", str(picture), "-i", str(bed)]
    elif music_file:
        ainputs = ["-i", str(picture), "-stream_loop", "-1", "-i", str(music_file)]
    else:
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(picture),
             "-filter_complex", "[0:a]loudnorm=I=-16:LRA=9:TP=-1.5,alimiter=limit=0.89[aout]", "-map", "[aout]", "-t", str(dur),
             "-c:a", "aac", "-b:a", "192k", str(out)], timeout=timeout_for(dur))
        return
    afc = ("[0:a]loudnorm=I=-16:LRA=9:TP=-1.5,asplit=2[voice][key];"
           f"[1:a]atrim=duration={dur},volume=0.16,loudnorm=I=-24:LRA=7:TP=-2[music];"
           "[music][key]sidechaincompress=threshold=0.025:ratio=8:attack=20:release=350[ducked];"
           "[voice][ducked]amix=inputs=2:duration=first:dropout_transition=2,alimiter=limit=0.89[aout]")
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-filter_complex_threads", "1",
         *ainputs, "-filter_complex", afc, "-map", "[aout]", "-t", str(dur),
         "-c:a", "aac", "-b:a", "192k", str(out)],
        timeout=timeout_for(dur))


def render_multipass(picture, windows, bed, music_file, subq, dur, total, hook_end, final):
    """抗高負載：把單一巨型 filtergraph 拆成多支「≤2 個 filter 節點」的簡單 ffmpeg。

    影像不用 overlay 大圖，改成「base 切片 + B-roll 切片」concat（B-roll 期間畫面換成
    B-roll、旁白由獨立音訊軌延續）；字幕/字卡與音樂 ducking 各自一支簡單呼叫。
    每支呼叫都夠簡單，不會觸發 ffmpeg 7.1 在高負載下的 filtergraph -60 逾時。"""
    MP = WORK / "mp"
    if MP.exists():
        shutil.rmtree(MP)
    MP.mkdir(parents=True, exist_ok=True)

    # 1) 影像：base 與 B-roll 切片，逐段獨立簡單編碼（統一 h264/1280x720/30fps）
    pieces = []

    def slice_out(src, ss, d, vf):
        o = MP / f"v{len(pieces):03d}.mp4"
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-ss", f"{ss:.3f}", "-i", str(src), "-t", f"{d:.3f}",
             "-an", "-vf", vf, *VENC, "-r", "30", str(o)])
        pieces.append(o)

    cursor = 0.0
    for w in windows:
        if w["t0"] > cursor + 0.04:
            slice_out(picture, cursor, w["t0"] - cursor, "fps=30,format=yuv420p")
        slice_out(SRC / f"{w['assetId']}.MOV", w["sourceIn"], w["t1"] - w["t0"], VF)
        cursor = w["t1"]
    if total - cursor > 0.04:
        slice_out(picture, cursor, total - cursor, "fps=30,format=yuv420p")

    # 2) concat（同編碼參數 → -c copy 即可）
    lst = MP / "vconcat.txt"
    lst.write_text("\n".join(f"file '{p}'" for p in pieces) + "\n", encoding="utf-8")
    comp = MP / "comp.mp4"
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(comp)])
    assert_dur(comp, total, "concat 影像")

    # 3) 燒字幕 + 精華片頭字卡（單輸入、≤2 個 video filter）
    vf = f"subtitles='{subq}':{SUB_STYLE}"
    if hook_end > 0:
        vf += "," + _hook_drawtext(hook_end)
    sub_video = MP / "sub.mp4"
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(comp), "-vf", vf, "-an", *VENC, "-r", "30", str(sub_video)],
        timeout=timeout_for(total))

    assert_dur(sub_video, total, "字幕燒錄影像")

    # 4) 音訊混音（audio-only）
    mixed = MP / "mix.m4a"
    _audio_mix_run(picture, bed, music_file, dur, mixed)
    assert_dur(mixed, dur, "音訊混音")

    # 5) mux（copy，不再編碼）
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(sub_video), "-i", str(mixed),
         "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-t", str(dur),
         "-movflags", "+faststart", str(final)])
    assert_dur(final, dur, "最終成片")
    print(f"  多 pass 渲染：{len(pieces)} 影像切片 + 字幕 + 音訊 + mux")


def render_singlepass(picture, windows, bed, music_file, subq, dur, hook_end, final):
    """單一 filtergraph 一次合成（快，但高負載下 ffmpeg 7.1 易 -60 逾時）。"""
    inputs = ["-i", str(picture)]
    for w in windows:
        inputs += ["-ss", str(w["sourceIn"]), "-t", str(round(w["t1"] - w["t0"], 3)),
                   "-i", str(SRC / f"{w['assetId']}.MOV")]
    music_idx = 1 + len(windows)
    if bed:
        inputs += ["-i", str(bed)]
    elif music_file:
        inputs += ["-stream_loop", "-1", "-i", str(music_file)]
    fc = ["[0:v]fps=30,format=yuv420p[base]"]
    prev = "base"
    for i, w in enumerate(windows, start=1):
        fc.append(f"[{i}:v]{VF},setpts=PTS-STARTPTS+{w['t0']}/TB[b{i}]")
        fc.append(f"[{prev}][b{i}]overlay=enable='between(t,{w['t0']},{w['t1']})':"
                  f"eof_action=pass[ov{i}]")
        prev = f"ov{i}"
    if hook_end > 0:
        fc.append(f"[{prev}]{_hook_drawtext(hook_end)}[hk]")
        prev = "hk"
    fc.append(f"[{prev}]subtitles='{subq}':{SUB_STYLE}[vout]")
    if bed or music_file:
        fc.append("[0:a]loudnorm=I=-16:LRA=9:TP=-1.5,asplit=2[voice][key]")
        fc.append(f"[{music_idx}:a]atrim=duration={dur},volume=0.16,loudnorm=I=-24:LRA=7:TP=-2[music]")
        fc.append("[music][key]sidechaincompress=threshold=0.025:ratio=8:attack=20:release=350[ducked]")
        fc.append("[voice][ducked]amix=inputs=2:duration=first:dropout_transition=2,"
                  "alimiter=limit=0.89[aout]")
    else:
        fc.append("[0:a]loudnorm=I=-16:LRA=9:TP=-1.5,alimiter=limit=0.89[aout]")
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-filter_complex_threads", "1",
         *inputs, "-filter_complex", ";".join(fc),
         "-map", "[vout]", "-map", "[aout]", "-t", str(dur),
         *VENC, "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(final)])


def resolve_cue_times(cues, placed, total):
    """把 cue 的 fromChapter/toChapter 解析成 fromSec/toSec（用實際 timeline，含 padding）。
    每個 cue 以其 fromChapter 首次出現的 timelineStart 為起點；終點自動接到下一個 cue 的
    起點（最後一段接到 total）。已寫死 fromSec/toSec 的 cue 原樣沿用（向後相容）。"""
    chap_start = {}
    for p in placed:
        ch = p["clip"].get("chapter")
        if ch and ch not in chap_start:
            chap_start[ch] = round(p["timelineStart"], 3)
    resolved = []
    for cue in cues:
        c = dict(cue)
        if "fromSec" not in c and "fromChapter" in c:
            c["fromSec"] = chap_start.get(c["fromChapter"], 0.0)
        resolved.append(c)
    resolved.sort(key=lambda c: c.get("fromSec", 0.0))
    for i, c in enumerate(resolved):
        if "toSec" not in c:
            c["toSec"] = resolved[i + 1]["fromSec"] if i + 1 < len(resolved) else total
    return resolved


def _beats_for(cue_file):
    """讀取某首歌的拍點 JSON（analysis/<project>/beats/<slug>.json，唯讀）；不存在或壞檔回傳
    None——呼叫端把「沒有拍點資料」當成安全跳過，零風險漸進導入。"""
    if not cue_file:
        return None
    p = BEATS_DIR / f"{slugify(cue_file)}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def snap_windows_to_beats(windows, cues, placed, max_shift=0.15):
    """把每個 B-roll window 的進點 t0 吸到最近拍點，出點 t1 跟著等量平移（保持時長）。

    只平移 B-roll 窗本身，完全不碰 A-roll timeline（placed）與字幕——見
    analysis/p1/beat_align_design.md 的「對話完整性 > 卡拍」原則。

    護欄（全部通過才採用吸附後的值，否則撤回維持原窗）：
      1. 吸附後仍須落在原窗所屬 A-roll clip 的 timeline 範圍內
         （與 broll_windows 同一上界：clip 尾 -0.1s）。
      2. 吸附後長度仍 >= 0.5s。
      3. 找不到該 cue 對應的拍點 JSON、或找不到所屬 A-roll clip 範圍 → 不吸，原樣保留。
    cue 以窗的原始 t0 落在哪個 cue 的 [fromSec, toSec) 決定要用哪首歌的 beat grid
    （跨 cue 邊界的窗一律以 t0 為準）。
    """
    if not windows:
        return windows
    clip_bounds = [(p["timelineStart"], p["timelineStart"] + p["dur"]) for p in placed]

    def _clip_range_for(t0):
        for start, end in clip_bounds:
            if start - 1e-6 <= t0 < end + 1e-6:
                return start, end
        return None

    out = []
    for w in windows:
        cue = next((c for c in cues
                    if c.get("fromSec", 0.0) <= w["t0"] < c.get("toSec", float("inf"))), None)
        bj = _beats_for(cue.get("file")) if cue else None
        if not bj or not bj.get("beats"):
            print(f"  beat-snap: {w['assetId']}@{w['t0']:.2f}s 無拍點資料，維持原時間")
            out.append(w)
            continue
        tl_beats = beats_on_timeline(bj["beats"], cue["fromSec"], cue["toSec"],
                                     bj.get("duration"))
        new_t0 = snap_to_beats([w["t0"]], tl_beats, max_shift)[0]
        dt = round(new_t0 - w["t0"], 3)
        if dt == 0:
            print(f"  beat-snap: {w['assetId']}@{w['t0']:.2f}s 位移 0.000s（已在拍上或附近無拍）")
            out.append(w)
            continue
        cand_t0, cand_t1 = round(w["t0"] + dt, 3), round(w["t1"] + dt, 3)
        rng = _clip_range_for(w["t0"])
        ok = (cand_t1 - cand_t0) >= 0.5 and rng is not None
        if ok:
            clip_start, clip_end = rng
            ok = cand_t0 >= clip_start - 1e-6 and cand_t1 <= (clip_end - 0.1) + 1e-6
        if ok:
            print(f"  beat-snap: {w['assetId']}@{w['t0']:.2f}s → {cand_t0:.2f}s（位移 {dt:+.3f}s）")
            out.append({**w, "t0": cand_t0, "t1": cand_t1})
        else:
            print(f"  beat-snap: {w['assetId']}@{w['t0']:.2f}s 位移 {dt:+.3f}s 違反護欄，撤回維持原時間")
            out.append(w)
    return out


def _check_refine_deps():
    """啟動時依賴自檢（main() 綁定專案後、開始切片前呼叫一次）：嘗試偵測精修需要的
    模組（目前程式碼裡只有 numpy，見 _tail_envelope/_compute_tail_refine/
    _compute_head_refine 的 import）。用 find_spec 偵測、不實際 import，純提示。
    缺了就醒目印出「本次會略過精修」＋指路（有 .venv 就給可複製指令，沒有就說明
    缺哪些套件）；絕不自動重新執行自己、不自動切換直譯器。"""
    required = ["numpy"]
    missing = [m for m in required if importlib.util.find_spec(m) is None]
    if not missing:
        return
    venv_py = ROOT / ".venv" / "bin" / "python"
    mods = "、".join(missing)
    print("", file=sys.stderr)
    print("!" * 70, file=sys.stderr)
    print(f"⚠⚠⚠ 目前直譯器（{sys.executable}）缺少模組：{mods} ⚠⚠⚠", file=sys.stderr)
    print("    本次執行將略過切點能量精修（尾字被剪風險未經防護）。", file=sys.stderr)
    if venv_py.exists():
        print(f"    → 改用 .venv/bin/python 執行即可啟用精修：{_reexec_hint_cmd(venv_py)}",
              file=sys.stderr)
    else:
        print(f"    → 找不到 {venv_py}；請安裝 {mods} 後再渲染（或建立 .venv）。",
              file=sys.stderr)
    print("!" * 70, file=sys.stderr)
    print("", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    project_paths.add_project_arg(ap)
    ap.add_argument("--radio", action="store_true")
    ap.add_argument("--no-broll", action="store_true")
    ap.add_argument("--hook", dest="hook", action="store_true", default=None,
                    help="強制加精華片頭（覆蓋 EDL）")
    ap.add_argument("--no-hook", dest="hook", action="store_false",
                    help="關閉精華片頭，直接從正片開始")
    ap.add_argument("--single", action="store_true",
                    help="用單一 filtergraph 一次渲染（快，但高負載下 ffmpeg 7.1 易逾時）；"
                         "預設為抗高負載的多 pass 版")
    ap.add_argument("--no-snap", action="store_true",
                    help="關閉 B-roll 畫面切換點卡拍（beat snap）；預設開啟")
    ap.add_argument("--beat-shift", type=float, default=None, metavar="SEC",
                    help="beat snap 最大位移秒數（預設 0.15，或讀 config.json 的 "
                         "broll.beatMaxShift）")
    ap.add_argument("--no-tail-refine", dest="tail_refine", action="store_false",
                    help="關閉尾切點能量谷精修（回到只信 whisper 時間碼的切點）")
    ap.add_argument("--preview", action="store_true",
                    help="快速預覽模式：強制 1280x720 / veryfast / crf22（改 EDL 後確認"
                         "節奏用）。預設走專案 manifest 的 target.resolution（未設則 1080p）"
                         "＋ medium / crf19 品質模式")
    args = ap.parse_args()

    global PREVIEW_MODE          # 須在 _bind_project → _bind_video_profile 之前設定
    PREVIEW_MODE = args.preview

    with project_paths.cli_guard():
        proj = project_paths.load(args.project)
        _bind_project(proj)

    global TAIL_REFINE
    TAIL_REFINE = args.tail_refine
    if TAIL_REFINE:   # 使用者明確 --no-tail-refine 時無需檢查（精修本來就不會跑）
        _check_refine_deps()

    edl = json.loads(EDL.read_text(encoding="utf-8"))
    aroll = edl.get("aroll", [])
    if not aroll:
        print("EDL 無 aroll", file=sys.stderr); sys.exit(1)
    OUT.mkdir(parents=True, exist_ok=True)
    _preflight_sources(aroll, edl.get("broll", []))
    segs_by_asset = load_segments_by_asset()

    # 精華片頭開關：預設依 EDL hookCount；--no-hook 關閉、--hook 強制開
    edl_hookcount = edl.get("hookCount", 0)
    use_hook = (edl_hookcount > 0) if args.hook is None else args.hook
    if not use_hook:
        aroll = [c for c in aroll if c.get("chapter") != "hook"]
        eff_hookcount = 0
    else:
        eff_hookcount = sum(1 for c in aroll if c.get("chapter") == "hook") or edl_hookcount

    concat, total, placed = cut_aroll(aroll, segs_by_asset)
    picture = WORK / "picture.mp4"
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", str(concat),
         "-c:v", "copy", "-af", "asetpts=N/SR/TB", "-c:a", "aac", "-b:a", "160k",
         str(picture)])

    if args.radio:
        radio = OUT / project_paths.RADIO_MP4
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-i", str(picture), "-c", "copy", "-movflags", "+faststart", str(radio)])
        print(f"radio edit → {radio}  ({total:.1f}s, {len(aroll)} clips)")
        degradation = dep_degradation_summary()
        if degradation:
            print(degradation)
        return

    brolls = [] if args.no_broll else edl.get("broll", [])
    windows = broll_windows(placed, brolls)

    raw_music = edl.get("music")                                          # dict / false / 省略
    music_cfg = raw_music if isinstance(raw_music, dict) else {}
    cues = resolve_cue_times(music_cfg.get("cues", []), placed, total)   # 章節→秒（提前算，供 beat-snap 用）

    # beat 對齊：只吸附 B-roll 畫面切換點的進點，A-roll timeline / 字幕完全不動
    # （見 analysis/p1/beat_align_design.md）。config.json 唯讀，只用 .get 容錯讀取。
    p1_cfg = {}
    if CONFIG.exists():
        try:
            p1_cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            p1_cfg = {}
    broll_cfg = p1_cfg.get("broll", {}) if isinstance(p1_cfg, dict) else {}
    snap_enabled = bool(broll_cfg.get("snapToBeat", True)) and not args.no_snap
    beat_max_shift = (args.beat_shift if args.beat_shift is not None
                       else float(broll_cfg.get("beatMaxShift", 0.15)))
    if snap_enabled and windows:
        windows = snap_windows_to_beats(windows, cues, placed, max_shift=beat_max_shift)

    srt = build_srt(placed)
    subq = str(srt).replace(":", r"\:").replace(",", r"\,")
    final = OUT / project_paths.FINAL_MP4
    dur = int(round(total))
    bed = build_music_bed(cues, dur)                                     # 多首段落切換
    music_file = None if bed else resolve_music_file(raw_music)
    if not bed:
        print("  背景音樂：" + (f"單曲循環 {music_file.name}" if music_file else
              "無（EDL 設為 false）" if raw_music is False else
              "無（EDL 未指定、素材資料夾也沒有 mp3）"))
    if bed:
        print("  背景音樂：{} 段切換 ".format(len(cues)) +
              " | ".join("{}@{:.0f}-{:.0f}s".format(
                  c.get("file", "?").split(" - ")[0], c.get("fromSec", 0), c.get("toSec", 0))
                  for c in cues))
    # 精華片頭時長（前 hookCount 段的 timeline 長度）→ 期間顯示「搶先看」字卡
    hook_end = sum(p["dur"] for p in placed[:eff_hookcount]) if eff_hookcount else 0.0

    if args.single:
        render_singlepass(picture, windows, bed, music_file, subq, dur, hook_end, final)
    else:
        render_multipass(picture, windows, bed, music_file, subq, dur, total, hook_end, final)
    print(f"  B-roll cutaways: {len(windows)} 段")
    run(["ffprobe", "-v", "error", "-show_entries",
         "format=filename,duration,size:stream=codec_type,codec_name,width,height,r_frame_rate",
         "-of", "json", str(final)])
    print(f"final → {final}  ({total:.1f}s, {len(aroll)} clips, "
          f"{len(brolls) if not args.no_broll else 0} broll)")
    print(f"srt   → {srt}")
    degradation = dep_degradation_summary()
    if degradation:
        print(degradation)


if __name__ == "__main__":
    main()
