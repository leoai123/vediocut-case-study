#!/usr/bin/env python3
"""vediocut 簡易本地剪輯介面 — 純標準庫後端（http.server，零額外相依）。

在 pipeline 產出 edit_decision.json 之後，用瀏覽器微調 A-roll / B-roll / hookCount
再重新渲染。全程只用 Python 標準庫，直接 `python3 app/edit_server.py` 就能跑，
不需要 pip install，也不需要 .venv。

多專案：所有專案路徑一律經 app/project_paths.py（契約模組，唯一真相來源，本檔絕不
修改它）解析。伺服器啟動時不綁死任何專案，每個 API 請求各自解析一次：
    GET  請求：?project=<id>（省略 → project_paths.load(None) 的預設解析順序：
                明確參數 > VEDIOCUT_PROJ > analysis/current_project 指標檔 > 唯一專案）
    POST 請求：body.project（同上；解析後即從 body 移除，不會被寫回 EDL 檔）
    project 不在 project_paths.list_projects() 白名單內 → 一律 400（防路徑注入）

啟動：
    python3 app/edit_server.py            # 預設 http://127.0.0.1:8766
    python3 app/edit_server.py --port 9000
    PORT=9000 python3 app/edit_server.py

提供的 API：
    GET  /                    → 前端單頁 (app/editor.html)
    GET  /api/projects        → 專案清單（id/notes/createdAt/各成片是否存在）+ 預設專案 id
    GET  /api/edl             → analysis/<project>/edit_decision.json（?project=<id>）
    POST /api/edl             → 存回 edit_decision.json（先備份 edit_decision.bak.json；body.project）
    POST /api/render          → 背景執行 scripts/render_p1.py --project <id>
                                 （body: radio/no_broll/clear_cache/project）
    POST /api/render/vertical → 背景執行 app/reframe_vertical.py，輸出 9:16 直式
                                 （body: mode/start/duration/project）
    GET  /api/render/status   → 查詢渲染工作狀態（含 log 尾段、指定專案成片是否存在、
                                 系統負載；?project=<id> 只影響「成片是否存在」欄位，
                                 渲染工作本身全域只有一個，欄位 project 標示它屬於哪個專案）
    POST /api/validate        → 執行 scripts/validate_edl.py（唯讀，只印結果不改檔；body.project）
    GET  /api/preview         → 串流 output/<project>/ 成片 mp4（支援 HTTP Range；
                                 ?file=final|radio|vertical&project=<id>）

本檔只讀寫下列既有資料檔，不改動任何既有程式：
    analysis/<id>/edit_decision.json      （POST /api/edl 寫入，結構原樣保留）
    analysis/<id>/edit_decision.bak.json  （每次存檔前的備份）
    .{id}-render/ 快取                     （clear_cache 時清除，可由 render_p1.py 重生）
    .{id}-render/render.lock              （渲染中鎖檔，內容為 pid，供偵測併發渲染）
"""
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import project_paths as pp   # 契約模組（app/project_paths.py）：唯一真相來源，本檔絕不修改它

# ---------- 路徑（相對本檔解析，與 cwd 無關；專案相關路徑一律經 pp.load() 取得）----------
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
# 伺服器本身只用標準庫，常以系統 python3 啟動；但渲染與直式重構需要 numpy 等套件。
# 若沿用 sys.executable，渲染會在系統 Python 下執行——切點精修因缺 numpy 靜默降級、
# 字尾被切掉，而且是照著「直接 python3 app/edit_server.py」的說明做才會觸發。
VENV_PY = ROOT / ".venv" / "bin" / "python"
HEAVY_PY = str(VENV_PY) if VENV_PY.exists() else sys.executable
RENDER_SCRIPT = ROOT / "scripts" / "render_p1.py"
VALIDATE_SCRIPT = ROOT / "scripts" / "validate_edl.py"
REFRAME_SCRIPT = HERE / "reframe_vertical.py"
EDITOR_HTML = HERE / "editor.html"
EDITOR_JS = HERE / "editor.js"

DEFAULT_PORT = int(os.environ.get("PORT", "8766"))

# ---------- S1: Host/Origin 防護（main() 依實際啟動 port 填入）----------
ALLOWED_HOSTS = set()
ALLOWED_ORIGINS = set()


def _configure_allowed(port):
    ALLOWED_HOSTS.clear()
    ALLOWED_HOSTS.update({f"127.0.0.1:{port}", f"localhost:{port}"})
    ALLOWED_ORIGINS.clear()
    ALLOWED_ORIGINS.update({f"http://127.0.0.1:{port}", f"http://localhost:{port}"})


