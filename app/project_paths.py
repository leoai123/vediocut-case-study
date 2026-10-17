#!/usr/bin/env python3
"""vediocut 多專案路徑解析 — 單一真相來源（所有腳本/模組一律經由這裡拿路徑）。

專案 manifest：analysis/<id>/project.json（沒有 manifest 的目錄不視為專案）
解析優先序：明確參數（--project / load(project=...)）
          > 環境變數 VEDIOCUT_PROJ
          > 指標檔 analysis/current_project（內容為專案 id）
          > 若全庫只有一個專案 → 用它；否則 raise（絕不默默 fallback 到 p1）。

契約（代理實作時不得更動本模組；需要改由協調者改）：
- Project.output = output/<id>/，標準檔名見 FINAL_MP4 等常數
- Project.src 來自 manifest 的 srcDir（相對 ROOT）
- 找不到/壞掉的 manifest 一律 raise ProjectError，訊息要能指路
"""
# 讓型別標註延後求值：本模組用了 `str | None`（3.10 語法），而 macOS 內建的 /usr/bin/python3
# 是 3.9——所有腳本都 import 本模組，少了這行，沒另裝 Python 的使用者第一步開案就會崩潰。
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ANALYSIS = ROOT / "analysis"
POINTER = ANALYSIS / "current_project"

# 標準輸出檔名（output/<id>/ 之下）
FINAL_MP4 = "final.mp4"
FINAL_SRT = "final.srt"
RADIO_MP4 = "radio.mp4"
VERTICAL_MP4 = "vertical.mp4"

# 分析產物檔名（analysis/<id>/ 之下）
AUDIT_JSON = "cut_tail_audit.json"   # scripts/audit_cut_tails.py 機器可讀明細
AUDIT_MD = "cut_tail_audit.md"       # scripts/audit_cut_tails.py 人類可讀報告


class ProjectError(RuntimeError):
    pass


@contextlib.contextmanager
def cli_guard():
    """CLI 進入點包住專案解析：ProjectError 印一行乾淨訊息後以 2 退出（不噴 traceback）。

    用法（各腳本 main 開頭）：
        with project_paths.cli_guard():
            proj = project_paths.load(args.project)
            ...
    只攔 ProjectError（訊息本身已能指路）；其他例外照常拋出，bug 要看得到 traceback。
    """
    try:
        yield
    except ProjectError as e:
        print(f"[project] {e}", file=sys.stderr)
        raise SystemExit(2)


@dataclass(frozen=True)
class Project:
    id: str
    manifest: dict = field(repr=False)

    # ---- 目錄 ----
    @property
    def src(self) -> Path:
        d = self.manifest.get("srcDir")
        if not d:
            raise ProjectError(f"專案 {self.id} 的 manifest 缺 srcDir")
        p = ROOT / d
        if not p.is_dir():
            raise ProjectError(f"專案 {self.id} 的來源資料夾不存在：{p}")
        return p

    @property
    def language(self) -> str:
        """素材語言（粗粒度碼：zh / en / ...）。驅動轉錄 prompt、字幕字型與斷行、
        校正策略、B-roll 檢索語言。舊專案 manifest 無此欄位 → 預設 zh（向後相容）。"""
        return self.manifest.get("language", "zh")

    @property
    def analysis(self) -> Path:
        return ANALYSIS / self.id

    @property
    def work(self) -> Path:                 # 渲染快取（gitignore 以 /.p*-render/ 涵蓋）
        return ROOT / f".{self.id}-render"

    @property
    def output(self) -> Path:
        return ROOT / "output" / self.id

    # ---- 檔案 ----
    @property
    def edl(self) -> Path: return self.analysis / "edit_decision.json"
    @property
    def transcript(self) -> Path: return self.analysis / "transcript.json"
    @property
    def config(self) -> Path: return self.analysis / "config.json"
    @property
    def story(self) -> Path: return self.analysis / "story.json"
    @property
    def shot_index(self) -> Path: return self.analysis / "shot_index.json"
    @property
    def visual_index(self) -> Path: return self.analysis / "visual_index.json"
    @property
    def beats_dir(self) -> Path: return self.analysis / "beats"
    @property
    def audio_dir(self) -> Path: return self.analysis / "audio"
    @property
    def transcripts_raw(self) -> Path: return self.analysis / "transcripts" / "raw"

    def asset_path(self, asset_id: str) -> Path:
        return self.src / f"{asset_id}.MOV"

    def list_assets(self) -> list[str]:
        """直接列舉來源資料夾（.MOV，大小寫皆可）——不依賴任何舊產物。"""
        out = {p.stem for p in self.src.iterdir()
               if p.suffix.lower() == ".mov" and p.is_file()}
        return sorted(out)


def list_projects() -> list[str]:
    if not ANALYSIS.is_dir():
        return []
    return sorted(p.parent.name for p in ANALYSIS.glob("*/project.json"))


def load(project: str | None = None) -> Project:
    pid = project or os.environ.get("VEDIOCUT_PROJ") or _pointer() or _sole_project()
    if not pid:
        raise ProjectError(
            "無法決定專案：請用 --project <id>、設 VEDIOCUT_PROJ，"
            f"或寫入指標檔 {POINTER}。現有專案：{list_projects() or '（無）'}")
    mf = ANALYSIS / pid / "project.json"
    if not mf.exists():
        raise ProjectError(
            f"專案 {pid!r} 沒有 manifest（{mf} 不存在）。"
            f"現有專案：{list_projects() or '（無）'}；新專案請先用 scripts/new_project.sh 建立。")
    try:
        manifest = json.loads(mf.read_text(encoding="utf-8"))
    except Exception as e:
        raise ProjectError(f"專案 {pid} 的 manifest 解析失敗：{e}") from e
    if manifest.get("id") not in (None, pid):
        raise ProjectError(f"manifest id 不一致：目錄 {pid} vs manifest {manifest.get('id')!r}")
    return Project(id=pid, manifest=manifest)


def _pointer() -> str | None:
    try:
        v = POINTER.read_text(encoding="utf-8").strip()
        return v or None
    except OSError:
        return None


def _sole_project() -> str | None:
    ps = list_projects()
    return ps[0] if len(ps) == 1 else None


def add_project_arg(parser: argparse.ArgumentParser) -> None:
    """各腳本共用：加 --project 參數（預設 None → load() 走解析順序）。"""
    parser.add_argument("--project", default=None,
                        help="專案 id（預設：VEDIOCUT_PROJ > analysis/current_project > 唯一專案）")


if __name__ == "__main__":
    with cli_guard():
        if len(sys.argv) > 1 and sys.argv[1] == "list":
            for pid in list_projects():
                print(pid)
        else:
            p = load(sys.argv[1] if len(sys.argv) > 1 else None)
            # 【穩定契約】以下 key=value stdout 是 zsh 腳本的解析介面：
            #   scripts/prepare_p1_audio.sh      讀 src / analysis
            #   scripts/transcribe_p1_perfile.sh 讀 analysis / language
            # 改 key 名稱、順序無妨，但改「key=value 逐行」格式或刪 key 前，
            # 必須先改完所有消費端（grep 'project_paths.py' scripts/*.sh）。
            print(f"id={p.id}\nsrc={p.src}\nanalysis={p.analysis}\nwork={p.work}\noutput={p.output}")
            print(f"language={p.language}")
            print(f"assets={len(p.list_assets())}")
