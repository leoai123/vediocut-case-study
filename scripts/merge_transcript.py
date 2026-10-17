#!/usr/bin/env python3
"""合併逐檔 whisper JSON → analysis/<project>/transcript.json（統一逐字稿）。

每個來源檔的 segment 時間碼即為該檔的 sourceStart/End（秒）。
依 assetId（檔名，等同拍攝時間順序）排序。

用法：
  python3 scripts/merge_transcript.py --project p1
  未帶 --project 時，走 app/project_paths.load() 的解析順序
  （明確參數 > VEDIOCUT_PROJ > current_project 指標 > 唯一專案）。
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))   # 讓 `python3 scripts/merge_transcript.py` 也能 import app.*
import app.project_paths as project_paths  # noqa: E402  單一真相來源：路徑一律經此模組解析

SENT_END = re.compile(r"[。！？!?…]|\.$|\byeah\b$", re.IGNORECASE)


def load_segments(raw_dir: Path):
    segments = []
    for jf in sorted(raw_dir.glob("IMG_*.json")):
        asset = jf.stem
        data = json.loads(jf.read_text(encoding="utf-8"))
        lang = (data.get("result") or {}).get("language", "auto")
        for seg in data.get("transcription", []):
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            off = seg.get("offsets") or {}
            start = off.get("from", 0) / 1000.0
            end = off.get("to", 0) / 1000.0
            segments.append({
                "assetId": asset,
                "sourceStart": round(start, 3),
                "sourceEnd": round(end, 3),
                "text": text,          # 可校正欄位（僅標點/同音字/專名）
                "textRaw": text,       # 原始 ASR，永不改
                "language": lang,
                "sentenceComplete": bool(SENT_END.search(text)),
                "lowConfidence": len(text) <= 2,
            })
    return segments


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    project_paths.add_project_arg(ap)
    args = ap.parse_args()

    with project_paths.cli_guard():
        proj = project_paths.load(args.project)
        raw_dir = proj.transcripts_raw
        out = proj.transcript

    segments = load_segments(raw_dir)
    if not segments:
        print("no segments found in", raw_dir, file=sys.stderr)
        sys.exit(1)
    # 依 asset 名（拍攝順序）、再依片內起點排序
    segments.sort(key=lambda s: (s["assetId"], s["sourceStart"]))
    total_spoken = sum(s["sourceEnd"] - s["sourceStart"] for s in segments)
    doc = {
        "schemaVersion": 1,
        "unit": "seconds",
        "note": "每段 sourceStart/End 為該 assetId 來源檔內的時間碼；text 可校正，textRaw 保留原始 ASR。",
        "assetCount": len({s["assetId"] for s in segments}),
        "segmentCount": len(segments),
        "totalSpokenSeconds": round(total_spoken, 1),
        "segments": segments,
    }
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out}")
    print(f"  assets={doc['assetCount']} segments={doc['segmentCount']} "
          f"spoken={doc['totalSpokenSeconds']}s")


if __name__ == "__main__":
    main()