# ---------- S0: 多專案解析（白名單 + 契約模組，防路徑注入）----------
def resolve_project(pid):
    """把請求帶來的 project id 解析成 project_paths.Project。

    先驗證 pid（若有指定）落在 list_projects() 白名單內，才交給 pp.load() 走
    完整解析順序；pid=None 時原樣沿用 pp.load(None) 的預設解析（不在此另訂規則）。
    找不到 / 不合法一律 raise pp.ProjectError，呼叫端一律轉 400。
    """
    if pid is not None and pid not in pp.list_projects():
        raise pp.ProjectError(
            f"不存在的專案 id：{pid!r}；現有專案：{pp.list_projects() or '（無）'}")
    return pp.load(pid)


def preview_files(proj):
    """/api/preview?file=<key> 白名單，防路徑穿越（只允許契約模組定義的三個既知輸出檔）。"""
    return {
        "final": proj.output / pp.FINAL_MP4,
        "radio": proj.output / pp.RADIO_MP4,
        "vertical": proj.output / pp.VERTICAL_MP4,
    }


def bak_path(proj):
    return proj.analysis / "edit_decision.bak.json"


def lock_path(proj):
    return proj.work / "render.lock"


# ---------- S2: EDL 輸入消毒 ----------
ASSET_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _is_finite_nonneg(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0


def _is_plain_filename(name):
    """純檔名：字串、非空、不含路徑分隔符、不含 .. 片段（防路徑穿越）。"""
    if not isinstance(name, str) or not name:
        return False
    if "/" in name or "\\" in name or ".." in name:
        return False
    return True


def _validate_edl_payload(incoming):
    """消毒使用者可控欄位；違規時 raise ValueError 說明哪一項（供 do_POST 轉 400）。"""
    aroll = incoming.get("aroll")
    if isinstance(aroll, list):
        for i, item in enumerate(aroll):
            if not isinstance(item, dict):
                raise ValueError(f"aroll[{i}] 必須是物件")
            aid = item.get("assetId")
            if aid is not None and not ASSET_ID_RE.match(str(aid)):
                raise ValueError(f"aroll[{i}].assetId 不合法（僅允許英數/底線/連字號）: {aid!r}")
            for field in ("sourceIn", "sourceOut"):
                if field in item and not _is_finite_nonneg(item[field]):
                    raise ValueError(f"aroll[{i}].{field} 必須是 ≥0 的有限數值: {item[field]!r}")
    broll = incoming.get("broll")
    if isinstance(broll, list):
        for i, item in enumerate(broll):
            if not isinstance(item, dict):
                raise ValueError(f"broll[{i}] 必須是物件")
            aid = item.get("assetId")
            if aid is not None and not ASSET_ID_RE.match(str(aid)):
                raise ValueError(f"broll[{i}].assetId 不合法（僅允許英數/底線/連字號）: {aid!r}")
            for field in ("sourceIn", "duration", "startInClip"):
                if field in item and not _is_finite_nonneg(item[field]):
                    raise ValueError(f"broll[{i}].{field} 必須是 ≥0 的有限數值: {item[field]!r}")
    target = incoming.get("target")
    if isinstance(target, dict):
        ds = target.get("durationSec")
        if ds is not None and not _is_finite_nonneg(ds):
            raise ValueError(f"target.durationSec 必須是 ≥0 的有限數值或省略（留空＝不限）: {ds!r}")
        tol = target.get("tolerance")
        if tol is not None:
            if (not isinstance(tol, list) or len(tol) != 2
                    or not all(_is_finite_nonneg(x) for x in tol) or tol[0] > tol[1]):
                raise ValueError(f"target.tolerance 必須是 [lo, hi] 且 0≤lo≤hi: {tol!r}")
    music = incoming.get("music")
    if isinstance(music, dict):
        mf = music.get("file")
        if mf is not None and not _is_plain_filename(mf):
            raise ValueError(f"music.file 必須是純檔名（不含路徑）: {mf!r}")
        cues = music.get("cues")
        if isinstance(cues, list):
            for i, cue in enumerate(cues):
                if not isinstance(cue, dict):
                    raise ValueError(f"music.cues[{i}] 必須是物件")
                cf = cue.get("file")
                if cf is not None and not _is_plain_filename(cf):
                    raise ValueError(f"music.cues[{i}].file 必須是純檔名（不含路徑）: {cf!r}")


# ---------- T3: 渲染鎖檔（跨行程偵測併發渲染；每個專案各自一支鎖檔）----------
def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 行程存在，只是不同使用者無法送訊號 → 視為存活
    except OSError:
        return False
    return True


def _acquire_lock_or_reason(proj):
    """嘗試取得 proj.work/render.lock。成功回 None；失敗回原因字串（供 409 訊息）。

    stale lock（pid 不存在）自動清除後重取。呼叫端須已持有 _render_lock。
    """
    lp = lock_path(proj)
    if lp.exists():
        try:
            pid = int(lp.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            pid = None
        if pid and _pid_alive(pid):
            return "另一渲染進行中（可能是 CLI）"
        try:
            lp.unlink()
        except OSError:
            pass
    proj.work.mkdir(parents=True, exist_ok=True)
    lp.write_text(str(os.getpid()), encoding="utf-8")
    return None


def _release_lock(lp):
    try:
        if lp.exists():
            lp.unlink()
    except OSError:
        pass


# ---------- 渲染工作狀態（單一背景工作，涵蓋完整渲染與 9:16 直式；跨專案共用同一個
# 佇列位——同一時間只允許一個渲染工作在跑，但鎖檔本身仍各專案各一支，以便 CLI 端
# 也能偵測到某專案正在被本介面渲染）----------
RENDER = {
    "status": "idle",     # idle | running | done | failed
    "kind": None,          # "full" | "vertical"
    "project": None,       # 這次渲染工作對應的專案 id
    "args": None,          # 這次渲染帶的旗標
    "argv": None,          # 實際傳給子行程的完整 argv（供除錯/驗證用）
    "started": None,
    "ended": None,
    "returncode": None,
    "log": "",            # stdout/stderr 尾段
    "cleared": None,      # clear_cache 時清掉的檔案數
}
_render_lock = threading.Lock()


# ---------- 資料存取 ----------
def read_edl(proj):
    if not proj.edl.exists():
        return None
    return json.loads(proj.edl.read_text(encoding="utf-8"))


def write_edl(proj, incoming):
    """先備份，再存回。以既有檔為底、incoming 覆蓋頂層鍵，避免前端漏送鍵導致結構掉失。"""
    existing = read_edl(proj) or {}
    if not isinstance(incoming, dict):
        raise ValueError("EDL 必須是 JSON 物件")
    if "aroll" in incoming and not isinstance(incoming["aroll"], list):
        raise ValueError("aroll 必須是陣列")
    if "broll" in incoming and not isinstance(incoming["broll"], list):
        raise ValueError("broll 必須是陣列")
    _validate_edl_payload(incoming)
    # 存檔前備份目前版本
    if proj.edl.exists():
        shutil.copy2(proj.edl, bak_path(proj))
    merged = {**existing, **incoming}          # incoming 覆蓋，未提供的頂層鍵沿用既有值
    proj.analysis.mkdir(parents=True, exist_ok=True)
    proj.edl.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return merged


def clear_render_cache(proj):
    """清除 .{id}-render 的切片快取，讓 A-roll 微調/排序在重渲染時生效。

    render_p1.py 以 segments/{i:03d}.mp4 為索引快取切片，存在就沿用；不清就吃不到 trim。
    只刪可由 pipeline 重生的中繼檔（.{id}-render 已在 .gitignore）。
    """
    removed = 0
    for sub in ("segments", "broll"):
        d = proj.work / sub
        if d.exists():
            for f in d.iterdir():
                if f.is_file():
                    f.unlink()
                    removed += 1
    for name in ("concat.txt", "picture.mp4"):
        p = proj.work / name
        if p.exists():
            p.unlink()
            removed += 1
    return removed


# ---------- 背景渲染（T3：狀態讀寫一律在 _render_lock 內；結束時釋放對應專案的鎖檔）----------
def _render_worker(argv, lp):
    try:
        proc = subprocess.Popen(
            argv, cwd=str(ROOT),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        buf = []
        for line in proc.stdout:
            buf.append(line)
            with _render_lock:
                RENDER["log"] = "".join(buf)[-8000:]
        proc.wait()
        with _render_lock:
            RENDER["returncode"] = proc.returncode
            RENDER["status"] = "done" if proc.returncode == 0 else "failed"
    except Exception as e:  # noqa: BLE001
        with _render_lock:
            RENDER["status"] = "failed"
            RENDER["log"] = (RENDER["log"] + f"\n[server] {e}")[-8000:]
    finally:
        with _render_lock:
            RENDER["ended"] = time.time()
        _release_lock(lp)


def start_render(proj, radio=False, no_broll=False, clear_cache=False):
    with _render_lock:
        if RENDER["status"] == "running":
            return False, "已有渲染工作進行中"
        lock_reason = _acquire_lock_or_reason(proj)
        if lock_reason:
            return False, lock_reason
        # 渲染子行程一律以 --project 明確傳遞專案 id，不再靠 VEDIOCUT_PROJ 環境變數。
        argv = [HEAVY_PY, str(RENDER_SCRIPT), "--project", proj.id]
        if radio:
            argv.append("--radio")
        if no_broll:
            argv.append("--no-broll")
        cleared = clear_render_cache(proj) if clear_cache else None
        RENDER.update({
            "status": "running",
            "kind": "full",
            "project": proj.id,
            "args": {"radio": radio, "no_broll": no_broll, "clear_cache": clear_cache},
            "argv": argv,
            "started": time.time(),
            "ended": None,
            "returncode": None,
            "log": "",
            "cleared": cleared,
        })
    threading.Thread(target=_render_worker, args=(argv, lock_path(proj)), daemon=True).start()
    return True, "已開始渲染"


def start_vertical_render(proj, mode="crop", start=None, duration=None):
    """背景執行 app/reframe_vertical.py，把現有 16:9 成片轉成 9:16 直式。共用同一 job 狀態機。"""
    if mode not in ("crop", "blur"):
        return False, f"不支援的 mode: {mode!r}（僅 crop/blur）"
    final_mp4 = proj.output / pp.FINAL_MP4
    vertical_mp4 = proj.output / pp.VERTICAL_MP4
    if not final_mp4.exists():
        return False, f"找不到輸入成片 {final_mp4.name}，請先完成 16:9 渲染"
    with _render_lock:
        if RENDER["status"] == "running":
            return False, "已有渲染工作進行中"
        lock_reason = _acquire_lock_or_reason(proj)
        if lock_reason:
            return False, lock_reason
        argv = [HEAVY_PY, str(REFRAME_SCRIPT), str(final_mp4), str(vertical_mp4),
                "--mode", mode]
        if start is not None:
            argv += ["--start", str(start)]
        if duration is not None:
            argv += ["--duration", str(duration)]
        RENDER.update({
            "status": "running",
            "kind": "vertical",
            "project": proj.id,
            "args": {"mode": mode, "start": start, "duration": duration},
            "argv": argv,
            "started": time.time(),
            "ended": None,
            "returncode": None,
            "log": "",
            "cleared": None,
        })
    threading.Thread(target=_render_worker, args=(argv, lock_path(proj)), daemon=True).start()
    return True, "已開始 9:16 直式渲染"


def render_status(proj):
    """回傳全域渲染工作狀態 + 指定專案（qs.project，可能與正在渲染的專案不同）的成片存在狀態。"""
    with _render_lock:
        s = dict(RENDER)
    pf = preview_files(proj)
    s["queried_project"] = proj.id
    s["preview_exists"] = pf["final"].exists()
    s["preview_mtime"] = pf["final"].stat().st_mtime if pf["final"].exists() else 0
    s["radio_exists"] = pf["radio"].exists()
    s["radio_mtime"] = pf["radio"].stat().st_mtime if pf["radio"].exists() else 0
    s["vertical_exists"] = pf["vertical"].exists()
    s["vertical_mtime"] = pf["vertical"].stat().st_mtime if pf["vertical"].exists() else 0
    try:
        s["loadavg"] = list(os.getloadavg())
    except (OSError, AttributeError):
        s["loadavg"] = None
    return s


def run_validate(proj):
    """POST /api/validate：唯讀跑 scripts/validate_edl.py，不改動任何檔案。

    scripts/validate_edl.py 尚未整合 project_paths 契約模組（不在本檔所有權範圍內，
    不予修改），暫以它既有理解的 VEDIOCUT_PROJ / VEDIOCUT_SRC 環境變數相容傳遞專案。
    """
    env = dict(os.environ)
    env["VEDIOCUT_PROJ"] = proj.id
    env["VEDIOCUT_SRC"] = proj.manifest.get("srcDir", "")
    try:
        proc = subprocess.run(
            [sys.executable, str(VALIDATE_SCRIPT)],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60, env=env,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except subprocess.TimeoutExpired as e:
        return {
            "ok": False,
            "returncode": None,
            "stdout": (e.stdout or ""),
            "stderr": "驗證逾時（60s）",
        }


def projects_listing():
    """GET /api/projects：專案清單 + 各專案成片是否存在 + 目前預設專案 id。"""
    try:
        default_id = pp.load(None).id
    except pp.ProjectError:
        default_id = None
    out = []
    for pid in pp.list_projects():
        try:
            proj = pp.load(pid)
        except pp.ProjectError:
            continue   # manifest 壞掉的專案目錄：略過，不讓整個清單掛掉
        pf = preview_files(proj)
        out.append({
            "id": proj.id,
            "notes": proj.manifest.get("notes", ""),
            "createdAt": proj.manifest.get("createdAt", ""),
            "final": pf["final"].exists(),
            "radio": pf["radio"].exists(),
            "vertical": pf["vertical"].exists(),
        })
    return {"projects": out, "default": default_id}


# ---------- HTTP handler ----------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "vediocut-editor/1.0"

    def log_message(self, fmt, *args):  # 精簡日誌
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    # --- 回應小工具（一律帶 Content-Length，配合 HTTP/1.1 keep-alive）---
    def _send(self, status, body=b"", ctype="application/json; charset=utf-8", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(n) if n else b""

    # --- S1: Host/Origin 防護（防惡意網頁對 localhost 發 CSRF）---
    def _guard(self):
        host = self.headers.get("Host", "")
        if host not in ALLOWED_HOSTS:
            self._json({"error": f"forbidden host: {host!r}"}, 403)
            return False
        if self.command == "POST":
            origin = self.headers.get("Origin")
            if origin and origin not in ALLOWED_ORIGINS:
                self._json({"error": f"forbidden origin: {origin!r}"}, 403)
                return False
        return True

    # --- GET ---
    def do_GET(self):
        if not self._guard():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        try:
            if path == "/" or path == "/index.html":
                return self._serve_editor()
            if path == "/editor.js":
                return self._serve_static(EDITOR_JS, "application/javascript; charset=utf-8")
            if path == "/api/projects":
                return self._json(projects_listing())
            if path == "/api/edl":
                try:
                    proj = resolve_project((qs.get("project") or [None])[0])
                except pp.ProjectError as e:
                    return self._json({"error": str(e)}, 400)
                edl = read_edl(proj)
                if edl is None:
                    return self._json({"error": f"找不到 {proj.edl}"}, 404)
                return self._json(edl)
            if path == "/api/render/status":
                try:
                    proj = resolve_project((qs.get("project") or [None])[0])
                except pp.ProjectError as e:
                    return self._json({"error": str(e)}, 400)
                return self._json(render_status(proj))
            if path == "/api/preview":
                return self._serve_preview(qs)
            if path == "/favicon.ico":
                return self._send(204)
            return self._json({"error": "not found", "path": path}, 404)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": str(e)}, 500)

    def do_HEAD(self):
        self.do_GET()

    # --- POST ---
    def do_POST(self):
        if not self._guard():
            return
        path = urlparse(self.path).path
        try:
            if path == "/api/edl":
                body = self._read_body()
                try:
                    data = json.loads(body.decode("utf-8"))
                except Exception as e:  # noqa: BLE001
                    return self._json({"error": f"JSON 解析失敗: {e}"}, 400)
                if not isinstance(data, dict):
                    return self._json({"error": "EDL 必須是 JSON 物件"}, 400)
                pid = data.pop("project", None)   # project 只用來選檔，絕不寫回 EDL 本體
                try:
                    proj = resolve_project(pid)
                except pp.ProjectError as e:
                    return self._json({"error": str(e)}, 400)
                merged = write_edl(proj, data)
                return self._json({
                    "ok": True,
                    "project": proj.id,
                    "backup": str(bak_path(proj)),
                    "aroll": len(merged.get("aroll", [])),
                    "broll": len(merged.get("broll", [])),
                    "hookCount": merged.get("hookCount", 0),
                })
            if path == "/api/render":
                body = self._read_body()
                opts = {}
                if body:
                    try:
                        opts = json.loads(body.decode("utf-8"))
                    except Exception as e:  # noqa: BLE001
                        return self._json({"error": f"JSON 解析失敗: {e}"}, 400)
                pid = opts.get("project") if isinstance(opts, dict) else None
                try:
                    proj = resolve_project(pid)
                except pp.ProjectError as e:
                    return self._json({"error": str(e)}, 400)
                ok, msg = start_render(
                    proj,
                    radio=bool(opts.get("radio")),
                    no_broll=bool(opts.get("no_broll")),
                    clear_cache=bool(opts.get("clear_cache")),
                )
                status = render_status(proj)
                status["message"] = msg
                return self._json(status, 200 if ok else 409)
            if path == "/api/render/vertical":
                body = self._read_body()
                opts = {}
                if body:
                    try:
                        opts = json.loads(body.decode("utf-8"))
                    except Exception as e:  # noqa: BLE001
                        return self._json({"error": f"JSON 解析失敗: {e}"}, 400)
                pid = opts.get("project") if isinstance(opts, dict) else None
                try:
                    proj = resolve_project(pid)
                except pp.ProjectError as e:
                    return self._json({"error": str(e)}, 400)
                mode = opts.get("mode") or "crop"
                start = opts.get("start")
                duration = opts.get("duration")
                ok, msg = start_vertical_render(
                    proj, mode=mode,
                    start=(float(start) if start is not None else None),
                    duration=(float(duration) if duration is not None else None),
                )
                status = render_status(proj)
                status["message"] = msg
                return self._json(status, 200 if ok else 409)
            if path == "/api/validate":
                body = self._read_body()
                opts = {}
                if body:
                    try:
                        opts = json.loads(body.decode("utf-8"))
                    except Exception as e:  # noqa: BLE001
                        return self._json({"error": f"JSON 解析失敗: {e}"}, 400)
                pid = opts.get("project") if isinstance(opts, dict) else None
                try:
                    proj = resolve_project(pid)
                except pp.ProjectError as e:
                    return self._json({"error": str(e)}, 400)
                return self._json(run_validate(proj))
            return self._json({"error": "not found", "path": path}, 404)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": str(e)}, 500)

    # --- 前端 ---
    def _serve_editor(self):
        if not EDITOR_HTML.exists():
            return self._send(
                500, "editor.html 不存在".encode("utf-8"),
                "text/html; charset=utf-8",
            )
        body = EDITOR_HTML.read_text(encoding="utf-8").encode("utf-8")
        self._send(200, body, "text/html; charset=utf-8")

    def _serve_static(self, fpath, ctype):
        if not fpath.exists():
            return self._send(404, f"{fpath.name} 不存在".encode("utf-8"),
                              "text/plain; charset=utf-8")
        self._send(200, fpath.read_bytes(), ctype)

    # --- 成片串流（支援 Range，供 <video> seek）---
    # file 參數限定白名單鍵名（"final"｜"radio"｜"vertical"），不接受任意路徑，防路徑穿越；
    # project 參數一律經 resolve_project() 驗證存在於 list_projects()，防路徑注入。
    def _serve_preview(self, qs):
        pid = (qs.get("project") or [None])[0]
        try:
            proj = resolve_project(pid)
        except pp.ProjectError as e:
            return self._json({"error": str(e)}, 400)
        file_key = (qs.get("file", ["final"])[0] or "final")
        target = preview_files(proj).get(file_key)
        if target is None:
            return self._json({"error": f"不支援的 file 參數: {file_key!r}"}, 400)
        if not target.exists():
            return self._json({"error": "尚未渲染，找不到檔案"}, 404)
        size = target.stat().st_size
        rng = self.headers.get("Range")
        if not rng:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return self._stream(target, 0, size - 1)
        # 解析 "bytes=start-end"
        try:
            _, spec = rng.split("=", 1)
            s_txt, e_txt = spec.split("-", 1)
            start = int(s_txt) if s_txt else 0
            end = int(e_txt) if e_txt else size - 1
        except Exception:  # noqa: BLE001
            start, end = 0, size - 1
        if start >= size:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        end = min(end, size - 1)
        length = end - start + 1
        self.send_response(206)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self._stream(target, start, end)

    def _stream(self, target, start, end):
        if self.command == "HEAD":
            return
        remaining = end - start + 1
        with open(target, "rb") as f:
            f.seek(start)
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(chunk)


def main():
    port = DEFAULT_PORT
    argv = sys.argv[1:]
    if "--port" in argv:
        i = argv.index("--port")
        if i + 1 < len(argv):
            port = int(argv[i + 1])
    _configure_allowed(port)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"vediocut 剪輯介面  →  http://127.0.0.1:{port}")
    try:
        default_proj = pp.load(None)
        print(f"  預設專案： {default_proj.id}（{default_proj.manifest.get('notes', '')}）")
    except pp.ProjectError as e:
        print(f"  ⚠ 尚無法解析預設專案：{e}")
    print(f"  可用專案： {', '.join(pp.list_projects()) or '（無）'}")
    print("  Ctrl-C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n關閉中…")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
