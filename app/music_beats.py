#!/usr/bin/env python3
"""音樂 beat 分析：偵測一首背景音樂的 tempo(BPM)、beat 時間點、能量曲線，
輸出 analysis/p1/beats/<track>.json，供「B-roll / montage 切換吸附拍點」使用。

設計原則（詳見 analysis/p1/beat_align_design.md）：
  對話完整性優先於卡拍——只有 montage / 風景 / B-roll 段才吸附 beat，
  A-roll 對話切點永遠不動。本模組只負責「產生拍點 grid」與「把想吸附的時間點
  吸到最近 beat（含最大位移上限）」，不碰 render / EDL。

偵測後端（自動挑選，遇重裝失敗會退場、不卡住）：
  1. librosa（優先）：beat_track + onset_strength + rms，準且穩。
  2. ffmpeg + numpy（後備）：ffmpeg 解成單聲道 PCM，用 spectral-flux 算 onset
     envelope，自相關估 tempo，再用 comb-filter 找相位、產生等距 beat grid。
  兩者輸出同一份 JSON schema（method 欄位標明用哪個後端）。

用法：
  python3 app/music_beats.py --project myvlog "footage/myvlog/bgm.mp3"
  python3 app/music_beats.py --project myvlog --all       # 分析該專案 srcDir 下的 *.mp3
  python3 app/music_beats.py --project p1 <mp3> --bpm-min 70 --bpm-max 160
  python3 app/music_beats.py --project p1 <mp3> --backend ffmpeg   # 強制用後備法

程式介面（供整合使用）：
  analyze_track(path) -> dict            # 完整分析結果（含 beats / bpm / energy）
  snap_to_beats(times, beats, max_shift=0.15)        # 把時間點吸到最近 beat
  beats_on_timeline(beats, from_sec, to_sec, track_len)  # 把 track 拍點展開到 timeline 秒
"""
import argparse
import json
import re
import subprocess
import sys
from bisect import bisect_left
from datetime import datetime, timezone
from pathlib import Path

try:
    import project_paths                      # python3 app/X.py 直接執行（app/ 在 sys.path）
except ModuleNotFoundError:                   # 被以 app.X 匯入時（如 render_p1 匯入 music_beats）
    from app import project_paths

ROOT = Path(__file__).resolve().parents[1]

SR = 22050            # 分析用取樣率（beat 偵測不需高頻，降採省時）
HOP = 512             # onset envelope 的 hop（frame 率 = SR/HOP ≈ 43.07 fps）
WIN = 1024            # STFT 視窗
BPM_MIN = 70.0        # 合理 tempo 下限（避免自相關挑到半拍）
BPM_MAX = 180.0       # 合理 tempo 上限（避免挑到雙倍拍）
ENERGY_HZ = 4.0       # 輸出能量曲線的取樣率（每秒 4 點，夠標段落強弱、又不肥）


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def slugify(name: str) -> str:
    """檔名 → 安全 slug：'My Song - Artist.mp3' → 'my-song-artist'。"""
    stem = Path(name).stem.lower()
    stem = re.sub(r"[^\w一-鿿]+", "-", stem, flags=re.UNICODE)
    return stem.strip("-") or "track"


def _ffprobe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def _decode_pcm(path: Path, sr: int = SR):
    """用 ffmpeg 把音檔解成單聲道 float32 PCM，回傳 numpy 陣列（範圍約 [-1,1]）。"""
    import numpy as np
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path),
         "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
        check=True, capture_output=True)
    return np.frombuffer(proc.stdout, dtype="<f4").astype(np.float32)


