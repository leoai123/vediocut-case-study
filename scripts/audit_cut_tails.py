#!/usr/bin/env python3
"""
audit_cut_tails.py — 切點稽核工具（獨立於 render_p1.py）。

目的：
  whisper 逐字稿時間碼不可盡信（已知案例：a12「好吃」的「吃」被剪、a14「賺100點」
  後多吃了半句）。這支工具對 edit_decision.json 全部 aroll 段的「尾切點」
  （sourceOut）做獨立的音訊能量量測，找出「切點落在語音中」或「whisper 標籤與
  實際語音明顯不合」的段落，供人工複核，不依賴、不修改渲染器實作。

方法（重要：這批素材是嘈雜賣場實拍，環境噪音本身就有 -20~-30dBFS，不會有乾淨錄音室
式的深度靜音；純粹「相對窗內峰值多少 dB」的絕對門檻在這類素材上會過度觸發，因此除了
回報 spec 要求的 cutEnergy / nearestDip 兩個量測值之外，verdict 判斷改用「切點前後能量
落差（decay）」為主要依據 —— 這是本工具實際驗證後對 a12 / a14 兩個已知錨點都能正確
判別的方法）：

  1. 對每段 aroll，從來源 MOV 抽出 sourceOut 前後 0.8s 的音訊，用 ffmpeg 解出
     PCM，再用 25ms 短窗（10ms hop）算 RMS 能量包絡（dBFS）。
  2. 回報 spec 要求的 cutEnergy：sourceOut / sourceOut+0.05 / sourceOut+0.55 三個
     候選切點，相對窗內能量峰值的差（dB，越大越安靜／越安全）；並在包絡上找
     nearestDip（窗內最近的合格安靜谷，位置與深度）。這兩者作為人工複核的輔助資訊。
  3. verdict 判斷主依據「decay」：比較 sourceOut 前一小段語音能量（pre，[-0.35,-0.03]s）
     與切點後兩段（post1 立即 [+0.03,+0.35]s、post2 延遲 [+0.35,+0.70]s）的能量落差：
       - drop1 = pre - post1，drop2 = pre - post2，comeback = drop1 - drop2
       - comeback 明顯為正（切點後先降一點、之後又回升到接近 pre 的能量）且回升處
         本身有實質能量（超過該素材的動態底噪門檻）→ SUSPECT_LABEL
         （whisper 尾標太早，實際語音在 0.5s 後又繼續，如 a12「好吃」被剪即此模式）
       - pre 明顯比更早的 farPre（[-0.8,-0.4]s）安靜很多，且 pre 本身已在底噪
         → SUSPECT_LABEL（whisper 尾標偏晚，實際語音提早結束）
       - post1 有實質能量、且 drop1 很小（幾乎沒降或反而變大聲）→ RISK_TAIL_CLIP
         （切點當下仍在講話）
       - drop1 夠大 → OK；drop1 中等但 drop2 夠大 → OK（稍慢的自然收音尾音）；
         其餘中間地帶 → RISK_TAIL_CLIP（建議人工複核）
  4. 對 verdict != OK 的段落，額外用 whisper-cli 對 [sourceOut-3s, sourceOut+0.6s]
     重新轉錄，記錄「聽到的實際結尾」供比對 EDL text。
  5. 順帶輕量檢查頭切點（sourceIn 前 0.3s 是否仍有實質語音能量）。

輸出：
  analysis/<project>/cut_tail_audit.md    人類可讀報告
  analysis/<project>/cut_tail_audit.json  機器可讀明細

用法：
  python3 scripts/audit_cut_tails.py --project p1
  python3 scripts/audit_cut_tails.py --project p1 --skip-asr        # 跳過 whisper 複核（快）
  python3 scripts/audit_cut_tails.py --project p1 --only a12,a14    # 只跑指定段（除錯用）
  python3 scripts/audit_cut_tails.py --project p1 --asr-all         # 對全部段（含 OK）都跑 ASR
  未帶 --project 時，走 app/project_paths.load() 的解析順序
  （明確參數 > VEDIOCUT_PROJ > current_project 指標 > 唯一專案）。

僅新增檔案，不讀取/依賴 render_p1.py 的實作細節；只讀 edit_decision.json 與
transcript.json，音訊全部從專案來源資料夾（Project.src）的 <assetId>.MOV 現場抽取量測。
"""
from __future__ import annotations

import argparse
import io
import json
import math
import shutil
import subprocess
import sys
import wave
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))   # 讓 `python3 scripts/audit_cut_tails.py` 也能 import app.*
import app.project_paths as project_paths  # noqa: E402  單一真相來源：路徑一律經此模組解析

# 來源資料夾一律綁到 app.project_paths 解析出的 Project 物件（見 main() 開頭）。
TEST_DATA_DIR: Path = None
DEFAULT_MODEL = ROOT / "models/ggml-medium.bin"

