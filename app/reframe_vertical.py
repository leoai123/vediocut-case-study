#!/usr/bin/env python3
"""9:16 直式輸出（自足模組）：把 16:9 成片 reframe 成直式短影音。

這是一個「後製 reframe」模組，不改動 render_p1.py / broll_engine.py / edit_decision.json，
輸入現有 16:9 成片（如 output/<id>/final.mp4，1280x720，字幕已燒錄），
用 ffmpeg 輸出 9:16 直式片（預設 720x1280）。

兩種取景：
  crop（預設）  置中裁切填滿（fill），可用 --offset 做「中心偏移」，或 --subject auto 做
                「簡易主體感知」自動偏移（需 opencv；無則自動退回置中並提示，作法見設計筆記）。
  blur          整張 16:9 縮到滿版寬度、置中疊在放大模糊的自身之上（不裁掉任何內容，
                適合畫面構圖比填滿更重要時）。

字幕：
  預設沿用來源已燒錄的字幕（會隨裁切一起放大 ~1.78x）。若要「重新燒、放大、置底」給直式，
  傳 --srt <乾淨無字幕母帶的 SRT>；注意此選項是給「無燒字幕的母帶」用的，對已內建字幕的
  成片會出現雙重字幕（設計筆記有整合建議 B：在 pipeline 層直接出直式、避免此問題）。

慣例對齊 scripts/render_p1.py：libx264/veryfast/yuv420p、libass force_style（FontName=PingFang TC）、
SRT 路徑對 ffmpeg filter 做冒號/逗號跳脫。

用法：
  python3 app/reframe_vertical.py <輸入mp4> <輸出mp4>
  python3 app/reframe_vertical.py in.mp4 out.mp4 --mode crop --offset 0.15
  python3 app/reframe_vertical.py in.mp4 out.mp4 --mode blur
  python3 app/reframe_vertical.py in.mp4 out.mp4 --subject auto            # 需 opencv，否則置中
  python3 app/reframe_vertical.py in.mp4 out.mp4 --srt clean_master.srt    # 重燒直式字幕
  python3 app/reframe_vertical.py in.mp4 out.mp4 --start 0 --duration 8    # 只做前 8 秒小樣本
  python3 app/reframe_vertical.py in.mp4 --project p1   # 省略輸出路徑 → 預設 output/p1/vertical.mp4
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import project_paths                      # python3 app/X.py 直接執行（app/ 在 sys.path）
except ModuleNotFoundError:                   # 被以 app.X 匯入時（如 render_p1 匯入 music_beats）
    from app import project_paths

ROOT = Path(__file__).resolve().parents[1]

# --- 對齊 render_p1.py 的編碼慣例（此處自帶一份、保持模組自足，不 import render_p1）---
VENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-maxrate", "5000k", "-bufsize", "7000k"]
# libass 直式字幕樣式：字型沿用 render_p1（PingFang TC），字級/底邊留白預設放大、置底安全區。
# FontSize/MarginV 由 CLI 覆寫；描邊參數沿用 render_p1 風格（白字、半透明黑描邊）。
SUB_FONT = "PingFang TC"


def run(cmd):
    subprocess.run(cmd, check=True)


def probe_size(path):
    """回傳 (width, height)。"""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", str(path)],
        check=True, capture_output=True, text=True).stdout
    s = json.loads(out)["streams"][0]
    return int(s["width"]), int(s["height"])


def even(n):
    """h264 yuv420p 需要偶數維度。"""
    n = int(round(n))
    return n - (n % 2)


def crop_rect(iw, ih, tw, th, offset=0.0, voffset=0.0):
    """算出要從輸入裁切、之後縮放到 tw x th 的子矩形 (cw, ch, x, y)。

    offset/voffset ∈ [-1, 1]：0=置中，±1=貼齊邊緣（水平/垂直）。填滿目標長寬比、不留黑邊。
    """
    target_ar = tw / th
    input_ar = iw / ih
    if input_ar >= target_ar:
        # 輸入較寬 → 裁切寬度（16:9→9:16 屬此情形）
        ch = ih
        cw = even(ih * target_ar)
        slack = iw - cw
        x = even(max(0, min(slack, (slack / 2.0) * (1.0 + offset))))
        y = 0
    else:
        # 輸入較高 → 裁切高度
        cw = iw
        ch = even(iw / target_ar)
        slack = ih - ch
        x = 0
        y = even(max(0, min(slack, (slack / 2.0) * (1.0 + voffset))))
    return cw, ch, x, y


def escape_sub_path(srt_path):
    """SRT 路徑進 ffmpeg subtitles filter 的跳脫（與 render_p1.py 一致）。"""
    return str(srt_path).replace(":", r"\:").replace(",", r"\,")


def sub_filter(srt_path, fontsize, marginv):
    """直式字幕 libass filter：放大字級、置底安全區。樣式沿用 render_p1（白字半透黑描邊）。

    注意：SRT 無 PlayRes，libass 以預設 ~288px 腳本座標空間換算，再等比放大到輸出高度。
    因此 fontsize/marginV 的單位是「288 空間」像素：預設 marginV=48 → 約 48/288*1280≈213px
    （距底 ~17%，落在下三分之一且避開平台 UI）；fontsize=26 中文單行約可容 12~14 字。
    """
    subq = escape_sub_path(srt_path)
    style = (f"force_style='FontName={SUB_FONT},FontSize={fontsize},"
             f"PrimaryColour=&H00FFFFFF,OutlineColour=&H90000000,"
             f"BorderStyle=1,Outline=3,Shadow=0,Alignment=2,MarginV={marginv}'")
    return f"subtitles='{subq}':{style}"


def auto_offset(inp, start, dur):
    """簡易主體感知：抽幾張幀跑 OpenCV 人臉偵測，回傳水平 offset ∈ [-1,1]（置中臉部）。

    無 opencv（本專案 venv 預設就沒有）或偵測不到臉 → 回傳 0.0（置中）並提示。
    設計筆記說明日後如何換成逐幀臉部/顯著性追蹤。
    """
    try:
        import cv2  # 選用相依，未安裝則走 except
    except Exception:
        print("  [subject] 未安裝 opencv-python，退回置中裁切（offset=0）。"
              "日後接臉部追蹤見 analysis/p1/reframe_design.md。", file=sys.stderr)
        return 0.0
    iw, ih = probe_size(inp)
    span = dur if dur else 0.0
    # 抽 3 張取樣幀（避開頭尾）
    fracs = [0.2, 0.5, 0.8]
    cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    centers = []
    with tempfile.TemporaryDirectory() as td:
        for i, fr in enumerate(fracs):
            ts = start + (span * fr if span else 0.0)
            png = Path(td) / f"f{i}.png"
            try:
                run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                     "-ss", f"{ts:.3f}", "-i", str(inp), "-frames:v", "1",
                     "-vf", "scale=640:-2", str(png)])
                img = cv2.imread(str(png))
                if img is None:
                    continue
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                faces = cascade.detectMultiScale(gray, 1.1, 5)
                if len(faces):
                    x, _, w, _ = max(faces, key=lambda f: f[2] * f[3])
                    centers.append((x + w / 2.0) / img.shape[1])
            except Exception:
                continue
    if not centers:
        print("  [subject] 偵測不到人臉，退回置中裁切（offset=0）。", file=sys.stderr)
        return 0.0
    centers.sort()
    fx = centers[len(centers) // 2]                 # 取中位數，抗離群
    off = max(-1.0, min(1.0, (fx - 0.5) * 2.0))
    print(f"  [subject] 偵測到主體中心 x={fx:.2f} → offset={off:+.2f}", file=sys.stderr)
    return off


def build_and_run(inp, out, mode, tw, th, offset, srt, fontsize, marginv,
                  start, dur, crf, preset):
    iw, ih = probe_size(inp)
    trimming = (start and start > 0) or (dur is not None)

    pre = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if start and start > 0:
        pre += ["-ss", f"{start:.3f}"]          # 輸入端快速 seek
    pre += ["-i", str(inp)]
    if dur is not None:
        pre += ["-t", f"{dur:.3f}"]

    venc = ["-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-maxrate", "5000k", "-bufsize", "7000k", "-pix_fmt", "yuv420p"]
    # 有裁切/位移就重編音訊避免 copy 造成的位移；否則直接 copy（reframe 只動畫面）。
    aenc = ["-c:a", "aac", "-b:a", "192k"] if trimming else ["-c:a", "copy"]

    if mode == "blur":
        # 整張 16:9 疊在放大模糊背景上，不裁掉內容（前景滿版寬度、垂直置中）
        fc = (f"[0:v]scale={tw}:{th}:force_original_aspect_ratio=increase,"
              f"crop={tw}:{th},boxblur=20:2,setsar=1[bg];"
              f"[0:v]scale={tw}:-2[fg];"
              f"[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v0]")
        vlabel = "v0"
        if srt:
            fc += f";[{vlabel}]{sub_filter(srt, fontsize, marginv)}[vout]"
            vlabel = "vout"
        cmd = pre + ["-filter_complex", fc, "-map", f"[{vlabel}]", "-map", "0:a?",
                     *venc, *aenc, "-movflags", "+faststart", str(out)]
        geom = f"blur pad, fg={tw}x{even(tw*ih/iw)}"
    else:
        # crop：置中/偏移裁切填滿。線性 -vf 鏈（節點少、抗高負載，符合 render_p1 精神）
        cw, ch, x, y = crop_rect(iw, ih, tw, th, offset=offset)
        vf = f"crop={cw}:{ch}:{x}:{y},scale={tw}:{th},setsar=1"
        if srt:
            vf += "," + sub_filter(srt, fontsize, marginv)
        vf += ",format=yuv420p"
        cmd = pre + ["-vf", vf, "-map", "0:v:0", "-map", "0:a?",
                     *venc, *aenc, "-movflags", "+faststart", str(out)]
        geom = f"crop {cw}x{ch}+{x}+{y} (offset={offset:+.2f}) → {tw}x{th}"

    run(cmd)
    return geom


def main():
    ap = argparse.ArgumentParser(
        description="把 16:9 成片 reframe 成 9:16 直式短影音（後製 reframe，不動既有 pipeline）。")
    ap.add_argument("input", help="輸入 mp4（16:9 成片，如 output/<id>/final.mp4）")
    ap.add_argument("output", nargs="?", default=None,
                    help="輸出 mp4（9:16 直式）；省略時預設 Project.output/vertical.mp4（見 --project）")
    project_paths.add_project_arg(ap)
    ap.add_argument("--mode", choices=["crop", "blur"], default="crop",
                    help="crop=置中/偏移裁切填滿（預設）；blur=不裁切、模糊背景襯底")
    ap.add_argument("--size", default="720x1280", help="輸出尺寸 WxH（預設 720x1280）")
    ap.add_argument("--offset", type=float, default=0.0,
                    help="crop 水平偏移 -1..1（0=置中；正=右移取景）")
    ap.add_argument("--subject", choices=["none", "auto"], default="none",
                    help="auto=OpenCV 人臉自動算 offset（無 opencv 退回置中）")
    ap.add_argument("--srt", default=None,
                    help="重燒直式字幕的 SRT（給無燒字幕母帶用；對已燒字幕成片會雙字幕）")
    ap.add_argument("--sub-fontsize", type=int, default=26,
                    help="重燒字幕字級（libass ~288 空間，預設 26）")
    ap.add_argument("--sub-marginv", type=int, default=48,
                    help="重燒字幕底邊留白（libass ~288 空間，預設 48≈距底17%%）")
    ap.add_argument("--start", type=float, default=0.0, help="起始秒（小樣本用）")
    ap.add_argument("--duration", type=float, default=None, help="長度秒（小樣本用，如 8）")
    ap.add_argument("--crf", type=int, default=22)
    ap.add_argument("--preset", default="veryfast")
    args = ap.parse_args()

    inp = Path(args.input)
    if args.output:
        out = Path(args.output)
    else:
        with project_paths.cli_guard():
            project = project_paths.load(args.project)
        out = project.output / project_paths.VERTICAL_MP4
        print(f"  輸出：未指定 --output → 預設 {out}")
    if not inp.exists():
        print(f"找不到輸入：{inp}", file=sys.stderr); sys.exit(1)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        tw, th = (int(v) for v in args.size.lower().split("x"))
    except Exception:
        print(f"--size 格式錯誤：{args.size}（應為 WxH，如 720x1280）", file=sys.stderr); sys.exit(1)
    tw, th = even(tw), even(th)

    offset = args.offset
    if args.subject == "auto":
        offset = auto_offset(inp, args.start, args.duration)

    if args.srt:
        srt = Path(args.srt)
        if not srt.exists():
            print(f"找不到 --srt：{srt}", file=sys.stderr); sys.exit(1)
        print(f"  字幕：重燒直式字幕 {srt.name}（fontsize={args.sub_fontsize}, "
              f"marginV={args.sub_marginv}）。注意：對已內建字幕的成片會雙重字幕。")
    else:
        srt = None
        print("  字幕：沿用來源已燒錄字幕（隨裁切一起放大）。")

    geom = build_and_run(inp, out, args.mode, tw, th, offset, srt,
                         args.sub_fontsize, args.sub_marginv,
                         args.start, args.duration, args.crf, args.preset)

    ow, oh = probe_size(out)
    import math
    g = math.gcd(ow, oh)
    ar = f"{ow // g}:{oh // g}"
    ok = "OK" if ar == "9:16" else "!!"
    print(f"  取景：{geom}")
    print(f"[{ok}] {out}  {ow}x{oh}  ({ar})")
    if ar != "9:16":
        sys.exit(2)


if __name__ == "__main__":
    main()