# --------------------------------------------------------------------------- #
# 後備法：ffmpeg + numpy（spectral flux onset → 自相關 tempo → comb 相位 beat grid）
# --------------------------------------------------------------------------- #
def _onset_envelope(y, sr=SR, hop=HOP, win=WIN):
    """Spectral-flux onset envelope：相鄰 frame 幅度譜的正向差之和。回傳 (env, fps)。"""
    import numpy as np
    fps = sr / hop
    if len(y) < win + hop:
        return np.zeros(1, dtype=np.float32), fps
    window = np.hanning(win).astype(np.float32)
    n_frames = 1 + (len(y) - win) // hop
    idx = np.arange(win)[None, :] + hop * np.arange(n_frames)[:, None]
    frames = y[idx] * window                       # (n_frames, win)
    mag = np.abs(np.fft.rfft(frames, axis=1))      # (n_frames, win/2+1)
    flux = np.maximum(np.diff(mag, axis=0), 0.0).sum(axis=1)
    flux = np.concatenate([[0.0], flux]).astype(np.float32)
    m = float(flux.max())
    if m > 0:
        flux /= m
    return flux, fps


def _estimate_tempo(onset_env, fps, bpm_min=BPM_MIN, bpm_max=BPM_MAX):
    """自相關 onset envelope，在合理 BPM 範圍內取峰值 → tempo。"""
    import numpy as np
    o = onset_env - onset_env.mean()
    if o.size < 4 or not np.any(o):
        return 0.0
    ac = np.correlate(o, o, mode="full")[o.size - 1:]     # lag >= 0
    lag_min = max(1, int(round(fps * 60.0 / bpm_max)))
    lag_max = min(ac.size - 1, int(round(fps * 60.0 / bpm_min)))
    if lag_max <= lag_min:
        return 0.0
    seg = ac[lag_min:lag_max + 1]
    best_lag = int(np.argmax(seg)) + lag_min
    # 拋物線插值精修峰值位置，tempo 更準
    if 0 < best_lag < ac.size - 1:
        a, b, c = ac[best_lag - 1], ac[best_lag], ac[best_lag + 1]
        denom = (a - 2 * b + c)
        if denom != 0:
            best_lag = best_lag + 0.5 * (a - c) / denom
    return float(60.0 * fps / best_lag) if best_lag else 0.0


def _beat_grid(onset_env, fps, bpm, duration):
    """已知 tempo，用 comb-filter 找最佳相位偏移，產生等距 beat grid（秒）。"""
    import numpy as np
    if bpm <= 0:
        return []
    period = fps * 60.0 / bpm            # 每拍 frame 數（float）
    P = int(round(period))
    n = onset_env.size
    if P < 1 or n < P:
        return []
    best_phi, best_score = 0, -1.0
    for phi in range(P):                 # 整數相位掃描（P 很小，約 20~40）
        score = float(onset_env[phi::P].sum())
        if score > best_score:
            best_score, best_phi = score, phi
    beats, k = [], 0
    while True:
        t = (best_phi + k * period) / fps    # 用 float period 累加避免漂移
        if t > duration:
            break
        beats.append(round(t, 3))
        k += 1
    return beats


def _energy_curve(y, sr=SR, out_hz=ENERGY_HZ):
    """粗粒度 RMS 能量曲線（正規化到 0~1），供設計上判斷段落強弱 / 找 drop。"""
    import numpy as np
    win = max(1, int(sr / out_hz))
    n = len(y) // win
    if n < 1:
        return {"hz": out_hz, "values": []}
    rms = np.sqrt((y[:n * win].reshape(n, win).astype(np.float64) ** 2).mean(axis=1))
    m = float(rms.max())
    if m > 0:
        rms = rms / m
    return {"hz": out_hz, "values": [round(float(v), 4) for v in rms]}


def _analyze_ffmpeg(path: Path, bpm_min=BPM_MIN, bpm_max=BPM_MAX):
    import numpy as np
    y = _decode_pcm(path, SR)
    duration = len(y) / SR
    onset_env, fps = _onset_envelope(y, SR)
    bpm = _estimate_tempo(onset_env, fps, bpm_min, bpm_max)
    beats = _beat_grid(onset_env, fps, bpm, duration)
    return {
        "bpm": round(bpm, 2),
        "beats": beats,
        "duration": round(duration, 3),
        "energy": _energy_curve(y, SR),
        "method": "ffmpeg+numpy-spectralflux",
        "onsetFps": round(fps, 3),
    }