# ---------------------------------------------------------------------------
# 可調參數
# ---------------------------------------------------------------------------
WINDOW_RADIUS = 0.8          # sourceOut 前後抽多少秒音訊來分析
FRAME_MS = 25.0              # 短窗長度
HOP_MS = 10.0                # 短窗步長

# --- spec 要求回報的 cutEnergy / nearestDip（相對窗內峰值，dB）---
QUIET_REL_DB = 18.0          # 相對窗內峰值 >= 這個值(dB) 視為「合格安靜谷」
PADDING_OFFSETS = [0.0, 0.05, 0.55]   # cutEnergy 取樣點：sourceOut + offset

# --- verdict 判斷主依據：切點前後能量落差 (decay) ---
PRE_WINDOW = (-0.35, -0.03)   # 切點前：這段語音收尾前的能量參考
POST1_WINDOW = (0.03, 0.35)   # 切點後：立即窗
POST2_WINDOW = (0.35, 0.70)   # 切點後：延遲窗（用來抓「尾標太早、其實還在講」）
FARPRE_WINDOW = (-0.80, -0.40)  # 更早之前：用來抓「提前靜音、尾標偏晚」

GATE_MARGIN_DB = 12.0        # 該段「有實質語音能量」門檻 = 窗內峰值 - 這個值（每段動態校準，避免固定 dBFS 門檻在不同錄音環境下失準）
COMEBACK_THRESHOLD = 2.0     # drop1-drop2 >= 這個值 → 判定「切點後又回升」(SUSPECT，尾標太早)
EARLY_QUIET_DIFF = -5.0      # pre-farPre <= 這個值 → 判定「提前很多就已經安靜」(SUSPECT，尾標偏晚)
RISK_DROP1_DB = 1.0          # drop1 <= 這個值 → 切點當下幾乎沒有轉場（RISK，立即風險）
SAFE_DROP1_DB = 3.0          # drop1 >= 這個值 → 判定 OK
SAFE_DROP2_TIEBREAK_DB = 4.0  # drop1 中間地帶時，drop2 >= 這個值 → 仍判 OK（收尾稍慢但確實安靜下來）

HEAD_WINDOW = 0.5             # 頭切點分析半徑
HEAD_PRE_WINDOW = (-0.30, -0.02)   # sourceIn 前一小段（若有實質能量代表切在語音中）

ASR_LOOKBACK = 3.0            # ASR 複核：sourceOut 前回溯秒數
ASR_LOOKAHEAD = 0.6           # ASR 複核：sourceOut 後延伸秒數

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"
WHISPER_CLI = shutil.which("whisper-cli") or "whisper-cli"

_duration_cache: dict[str, float] = {}


# ---------------------------------------------------------------------------
# 音訊工具
# ---------------------------------------------------------------------------

def get_duration(src: Path) -> float:
    key = str(src)
    if key in _duration_cache:
        return _duration_cache[key]
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nk=1:nw=1", str(src)],
        capture_output=True, text=True, check=True,
    )
    dur = float(out.stdout.strip())
    _duration_cache[key] = dur
    return dur


def extract_pcm(src: Path, start: float, dur: float, sr: int = 16000) -> tuple[np.ndarray, int]:
    """精確抽取 [start, start+dur) 的單聲道 PCM16（-ss 放在 -i 之後以求精確 seek）。"""
    start = max(0.0, start)
    dur = max(0.01, dur)
    cmd = [
        FFMPEG, "-v", "error",
        "-i", str(src),
        "-ss", f"{start:.6f}", "-t", f"{dur:.6f}",
        "-vn", "-ac", "1", "-ar", str(sr), "-acodec", "pcm_s16le",
        "-f", "wav", "pipe:1",
    ]
    proc = subprocess.run(cmd, capture_output=True, check=True)
    with wave.open(io.BytesIO(proc.stdout)) as w:
        n = w.getnframes()
        raw = w.readframes(n)
        actual_sr = w.getframerate()
    samples = np.frombuffer(raw, dtype=np.int16)
    return samples, actual_sr


def compute_envelope(samples: np.ndarray, sr: int, frame_ms: float = FRAME_MS,
                      hop_ms: float = HOP_MS) -> tuple[np.ndarray, np.ndarray]:
    """回傳 (times_sec_relative_to_window_start, dbfs_array)。"""
    n = len(samples)
    if n == 0:
        return np.array([0.0]), np.array([-120.0])
    frame_len = max(1, int(round(sr * frame_ms / 1000)))
    hop_len = max(1, int(round(sr * hop_ms / 1000)))
    x = samples.astype(np.float64)
    if n <= frame_len:
        rms = math.sqrt(float(np.mean(x ** 2))) if n > 0 else 0.0
        db = 20 * math.log10(rms / 32768.0) if rms > 1e-6 else -120.0
        return np.array([n / (2 * sr)]), np.array([db])
    times = []
    dbfs = []
    i = 0
    while i < n:
        seg = x[i:i + frame_len]
        if len(seg) < frame_len * 0.5:
            break
        rms = math.sqrt(float(np.mean(seg ** 2)))
        db = 20 * math.log10(rms / 32768.0) if rms > 1e-6 else -120.0
        center_t = (i + len(seg) / 2.0) / sr
        times.append(center_t)
        dbfs.append(db)
        if i + frame_len >= n:
            break
        i += hop_len
    return np.array(times), np.array(dbfs)


def interp(times: np.ndarray, values: np.ndarray, t: float) -> float:
    if len(times) == 0:
        return float("nan")
    if t <= times[0]:
        return float(values[0])
    if t >= times[-1]:
        return float(values[-1])
    idx = int(np.searchsorted(times, t))
    t0, t1 = times[idx - 1], times[idx]
    v0, v1 = values[idx - 1], values[idx]
    if t1 == t0:
        return float(v0)
    frac = (t - t0) / (t1 - t0)
    return float(v0 + frac * (v1 - v0))


def avg_power_db(times_abs: np.ndarray, dbfs: np.ndarray, a: float, b: float) -> Optional[float]:
    """以線性功率平均（而非算術平均 dB）計算 [a,b] 區間的平均能量，回傳 dBFS；無資料回傳 None。"""
    mask = (times_abs >= a) & (times_abs <= b)
    if not np.any(mask):
        return None
    lin = 10 ** (dbfs[mask] / 20.0)
    m = float(np.mean(lin))
    if m <= 1e-9:
        return -120.0
    return float(20 * math.log10(m))


def find_dips(times_abs: np.ndarray, rel_db: np.ndarray, min_depth: float = QUIET_REL_DB):
    """找連續「安靜區間」(rel_db >= min_depth) 內最深(能量最低=rel_db最大)的點，
    每個區間回傳一個 dip 候選 {time, relDb}。"""
    dips = []
    n = len(rel_db)
    i = 0
    mask = rel_db >= min_depth
    while i < n:
        if mask[i]:
            j = i
            while j < n and mask[j]:
                j += 1
            seg_idx = range(i, j)
            best = max(seg_idx, key=lambda k: rel_db[k])
            dips.append({"time": float(times_abs[best]), "relDb": float(rel_db[best])})
            i = j
        else:
            i += 1
    return dips


# ---------------------------------------------------------------------------
# 資料結構
# ---------------------------------------------------------------------------

@dataclass
class TailAnalysis:
    id: str
    chapter: str
    assetId: str
    sourceIn: float
    sourceOut: float
    text: str
    windowStart: float
    windowEnd: float
    peakDbfs: float
    cutEnergy: dict            # spec 要求：{"sourceOut":dB, "sourceOut+0.05":dB, "sourceOut+0.55":dB}（相對窗峰值）
    cutAbsDbfs: dict           # 同上，絕對 dBFS
    nearestDip: Optional[dict]   # spec 要求：{"time","offsetFromCut","relDb"}
    decay: dict                # 判 verdict 用的主要量測：pre/post1/post2/drop1/drop2/comeback/farPre/gateDbfs
    verdict: str
    reason: str
    suggestion: str
    headRisk: bool
    headRelDb: Optional[float]
    headNote: str
    asrTailHeard: Optional[str] = None
    asrNote: Optional[str] = None