# --------------------------------------------------------------------------- #
# 優先法：librosa
# --------------------------------------------------------------------------- #
def _analyze_librosa(path: Path, bpm_min=BPM_MIN, bpm_max=BPM_MAX):
    import librosa
    import numpy as np
    y, sr = librosa.load(str(path), sr=SR, mono=True)
    duration = float(len(y) / sr)
    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
    tempo, beat_frames = librosa.beat.beat_track(
        onset_envelope=onset_env, sr=sr, hop_length=HOP,
        start_bpm=120.0, units="frames")
    beats = [round(float(t), 3)
             for t in librosa.frames_to_time(beat_frames, sr=sr, hop_length=HOP)]
    bpm = float(np.atleast_1d(tempo)[0])
    return {
        "bpm": round(bpm, 2),
        "beats": beats,
        "duration": round(duration, 3),
        "energy": _energy_curve(y, sr),
        "method": "librosa",
        "onsetFps": round(sr / HOP, 3),
    }


# --------------------------------------------------------------------------- #
# 對外主入口
# --------------------------------------------------------------------------- #
def analyze_track(path, backend: str = "auto",
                  bpm_min: float = BPM_MIN, bpm_max: float = BPM_MAX) -> dict:
    """分析一首音樂，回傳含 bpm / beats / energy 的 dict。

    backend: 'auto'(先 librosa 後 ffmpeg) / 'librosa' / 'ffmpeg'。
    回傳鍵：track, path, method, bpm, medianBpm, beatCount, beats[],
            downbeats[], beatInterval, duration, energy, bpmRange, analyzedAt。
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"找不到音檔：{path}")

    order = {"auto": ["librosa", "ffmpeg"],
             "librosa": ["librosa"], "ffmpeg": ["ffmpeg"]}[backend]
    result, errors = None, []
    for be in order:
        try:
            if be == "librosa":
                result = _analyze_librosa(path, bpm_min, bpm_max)
            else:
                result = _analyze_ffmpeg(path, bpm_min, bpm_max)
            break
        except Exception as e:                          # noqa: BLE001 — 後端失敗要能退場
            errors.append(f"{be}: {type(e).__name__}: {e}")
            continue
    if result is None:
        raise RuntimeError("所有後端都失敗：\n  " + "\n  ".join(errors))

    beats = result["beats"]
    ibis = [round(b - a, 4) for a, b in zip(beats, beats[1:])]
    median_ibi = sorted(ibis)[len(ibis) // 2] if ibis else 0.0
    result.update({
        "track": path.name,
        "path": str(path.relative_to(ROOT)) if _under_root(path) else str(path),
        "beatCount": len(beats),
        "beatInterval": round(median_ibi, 4),
        "medianBpm": round(60.0 / median_ibi, 2) if median_ibi else 0.0,
        "downbeats": beats[::4],                          # 假設 4/4，每 4 拍一個小節首
        "bpmRange": [bpm_min, bpm_max],
        "analyzedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    if errors:
        result["backendFallback"] = errors
    return result


def _under_root(path: Path) -> bool:
    try:
        path.resolve().relative_to(ROOT)
        return True
    except ValueError:
        # 符號連結（素材資料夾 / models）指到專案根目錄外，用相對於未解析路徑判斷
        try:
            path.relative_to(ROOT)
            return True
        except ValueError:
            return False


def write_beats_json(result: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{slugify(result['track'])}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


# --------------------------------------------------------------------------- #
# 吸附 API
# --------------------------------------------------------------------------- #
def nearest_beat(t: float, beats):
    """回傳 beats 中離 t 最近的拍點（beats 需已排序）。空陣列回傳 t 本身。"""
    if not beats:
        return t
    i = bisect_left(beats, t)
    if i == 0:
        return beats[0]
    if i == len(beats):
        return beats[-1]
    lo, hi = beats[i - 1], beats[i]
    return lo if (t - lo) <= (hi - t) else hi


def snap_to_beats(times, beats, max_shift: float = 0.15, return_detail: bool = False):
    """把想吸附的時間點吸到最近 beat，位移超過 max_shift 就保留原值（不硬拉）。

    times / beats 必須同一時間基準（都用 track 秒，或都用 timeline 秒——見
    beats_on_timeline 負責換算）。beats 會自動排序。

    return_detail=False（預設）：回傳與 times 等長的 float 陣列（吸附後時間）。
    return_detail=True：回傳 dict 陣列
        {orig, snapped, beat, shift, locked}
      locked=True 代表成功吸附到 beat；False 代表最近 beat 太遠、維持原值。
    """
    bs = sorted(beats)
    out = []
    for t in times:
        nb = nearest_beat(t, bs)
        shift = nb - t
        locked = abs(shift) <= max_shift + 1e-9
        snapped = round(nb, 3) if locked else round(t, 3)
        if return_detail:
            out.append({"orig": round(t, 3), "snapped": snapped,
                        "beat": round(nb, 3), "shift": round(shift, 3),
                        "locked": locked})
        else:
            out.append(snapped)
    return out


def beats_on_timeline(beats, from_sec: float, to_sec: float, track_len: float = None):
    """把「track 相對」的拍點 grid 展開成 timeline 絕對秒（供 cue 窗 [from_sec,to_sec)）。

    對應 render_p1.build_music_bed 的行為：每個 cue 用 `-stream_loop -1 -t seg_dur`
    從 track 開頭重播，所以 track 第 b 秒的拍點會落在 timeline 的 from_sec + b。
    若 cue 比整首歌長（會 loop），傳入 track_len 讓拍點跟著每 track_len 秒重複。
    回傳落在 [from_sec, to_sec) 內、排序好的 timeline 秒拍點。
    """
    if not beats:
        return []
    span = to_sec - from_sec
    out = []
    if track_len and span > track_len:
        loops = int(span // track_len) + 1
        for n in range(loops):
            base = from_sec + n * track_len
            for b in beats:
                t = base + b
                if from_sec <= t < to_sec:
                    out.append(round(t, 3))
    else:
        for b in beats:
            t = from_sec + b
            if from_sec <= t < to_sec:
                out.append(round(t, 3))
    return sorted(out)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _print_summary(res: dict, out_path: Path):
    print(f"[{res['track']}]  method={res['method']}")
    print(f"  BPM={res['bpm']}  (median-IBI BPM={res['medianBpm']}, "
          f"beatInterval={res['beatInterval']}s)")
    print(f"  duration={res['duration']}s  beatCount={res['beatCount']}  "
          f"downbeats={len(res['downbeats'])}")
    head = ", ".join(f"{b:.3f}" for b in res["beats"][:12])
    print(f"  前 12 拍(s): {head}")
    if res.get("backendFallback"):
        print(f"  ! 後端退場：{res['backendFallback']}")
    print(f"  → {out_path}")


def main():
    ap = argparse.ArgumentParser(description="音樂 beat 分析 → analysis/<id>/beats/<track>.json")
    project_paths.add_project_arg(ap)
    ap.add_argument("track", nargs="?", help="音檔路徑（省略時分析專案素材資料夾下的全部 *.mp3）")
    ap.add_argument("--all", action="store_true", help="分析專案 srcDir 下的 *.mp3")
    ap.add_argument("--backend", choices=["auto", "librosa", "ffmpeg"], default="auto")
    ap.add_argument("--bpm-min", type=float, default=BPM_MIN)
    ap.add_argument("--bpm-max", type=float, default=BPM_MAX)
    args = ap.parse_args()

    with project_paths.cli_guard():
        project = project_paths.load(args.project)

        if args.all:
            tracks = sorted(project.src.glob("*.mp3"))
        elif args.track:
            tracks = [Path(args.track)]
        else:
            tracks = sorted(project.src.glob("*.mp3"))   # 省略曲目＝全部，同 --all

    if not tracks:
        print("找不到任何 .mp3", file=sys.stderr)
        sys.exit(1)

    for t in tracks:
        res = analyze_track(t, backend=args.backend,
                            bpm_min=args.bpm_min, bpm_max=args.bpm_max)
        out = write_beats_json(res, project.beats_dir)
        _print_summary(res, out)


if __name__ == "__main__":
    main()