def analyze_tail(seg: dict) -> TailAnalysis:
    asset = seg["assetId"]
    src = TEST_DATA_DIR / f"{asset}.MOV"
    cut = float(seg["sourceOut"])
    src_in = float(seg["sourceIn"])
    duration = get_duration(src)

    win_start = max(0.0, cut - WINDOW_RADIUS)
    win_end = min(duration, cut + WINDOW_RADIUS)
    win_dur = win_end - win_start

    samples, sr = extract_pcm(src, win_start, win_dur)
    local_times, dbfs = compute_envelope(samples, sr)
    times_abs = local_times + win_start
    peak_dbfs = float(np.max(dbfs))
    rel_db = peak_dbfs - dbfs

    # --- spec 要求：cutEnergy（相對峰值 dB）與 nearestDip ---
    cut_energy: dict[str, Optional[float]] = {}
    cut_abs: dict[str, Optional[float]] = {}
    for off in PADDING_OFFSETS:
        t = cut + off
        label = "sourceOut" if off == 0.0 else f"sourceOut+{off:g}"
        if win_start <= t <= win_end:
            cut_energy[label] = round(interp(times_abs, rel_db, t), 2)
            cut_abs[label] = round(interp(times_abs, dbfs, t), 2)
        else:
            cut_energy[label] = None
            cut_abs[label] = None

    dips = find_dips(times_abs, rel_db, QUIET_REL_DB)
    nearest_dip = None
    if dips:
        nd = min(dips, key=lambda d: abs(d["time"] - cut))
        nearest_dip = {
            "time": round(nd["time"], 3),
            "offsetFromCut": round(nd["time"] - cut, 3),
            "relDb": round(nd["relDb"], 2),
        }

    # --- decay：判 verdict 的主要量測（切點前後能量落差，per-clip 動態門檻）---
    pre = avg_power_db(times_abs, dbfs, cut + PRE_WINDOW[0], cut + PRE_WINDOW[1])
    post1 = avg_power_db(times_abs, dbfs, cut + POST1_WINDOW[0], cut + POST1_WINDOW[1])
    post2 = avg_power_db(times_abs, dbfs, cut + POST2_WINDOW[0], cut + POST2_WINDOW[1])
    far_pre = avg_power_db(times_abs, dbfs, cut + FARPRE_WINDOW[0], cut + FARPRE_WINDOW[1])
    gate_dbfs = peak_dbfs - GATE_MARGIN_DB

    drop1 = None if (pre is None or post1 is None) else pre - post1
    drop2 = None if (pre is None or post2 is None) else pre - post2
    comeback = None if (drop1 is None or drop2 is None) else drop1 - drop2
    early_diff = None if (pre is None or far_pre is None) else pre - far_pre

    decay = {
        "preDbfs": None if pre is None else round(pre, 2),
        "post1Dbfs": None if post1 is None else round(post1, 2),
        "post2Dbfs": None if post2 is None else round(post2, 2),
        "farPreDbfs": None if far_pre is None else round(far_pre, 2),
        "gateDbfs": round(gate_dbfs, 2),
        "drop1": None if drop1 is None else round(drop1, 2),
        "drop2": None if drop2 is None else round(drop2, 2),
        "comeback": None if comeback is None else round(comeback, 2),
        "earlyQuietDiff": None if early_diff is None else round(early_diff, 2),
    }

    verdict = None
    reason = ""
    suggestion = ""

    # Case A（SUSPECT）：切點後先降、但延遲窗又回升到接近 pre 的能量 → whisper 尾標太早，語音其實還在繼續
    if (comeback is not None and comeback >= COMEBACK_THRESHOLD
            and post2 is not None and post2 >= gate_dbfs):
        verdict = "SUSPECT_LABEL"
        reason = (f"切點後短暫下降 (drop1={drop1:.1f}dB)，但 0.35~0.7s 後能量又回升到接近切點前水準 "
                   f"(drop2={drop2:.1f}dB，回升 {comeback:.1f}dB) → whisper 尾標可能太早，實際語音延續到切點後")
        suggestion = "建議用 whisper-cli 對尾段重新轉錄核對，並將 sourceOut 延後到真正安靜處（見 asrTailHeard）"

    # Case B（SUSPECT）：切點前一小段已經比更早之前安靜很多 → whisper 尾標偏晚，實際語音可能提早結束
    if verdict is None and (early_diff is not None and early_diff <= EARLY_QUIET_DIFF
                             and pre is not None and pre <= gate_dbfs):
        verdict = "SUSPECT_LABEL"
        reason = (f"sourceOut 前 0.35s 內已經比再更早之前 (-0.8~-0.4s) 安靜了 {abs(early_diff):.1f}dB，"
                   f"且已低於本段動態門檻 → 實際語音可能提早結束，whisper 尾標偏晚，"
                   f"EDL text 最後幾字未必真的有完整講出來")
        suggestion = "建議核對 EDL text 最後幾字是否真的有講出來（見 asrTailHeard），必要時提前 sourceOut"

    # 立即風險：切點後馬上仍有實質能量、且幾乎沒有下降（甚至變大聲）
    if verdict is None and post1 is not None and post1 >= gate_dbfs and drop1 is not None and drop1 <= RISK_DROP1_DB:
        verdict = "RISK_TAIL_CLIP"
        reason = (f"切點後立即窗仍有實質能量 (post1={post1:.1f}dBFS，高於本段門檻 {gate_dbfs:.1f}dBFS)，"
                   f"且幾乎沒有下降 (drop1={drop1:.1f}dB) → 疑似切點當下仍在講話")
        if nearest_dip and nearest_dip["offsetFromCut"] < 0 and abs(nearest_dip["offsetFromCut"]) <= WINDOW_RADIUS:
            suggestion = (f"建議把 sourceOut 提前到 {nearest_dip['time']:.2f}s "
                           f"(相對 sourceOut {nearest_dip['offsetFromCut']:+.2f}s，安靜深度 {nearest_dip['relDb']:.1f}dB)")
        else:
            suggestion = "窗內找不到明顯安全靜音點，建議人工聽過整段尾巴後決定新切點"

    if verdict is None and drop1 is not None and drop1 >= SAFE_DROP1_DB:
        verdict = "OK"
        reason = f"切點後能量明顯下降 (drop1={drop1:.1f}dB)，已脫離語音"
        suggestion = "維持現有 sourceOut"

    if verdict is None:
        # 中間地帶：用 drop2 判斷是否只是收尾稍慢
        if drop2 is not None and drop2 >= SAFE_DROP2_TIEBREAK_DB:
            verdict = "OK"
            reason = (f"切點後立即下降幅度中等 (drop1={None if drop1 is None else round(drop1,1)}dB)，"
                       f"但延遲窗確認持續安靜 (drop2={drop2:.1f}dB)")
            suggestion = "維持現有 sourceOut，渲染時建議保留 >=0.05s 尾端 padding"
        else:
            verdict = "RISK_TAIL_CLIP"
            reason = (f"切點前後能量落差不夠明確 (drop1={None if drop1 is None else round(drop1,1)}dB, "
                       f"drop2={None if drop2 is None else round(drop2,1)}dB)，落在中間地帶")
            if nearest_dip and nearest_dip["offsetFromCut"] < 0 and abs(nearest_dip["offsetFromCut"]) <= WINDOW_RADIUS:
                suggestion = (f"建議人工複核；窗內較近的安靜點在 {nearest_dip['time']:.2f}s "
                               f"(相對 sourceOut {nearest_dip['offsetFromCut']:+.2f}s)")
            else:
                suggestion = "建議人工複核，窗內無明顯更安全的靜音點"

    # --- 頭切點輕量檢查（spec 定義為「順帶、輕量即可」，粗略指標而非高信心判定）---
    # 這批素材多半是「精華剪輯」風格：sourceIn 常常本來就落在一段連續錄影的中段，
    # 前面本來就有其他語音內容是正常現象（不是被剪到），除非 sourceIn 極接近檔案開頭
    # （代表這是一個全新片段的開始，前方沒有可講話的空間，可略過檢查）。
    head_risk = False
    head_rel_db: Optional[float] = None
    head_note = ""
    try:
        if src_in < 1.2:
            head_note = "sourceIn 接近檔案開頭（新片段起始），略過頭切點檢查"
        else:
            h_win_start = max(0.0, src_in - HEAD_WINDOW)
            h_win_end = min(duration, src_in + HEAD_WINDOW)
            h_dur = h_win_end - h_win_start
            if h_dur > 0.05:
                h_samples, h_sr = extract_pcm(src, h_win_start, h_dur)
                h_times, h_dbfs = compute_envelope(h_samples, h_sr)
                h_times_abs = h_times + h_win_start
                h_peak = float(np.max(h_dbfs))
                h_gate = h_peak - GATE_MARGIN_DB
                head_pre = avg_power_db(h_times_abs, h_dbfs, src_in + HEAD_PRE_WINDOW[0], src_in + HEAD_PRE_WINDOW[1])
                if head_pre is not None:
                    head_rel_db = round(h_peak - head_pre, 2)
                    if head_pre >= h_gate:
                        head_risk = True
                        head_note = (f"sourceIn 前 0.3s 仍有實質能量 (head_pre={head_pre:.1f}dBFS >= "
                                       f"門檻 {h_gate:.1f}dBFS)。粗略指標：由於本片是精華剪輯，"
                                       f"sourceIn 前方本來就常有未選用的語音內容屬正常，"
                                       f"僅列出供人工快速抽聽，非高信心風險判定")
                    else:
                        head_note = "頭切點前方安靜，無風險"
                else:
                    head_note = "視窗超出範圍，未檢查"
    except Exception as e:  # 頭檢查是輕量附帶功能，失敗不應中斷主流程
        head_note = f"頭切點檢查失敗: {e}"

    return TailAnalysis(
        id=seg["id"], chapter=seg.get("chapter", ""), assetId=asset,
        sourceIn=src_in, sourceOut=cut, text=seg.get("text", ""),
        windowStart=round(win_start, 3), windowEnd=round(win_end, 3),
        peakDbfs=round(peak_dbfs, 2),
        cutEnergy=cut_energy, cutAbsDbfs=cut_abs,
        nearestDip=nearest_dip, decay=decay,
        verdict=verdict, reason=reason, suggestion=suggestion,
        headRisk=head_risk, headRelDb=head_rel_db, headNote=head_note,
    )


# ---------------------------------------------------------------------------
# whisper-cli 尾段複核
# ---------------------------------------------------------------------------

def asr_tail_recheck(seg: dict, model: Path) -> tuple[Optional[str], Optional[str]]:
    asset = seg["assetId"]
    src = TEST_DATA_DIR / f"{asset}.MOV"
    duration = get_duration(src)
    cut = float(seg["sourceOut"])
    src_in = float(seg["sourceIn"])

    start = max(src_in, cut - ASR_LOOKBACK, 0.0)
    end = min(duration, cut + ASR_LOOKAHEAD)
    dur = end - start
    if dur <= 0.05:
        return None, "ASR 視窗過短，略過"

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        wav_path = Path(tmp) / "clip.wav"
        cmd_extract = [
            FFMPEG, "-v", "error", "-y",
            "-i", str(src), "-ss", f"{start:.6f}", "-t", f"{dur:.6f}",
            "-vn", "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le",
            str(wav_path),
        ]
        try:
            subprocess.run(cmd_extract, check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            return None, f"抽音訊失敗: {e.stderr.decode('utf-8', 'ignore')[:200]}"

        out_prefix = Path(tmp) / "asr_out"
        cmd_asr = [
            WHISPER_CLI, "-m", str(model), "-f", str(wav_path),
            "-l", "zh", "-nt", "-np",
            "-of", str(out_prefix),
        ]
        try:
            proc = subprocess.run(cmd_asr, check=True, capture_output=True, text=True, timeout=180)
            text = " ".join(proc.stdout.split())
            if not text:
                return None, "whisper-cli 未輸出文字"
            return text, f"ASR 視窗 [{start:.2f}s, {end:.2f}s]（sourceOut={cut:.2f}s）"
        except subprocess.CalledProcessError as e:
            return None, f"whisper-cli 失敗: {e.stderr[:300]}"
        except subprocess.TimeoutExpired:
            return None, "whisper-cli 逾時"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def load_aroll(edl_path: Path) -> list[dict]:
    data = json.loads(edl_path.read_text(encoding="utf-8"))
    return data["aroll"]


def build_markdown(results: list[TailAnalysis]) -> str:
    n_total = len(results)
    n_ok = sum(1 for r in results if r.verdict == "OK")
    n_risk = sum(1 for r in results if r.verdict == "RISK_TAIL_CLIP")
    n_suspect = sum(1 for r in results if r.verdict == "SUSPECT_LABEL")
    n_head_risk = sum(1 for r in results if r.headRisk)

    lines = []
    lines.append("# 切點稽核報告：A-roll 尾切點是否切到語音\n")
    lines.append("獨立稽核工具：`scripts/audit_cut_tails.py`（只讀 edit_decision.json，"
                  "音訊量測與 render_p1.py 完全獨立、不依賴其實作細節）。\n")
    lines.append(f"總段數：{n_total}　OK：{n_ok}　RISK_TAIL_CLIP：{n_risk}　"
                  f"SUSPECT_LABEL：{n_suspect}　頭切點抽聽提示：{n_head_risk}（粗略指標，見下方說明）\n")
    lines.append(
        "方法：ffmpeg 抽 sourceOut±0.8s 音訊，25ms/10ms-hop 短窗量 RMS 包絡(dBFS)。\n\n"
        "- `cutEnergy`（spec 要求的輔助量測）：候選切點（sourceOut / +0.05 / +0.55）相對窗內峰值的差，"
        "dB 越大代表越安靜。`nearestDip`：窗內最近的合格安靜谷（相對峰值 >= "
        f"{QUIET_REL_DB:.0f}dB）。\n"
        "- **verdict 判斷主依據是 decay（切點前後能量落差）**，而非固定 dB 門檻：這批賣場實拍素材"
        "本身環境噪音就有 -20~-30dBFS，用「絕對安靜」門檻幾乎每段都會誤判為風險；改比較切點前"
        "(`pre`) 與切點後立即窗(`post1`)/延遲窗(`post2`)的能量落差(`drop1`/`drop2`)，並用每段自己的"
        f"窗內峰值減 {GATE_MARGIN_DB:.0f}dB 當作『有無實質語音能量』的動態門檻(`gateDbfs`)。"
        "`comeback`(=drop1-drop2) 明顯為正代表切點後先降又回升 → whisper 尾標太早、語音其實還在"
        "繼續（SUSPECT_LABEL）；`drop1` 很小或為負代表切點當下仍在講話（RISK_TAIL_CLIP）。\n"
        "- verdict != OK 的段落另用 whisper-cli (models/ggml-medium.bin, -l zh -nt) 對尾段重新轉錄核對。\n"
        "- **如何解讀 asrTailHeard**：這批段落的複核結果幾乎都與 EDL text 的尾字「文字上」對得起來"
        "（沒有聽到 EDL 沒寫的額外字句），包括測試錨點 a12 也是如此 — whisper-cli 重新轉錄仍只聽到"
        "「好吃」兩字，沒有多轉出任何額外文字。這正說明這類 bug（同一個字的收尾音被切斷）屬於"
        "**次詞（sub-word）層級的音檔衰減問題**，不會在文字轉錄上留下痕跡，必須靠音檔能量本身"
        "（decay/comeback）才抓得到 —— ASR 複核只能用來確認『有沒有漏聽/多聽出完整的字句』，"
        "不能用來排除『同一個字尾音被切』的風險，兩者要一起看。\n"
    )

    lines.append("## 全部段落一覽\n")
    lines.append("| id | asset | chapter | sourceOut | cutEnergy(sourceOut/+0.05/+0.55, dB) | "
                  "decay(drop1/drop2/comeback, dB) | nearestDip(offset,depth) | verdict | headRisk |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in results:
        ce = r.cutEnergy
        ce_str = "{} / {} / {}".format(
            "n/a" if ce.get("sourceOut") is None else f"{ce['sourceOut']:.1f}",
            "n/a" if ce.get("sourceOut+0.05") is None else f"{ce['sourceOut+0.05']:.1f}",
            "n/a" if ce.get("sourceOut+0.55") is None else f"{ce['sourceOut+0.55']:.1f}",
        )
        d = r.decay
        decay_str = "{} / {} / {}".format(
            "n/a" if d.get("drop1") is None else f"{d['drop1']:.1f}",
            "n/a" if d.get("drop2") is None else f"{d['drop2']:.1f}",
            "n/a" if d.get("comeback") is None else f"{d['comeback']:.1f}",
        )
        if r.nearestDip:
            dip_str = f"{r.nearestDip['offsetFromCut']:+.2f}s, {r.nearestDip['relDb']:.1f}dB"
        else:
            dip_str = "無合格安靜谷"
        verdict_mark = {"OK": "OK", "RISK_TAIL_CLIP": "**RISK_TAIL_CLIP**", "SUSPECT_LABEL": "**SUSPECT_LABEL**"}[r.verdict]
        head_mark = "有風險" if r.headRisk else "-"
        lines.append(f"| {r.id} | {r.assetId} | {r.chapter} | {r.sourceOut:.2f} | {ce_str} | "
                      f"{decay_str} | {dip_str} | {verdict_mark} | {head_mark} |")

    lines.append("\n## 需要處理的段落明細\n")
    flagged = [r for r in results if r.verdict != "OK" or r.headRisk]
    if not flagged:
        lines.append("無。所有段落尾切點皆判定安全。\n")
    else:
        for r in flagged:
            anchor_note = "（本工具的測試錨點：已知會被剪到「吃」字）" if r.id == "a12" else ""
            lines.append(f"### {r.id}（{r.assetId} / {r.chapter}）{anchor_note}\n")
            lines.append(f"- EDL text 尾句：「…{r.text[-16:]}」（sourceOut={r.sourceOut:.2f}s）")
            lines.append(f"- verdict：**{r.verdict}**")
            lines.append(f"- 判定理由：{r.reason}")
            lines.append(f"- 建議動作：{r.suggestion}")
            if r.asrTailHeard is not None:
                lines.append(f"- whisper-cli 尾段複核聽到：「{r.asrTailHeard}」（{r.asrNote}）")
            elif r.asrNote:
                lines.append(f"- ASR 複核：{r.asrNote}")
            if r.headRisk:
                lines.append(f"- 頭切點：{r.headNote}")
            lines.append("")

    lines.append("## 頭切點輕量檢查（全部段落，粗略指標，非高信心判定）\n")
    lines.append("本片是精華剪輯風格，sourceIn 前方本來就常有未選用的語音內容（同一顆鏡頭裡沒選進來的話），"
                  "這屬於正常剪輯手法，不代表切壞。這裡的「有風險」只是「sourceIn 前方仍量到實質能量」的粗略"
                  "提示，供人工快速抽聽是否恰好切在單一個字/詞中間，不是可信的自動判定，建議只作為抽查參考。\n")
    lines.append("| id | asset | sourceIn | headRelDb | 結果 |")
    lines.append("|---|---|---|---|---|")
    for r in results:
        hrd = "n/a" if r.headRelDb is None else f"{r.headRelDb:.1f}"
        mark = "有能量(建議抽聽)" if r.headRisk else "OK"
        lines.append(f"| {r.id} | {r.assetId} | {r.sourceIn:.2f} | {hrd} | {mark} |")

    lines.append("")
    return "\n".join(lines)


def main():
    global TEST_DATA_DIR
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    project_paths.add_project_arg(ap)
    ap.add_argument("--edl", type=Path, default=None,
                    help="預設：所選專案的 edit_decision.json")
    ap.add_argument("--out-json", type=Path, default=None,
                    help=f"預設：analysis/<project>/{project_paths.AUDIT_JSON}")
    ap.add_argument("--out-md", type=Path, default=None,
                    help=f"預設：analysis/<project>/{project_paths.AUDIT_MD}")
    ap.add_argument("--asr-model", type=Path, default=DEFAULT_MODEL)
    ap.add_argument("--skip-asr", action="store_true", help="跳過 whisper-cli 尾段複核")
    ap.add_argument("--asr-all", action="store_true", help="對全部段落（含 OK）都做 ASR 複核")
    ap.add_argument("--only", type=str, default=None, help="逗號分隔的 id 清單，只跑指定段（除錯用）")
    args = ap.parse_args()

    with project_paths.cli_guard():
        proj = project_paths.load(args.project)
        TEST_DATA_DIR = proj.src
        args.edl = args.edl or proj.edl
        args.out_json = args.out_json or (proj.analysis / project_paths.AUDIT_JSON)
        args.out_md = args.out_md or (proj.analysis / project_paths.AUDIT_MD)

    aroll = load_aroll(args.edl)
    if args.only:
        wanted = set(args.only.split(","))
        aroll = [s for s in aroll if s["id"] in wanted]

    results: list[TailAnalysis] = []
    for seg in aroll:
        print(f"[audit] 分析 {seg['id']} ({seg['assetId']}, sourceOut={seg['sourceOut']})...", file=sys.stderr)
        try:
            r = analyze_tail(seg)
        except Exception as e:
            print(f"[audit] {seg['id']} 分析失敗: {e}", file=sys.stderr)
            r = TailAnalysis(
                id=seg["id"], chapter=seg.get("chapter", ""), assetId=seg["assetId"],
                sourceIn=float(seg["sourceIn"]), sourceOut=float(seg["sourceOut"]), text=seg.get("text", ""),
                windowStart=0, windowEnd=0, peakDbfs=float("nan"),
                cutEnergy={}, cutAbsDbfs={}, nearestDip=None, decay={},
                verdict="RISK_TAIL_CLIP", reason=f"分析例外: {e}", suggestion="人工複核",
                headRisk=False, headRelDb=None, headNote="",
            )
        results.append(r)

        need_asr = args.asr_all or r.verdict != "OK"
        if need_asr and not args.skip_asr:
            if not args.asr_model.exists():
                r.asrNote = f"找不到模型 {args.asr_model}，跳過 ASR"
            else:
                print(f"[audit]   -> verdict={r.verdict}，用 whisper-cli 複核尾段...", file=sys.stderr)
                heard, note = asr_tail_recheck(seg, args.asr_model)
                r.asrTailHeard = heard
                r.asrNote = note

    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schemaVersion": 1,
        "tool": "scripts/audit_cut_tails.py",
        "params": {
            "windowRadius": WINDOW_RADIUS, "frameMs": FRAME_MS, "hopMs": HOP_MS,
            "quietRelDb": QUIET_REL_DB, "paddingOffsets": PADDING_OFFSETS,
            "preWindow": PRE_WINDOW, "post1Window": POST1_WINDOW, "post2Window": POST2_WINDOW,
            "farPreWindow": FARPRE_WINDOW, "gateMarginDb": GATE_MARGIN_DB,
            "comebackThreshold": COMEBACK_THRESHOLD, "earlyQuietDiff": EARLY_QUIET_DIFF,
            "riskDrop1Db": RISK_DROP1_DB, "safeDrop1Db": SAFE_DROP1_DB,
            "safeDrop2TiebreakDb": SAFE_DROP2_TIEBREAK_DB,
        },
        "summary": {
            "total": len(results),
            "ok": sum(1 for r in results if r.verdict == "OK"),
            "riskTailClip": sum(1 for r in results if r.verdict == "RISK_TAIL_CLIP"),
            "suspectLabel": sum(1 for r in results if r.verdict == "SUSPECT_LABEL"),
            "headRisk": sum(1 for r in results if r.headRisk),
        },
        "segments": [asdict(r) for r in results],
    }
    args.out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    md = build_markdown(results)
    args.out_md.write_text(md, encoding="utf-8")

    print(f"\n[audit] 完成。共 {len(results)} 段。", file=sys.stderr)
    print(f"[audit]  OK={payload['summary']['ok']} "
          f"RISK_TAIL_CLIP={payload['summary']['riskTailClip']} "
          f"SUSPECT_LABEL={payload['summary']['suspectLabel']} "
          f"headRisk={payload['summary']['headRisk']}", file=sys.stderr)
    print(f"[audit] 報告: {args.out_md}", file=sys.stderr)
    print(f"[audit] JSON: {args.out_json}", file=sys.stderr)


if __name__ == "__main__":
    main()
