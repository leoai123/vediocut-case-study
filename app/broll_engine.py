#!/usr/bin/env python3
"""B-roll 策略引擎：transcript + aroll + shot_index + config → broll[]。

把「講到什麼配什麼」自動化並可自訂。訊號：
  - 語意配對：A-roll 台詞含 shot 的 keyword 數（關鍵詞分數）
  - 視覺檢索：CLIP 影像相似度（app/visual_index.py），找回 shot_index 沒標的畫面
  - 融合重排：final = w_kw*norm(keyword) + w_vis*norm(visual_cos)（見下方「融合」）
  - 畫質 quality、非人臉優先（B-roll 不該又是另一顆講話臉）
  - 新鮮度：重複使用扣分（allowReuse=false 時直接排除已用）
  - 策略參數（config.broll）：density/minLen/maxLen/protect/deny/threshold/fusion

融合（config.broll.fusion）
  兩個分數量綱不同：關鍵詞分數約 0~3+；CLIP 餘弦相似度約 0.20~0.35 且擠在窄帶。
  各自 min-max 正規化到 [0,1] 再加權相加：
    norm_kw  = clamp(kw  / kwCeil, 0, 1)               # 單一具體名詞 ~0.5
    norm_vis = clamp((cos-visFloor)/(visCeil-visFloor), 0, 1)
    fused    = wKeyword*norm_kw + wVisual*norm_vis
  視覺可提出 shot_index 沒標的新候選（最大價值），但排除：同源 asset、hasFace、
  已知講話 A-roll 大檔。visual_index.json 不存在／fusion.enabled=false 時自動退回
  純關鍵詞（零風險 rollout）。

鎖定
  broll[] 條目帶 "locked": true 時，重跑引擎會原樣保留該條目、不重新指派，且其
  assetId 不會被其他段落重用（人工校正過的配對受保護）。

用法：
  python3 app/broll_engine.py --project p1        # 讀 analysis/<id>/*，把 broll[] 寫回 edit_decision.json
  python3 app/broll_engine.py --project p1 --dry  # 只印結果不寫檔
  python3 app/broll_engine.py --dry --no-fusion   # 強制純關鍵詞（對照用）；--project 省略時走
                                                   # project_paths 預設解析順序（見該模組）
"""
import argparse
import importlib
import json
import os
import sys

try:
    import project_paths                      # python3 app/X.py 直接執行（app/ 在 sys.path）
except ModuleNotFoundError:                   # 被以 app.X 匯入時（如 render_p1 匯入 music_beats）
    from app import project_paths

STRATEGY_PRESETS = {
    "minimal":  {"densityPerMin": 1.5, "minLen": 2.0, "maxLen": 3.0},
    "balanced": {"densityPerMin": 3.0, "minLen": 2.0, "maxLen": 3.5},
    "rich":     {"densityPerMin": 5.0, "minLen": 1.8, "maxLen": 3.5},
}

# 融合預設（config.broll.fusion 未指定時使用）—— v1 英文 CLIP/LAION 尺規
FUSION_DEFAULTS = {
    "enabled": True,
    "wKeyword": 1.0,        # 關鍵詞權重（主：精準字面/品牌/專有名詞）
    "wVisual": 0.7,         # 視覺權重（輔：換句話說也找得到、漏標的畫面）
    "kwCeil": 2.5,          # 關鍵詞正規化上限（單一具體名詞≈0.57、雙名詞≈0.97）
    "visFloor": 0.25,       # 餘弦相似度雜訊地板（低於此視覺貢獻=0）
    "visCeil": 0.32,        # 餘弦相似度飽和上限（食物正配對典型 0.30~0.34）
    "fusedThreshold": 0.34, # 融合後保留門檻（關鍵詞候選 kw>=threshold 必過；主要用於篩視覺-only）
    "minVisCos": 0.28,      # 視覺-only 候選的硬性 cos 下限
    "topkPerQuery": 8,      # 每段台詞向視覺索引取回的候選數
    # ── 分離度門檻（寧缺勿錯；見 analysis/p2/broll_audit_report.md §四.A4）──
    # 同一查詢的 top1 與「合格候選中位數」的分離度不足、或 top1 絕對 cos 過低 →
    # 判該段「無高信心視覺候選」，不硬配（改走生成式/留白，見 generative fallback）。
    # v1 英文 CLIP 尺規：cos 擠在 0.29~0.33 雜訊帶、分離度本就極小，故 v1 預設關閉此
    # 門檻（sepEnabled=False）以維持既有行為；v2 多語索引才預設開啟（見 FUSION_V2_DEFAULTS）。
    "sepEnabled": False,
    "sepMargin": 0.04,      # top1 - median(合格候選) 的最小分離度
    "sepTop1Floor": 0.30,   # top1 絕對 cos 下限
    # faceDenyAssets 刻意不給預設值：每個專案的講話 A-roll 大檔都不同，把某一個
    # 專案手工核對過的清單當全域預設會誤套到別的專案。config.broll.fusion 未指定
    # 這個鍵時，resolve_broll_cfg() 會呼叫 asset_profile.suggest_face_deny() 依
    # 逐字稿口白量自動推定一份保守名單（見該模組與 _auto_face_deny()）。
}

# v2 多語/原生中文 CLIP（Chinese-CLIP）尺規的融合預設。與 v1 的差異：
#   - Chinese-CLIP 餘弦絕對值整體較高（p2 實測 per-asset-best 中位 ~0.40、正配對 0.41~0.47），
#     故 visFloor/visCeil/minVisCos 全數上移，否則 v1 尺規會讓所有 cos 都飽和成 1.0。
#   - 分離度門檻預設「開」：這是 v2「寧缺勿錯」的主要防線（p2 校準見 visual_v2_report.md）。
# 這些是「程式內預設」，config.broll.fusion.* 仍可逐鍵覆寫（.get 讀取，不寫 config 檔）。
FUSION_V2_DEFAULTS = dict(FUSION_DEFAULTS)
FUSION_V2_DEFAULTS.update({
    "visFloor": 0.38,       # Chinese-CLIP 尺規（p2 實測 eligible top1 0.38~0.49、中位 ~0.44）
    "visCeil": 0.49,
    "minVisCos": 0.35,      # 視覺-only 硬地板（cnclip 對任何畫面基線就 ~0.40，設低、由分離度把關）
    "fusedThreshold": 0.20,
    # ── 分離度門檻（寧缺勿錯）—— p2 全 30 段實測校準（見 visual_v2_report.md §分離度校準）──
    # 誠實結論：對「視覺同質」的美食/街景 B-roll，Chinese-CLIP 餘弦分佈壓縮（正配對
    # 分離度 0.027~0.056 與抽象無素材段 0.028~0.030 高度重疊），分離度無法「精準」辨別對錯；
    # 且正配對（車站/促銷 DM）絕對 cos 反而較低，故絕對地板不宜設高。因此門檻定位為
    # 「保守安全閥」：只擋掉分離度最平（後段 ~15%）的檢索，避免像 v1 那樣硬配純雜訊，
    # 同時不誤殺 a17/a21 這類已驗證的正配對（sep≈0.027）。被擋段落改走生成式/留白。
    "sepEnabled": True,
    "sepMargin": 0.02,      # top1 - median(eligible) 最小分離度（<0.02 視為檢索無鑑別力）
    "sepTop1Floor": 0.30,   # top1 絕對 cos 下限（僅擋近乎全黑/退化幀；非對錯鑑別器）
})


def maybe_generative_fallback(low_conf, project=None, cfg=None):
    """『無高信心視覺候選』段落的可插拔掛鉤（生成式 B-roll / 留白）。

    現階段**不真的生成**——只把清單記到 stderr，並保留未來接 app/generative_broll.py
    的插入點。這樣「寧缺勿錯」被門檻擋下的段落不會憑空消失，而是被明確標記為
    「建議生成式/留白」交回協調者/使用者決策（見 broll_audit_report §四.A6）。

    回傳原清單（每段含 overAroll / text / why），供 --dry 輸出與上層彙整。
    """
    for seg in low_conf:
        _log(f"[broll] → 候選不足，建議生成式/留白：{seg['overAroll']} "
             f"「{seg.get('text','')[:24]}」（{seg.get('why','')}）")
    # 未來啟用生成式時的插入點（示意，現不執行、不 import，避免載入生成後端）：
    #   import generative_broll
    #   for seg in low_conf:
    #       prompt = generative_broll.build_prompt(seg["text"])
    #       provider = generative_broll.pick_available_provider(["cloud_image", "stub"])
    #       img = provider.generate_image(prompt, aspect=cfg.get("target",{}).get("aspect","16:9"))
    #       ... 品質閘 + stage_asset + build_broll_entry ...
    return low_conf


def _log(*a):
    print(*a, file=sys.stderr, flush=True)


def resolve_broll_cfg(cfg, project=None, index_kind="v1"):
    b = dict(cfg.get("broll", {}))
    preset = STRATEGY_PRESETS.get(b.get("strategy", "balanced"))
    if preset and b.get("strategy") != "custom":
        for k, v in preset.items():
            b.setdefault(k, v)
            b[k] = preset[k] if k in ("densityPerMin", "minLen", "maxLen") else b.get(k, v)
    b.setdefault("densityPerMin", 3.0)
    b.setdefault("minLen", 2.0); b.setdefault("maxLen", 3.5)
    b.setdefault("protectChapters", []); b.setdefault("protectClips", [])
    b.setdefault("allowReuse", False); b.setdefault("maxPerClip", 1)
    b.setdefault("scoreThreshold", 1.0); b.setdefault("deny", [])
    # 融合設定：v1 與 v2 的餘弦尺規不同，各自有一套程式內預設。
    #   - v1 尺規調整放 config.broll.fusion（既有）。
    #   - v2 尺規調整放 config.broll.fusionV2（新鍵，避免 p2 既有 v1-尺規 fusion 值汙染 v2）。
    # 兩者皆「.get 讀取、不寫 config 檔」。
    cfg_fusion = dict(b.get("fusion", {}) or {})
    if index_kind == "v2":
        f = dict(FUSION_V2_DEFAULTS)
        f.update(b.get("fusionV2", {}) or {})
        # 結構性設定（與尺規無關）沿用 config.broll.fusion：講話臉黑名單、融合開關、取回數
        for k in ("enabled", "faceDenyAssets", "topkPerQuery"):
            if k in cfg_fusion:
                f[k] = cfg_fusion[k]
    else:
        f = dict(FUSION_DEFAULTS)
        f.update(cfg_fusion)
    if "faceDenyAssets" not in f:
        f["faceDenyAssets"] = _auto_face_deny(project)
    b["fusion"] = f
    return b


def _auto_face_deny(project):
    """config.broll.fusion 沒給 faceDenyAssets 時的保守預設：依逐字稿口白量自動推定
    重口白（=大概率是講話 A-roll）的資產（見 app/asset_profile.py）。沒有 project
    可用時（例如舊呼叫方沒傳）就沒有逐字稿可讀，退回空清單並提示。"""
    if project is None:
        _log("[broll] config 未指定 fusion.faceDenyAssets 且未提供 project → 無法自動推定，"
             "使用空清單（建議改用 --project 執行，或在 config.broll.fusion.faceDenyAssets 明列）")
        return []
    try:
        import asset_profile
        suggested = asset_profile.suggest_face_deny(project)
    except Exception as e:
        _log(f"[broll] 自動推定 faceDenyAssets 失敗（{e}）→ 使用空清單")
        return []
    _log(f"[broll] config 未指定 fusion.faceDenyAssets → 依口白量自動推定"
         f"（≥{asset_profile.DEFAULT_THRESHOLD}s，共 {len(suggested)} 支）："
         f"{'、'.join(suggested) if suggested else '（無）'}")
    return suggested


# 泛用購物/折扣詞：整間店到處都是，配到它們幾乎沒有畫面資訊量，
# 不該壓過具體名詞（魚/雞翅/披薩/壽司）。命中這些只給低分。
GENERIC_KW = {"優惠", "特價", "商品", "折扣", "打折", "便宜"}
GENERIC_WEIGHT = 0.35


def score_subshot(text, shot, sub):
    if shot.get("hasFace"):
        return 0.0, []              # B-roll 不用另一顆講話臉
    hits = [k for k in sub.get("keywords", []) if k in text]
    if not hits:
        return 0.0, []
    # 具體名詞每個 1.0、泛用詞每個 0.35；避免「講炸雞卻配到貨架特價牌」
    hit_score = sum(GENERIC_WEIGHT if k in GENERIC_KW else 1.0 for k in hits)
    concrete = [k for k in hits if k not in GENERIC_KW]
    score = hit_score + shot.get("quality", 0.5) * 0.5
    # 回傳時把具體命中詞排前面，理由文字更精準
    return score, (concrete + [k for k in hits if k in GENERIC_KW])


def clip_duration(clip):
    return clip["sourceOut"] - clip["sourceIn"]


def _clamp01(x):
    return max(0.0, min(1.0, x))


def _norm_kw(kw, kwceil):
    return _clamp01(kw / kwceil) if kwceil > 0 else 0.0


def _norm_vis(cos, floor, ceil):
    if cos is None or ceil <= floor:
        return 0.0
    return _clamp01((cos - floor) / (ceil - floor))


def _ranges_overlap(r1, r2, pad=1.0):
    """兩個 [start,end] 時窗是否重疊（含 pad 容差）。"""
    return not (r1[1] + pad < r2[0] or r2[1] + pad < r1[0])


def _fresh_key(asset, rng):
    """新鮮度去重鍵：asset + ~3s 時間桶（統一 keyword / visual 候選；
    對現有 shot_index 各 subshot 皆落在不同桶，等同原本的 subshot 級去重）。"""
    return f"{asset}#{int(round(rng[0] / 3.0))}"


def load_visual_index(fusion_cfg, project, index_path=None):
    """載入 visual_index（可選）。任何缺件/失敗都回 (None, None) → 退回純關鍵詞。

    index_path 未給時用 project.visual_index（v1）；v2 由呼叫方傳 visual_index_v2.json。
    索引自帶 kind（v1/v2），下游 idx.query 會自動用對應的文字塔（見 visual_index.py）。
    """
    if not fusion_cfg.get("enabled", True):
        _log("[broll] fusion.enabled=false → 純關鍵詞模式")
        return None, None
    # 兩種名稱都試：直接跑 app/broll_engine.py 時 sys.path[0]=app/ → "visual_index"；
    # 以 -m app.broll_engine 跑時 sys.path[0]=repo 根 → 只有 "app.visual_index" 找得到。
    # 先前只試前者，用 -m 呼叫會 import 失敗並「退回純關鍵詞」——融合被靜默關掉，
    # 輸出仍印「融合=off」看似正常，實際是環境問題不是策略決定。
    vi = None
    for name in ("visual_index", "app.visual_index"):
        try:
            vi = importlib.import_module(name)
            break
        except ImportError:
            continue
        except Exception as e:
            _log(f"[broll] import {name} 失敗（{type(e).__name__}: {e}）→ 退回純關鍵詞")
            return None, None
    if vi is None:
        _log("[broll] 找不到 visual_index 模組（試過 visual_index / app.visual_index）"
             "→ 退回純關鍵詞。fusion.enabled=true 卻走到這裡代表環境有問題，非策略決定")
        return None, None
    idx_path = index_path or project.visual_index
    if not idx_path.exists():
        _log(f"[broll] {idx_path} 不存在 → 退回純關鍵詞")
        return None, None
    try:
        idx = vi.VisualIndex.load(idx_path)
        _log(f"[broll] 視覺索引就緒：{idx.meta.get('frameCount')} 幀 / "
             f"{idx.meta.get('assetCount')} assets（{idx.meta.get('model')}，kind={idx.kind}）")
        return vi, idx
    except Exception as e:
        _log(f"[broll] 載入視覺索引失敗（{e}）→ 退回純關鍵詞")
        return None, None


def load_shot_index(project):
    """載入 shot_index.json；缺失時優雅降級為空索引（純視覺/生成式候選頂上），
    不再直接 crash（先前行為）。"""
    try:
        return json.loads(project.shot_index.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _log(f"[broll] {project.shot_index} 不存在 → 降級為空索引（0 shots，改走純視覺/生成式候選）")
        return {"shots": {}}


def _visual_hits(vi, idx, text, topk, lang="zh"):
    """回傳 (per-asset dict {asset:[(range,cos)]}, matched_cn_terms, ranked)。失敗回 ({}, [], [])。

    ranked = idx.query 原始排序 [(asset, range, cos), ...]（降序），供分離度門檻計算。
    matched：v1 才有（LEXICON 命中詞，用於 reason 文字）；v2 中文直查、無 LEXICON → []。
    lang：專案語言（見 project_paths.Project.language），只影響 v1 的文字塔前處理
    （zh 走 LEXICON 橋接、en 走 extract_keywords_en）；預設 "zh" 保留原行為。
    """
    if idx is None:
        return {}, [], []
    try:
        hits = idx.query(text, topk=topk, lang=lang)
    except Exception as e:
        _log(f"[broll] 視覺查詢失敗（{e}）→ 該段跳過視覺")
        return {}, [], []
    by_asset = {}
    for a, rng, cos in hits:
        by_asset.setdefault(a, []).append((rng, float(cos)))
    matched = []
    if getattr(idx, "kind", "v1") != "v2":
        _, matched = vi.expand_query(text)
    return by_asset, matched, hits


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    if n == 0:
        return 0.0
    return xs[n // 2] if n % 2 else 0.5 * (xs[n // 2 - 1] + xs[n // 2])


def generate(transcript, aroll, shot_index, cfg, existing_broll=None, vi=None, idx=None, project=None):
    index_kind = getattr(idx, "kind", "v1") if idx is not None else "v1"
    b = resolve_broll_cfg(cfg, project=project, index_kind=index_kind)
    f = b["fusion"]
    lang = getattr(project, "language", "zh") if project is not None else "zh"
    shots = shot_index.get("shots", {})
    deny = set(b["deny"])
    face_deny = set(f.get("faceDenyAssets", []))
    total_sec = sum(clip_duration(c) for c in aroll)
    budget = max(1, round(b["densityPerMin"] * total_sec / 60.0))
    fusion_on = idx is not None
    sep_on = fusion_on and f.get("sepEnabled", False)
    # 「無高信心視覺候選」的段落（寧缺勿錯門檻擋下 / 檢索無足夠分離度）→ 生成式/留白清單
    low_conf = []
    seg_has_cand = set()      # 有產出任何候選的段
    eligible_meta = {}        # id -> {text, sepDiag}

    # 鎖定條目：原樣保留、其 aroll 段不再指派、其 asset 不被重用
    locked = [dict(x) for x in (existing_broll or []) if x.get("locked")]
    locked_ids = {x.get("overAroll") for x in locked}
    locked_assets = {x.get("assetId") for x in locked}

    def excluded(asset, clip):
        if asset == clip["assetId"] or asset in deny or asset in locked_assets:
            return True                              # 同源 / 停用 / 鎖定素材不重用
        sh = shots.get(asset)
        if sh and sh.get("hasFace"):
            return True                              # shot_index 標的講話臉
        if asset in face_deny:
            return True                              # 保守：不在 shot_index 的講話 A-roll
        return False

    # 為每個「可放 B-roll」的 A-roll 段收集所有過門檻候選（關鍵詞 + 視覺）
    candidates = []
    for c in aroll:
        if c.get("id") in locked_ids:
            continue                                 # 鎖定段：跳過重新指派
        if c.get("chapter") in b["protectChapters"] or c.get("id") in b["protectClips"]:
            continue
        if clip_duration(c) < b["minLen"] + 1.5:     # 太短不放，保留完整臉
            continue
        text = c.get("text", "")
        vis_by_asset, matched, ranked = (
            _visual_hits(vi, idx, text, f["topkPerQuery"], lang=lang) if fusion_on else ({}, [], []))
        eligible_meta[c["id"]] = {"text": text, "sepDiag": None}

        # ── 分離度門檻（寧缺勿錯）：只看「合格候選」（排除同源/停用/講話臉/鎖定素材）──
        # top1 與合格候選中位數的分離度不足、或 top1 絕對 cos 過低 → 該段判「無高信心
        # 視覺候選」，不加視覺-only 候選（避免像 v1 那樣把雜訊硬配上去）。
        sep_pass = True
        if sep_on:
            elig = [(a, vc) for (a, vr, vc) in ranked if not excluded(a, c)]
            if elig:
                top1 = elig[0][1]
                med = _median([vc for _, vc in elig])
                sep = top1 - med
                eligible_meta[c["id"]]["sepDiag"] = {
                    "top1": round(top1, 4), "median": round(med, 4),
                    "sep": round(sep, 4), "top1Asset": elig[0][0]}
                if top1 < f["sepTop1Floor"] or sep < f["sepMargin"]:
                    sep_pass = False
            else:
                sep_pass = False

        kw_ranges = {}   # asset -> list of keyword 候選 range（用來去重視覺-only）

        # (1) 關鍵詞候選（來自 shot_index），視覺分數當加成
        for asset, shot in shots.items():
            if excluded(asset, c):
                continue
            for si, sub in enumerate(shot.get("subshots", [])):
                kw_sc, hits = score_subshot(text, shot, sub)
                if kw_sc < b["scoreThreshold"]:      # 保留原關鍵詞門檻（精準度不退）
                    continue
                # 找同 asset 且時窗重疊的視覺命中，取最高 cos 當視覺確認
                vcos = None
                for (vr, vc) in vis_by_asset.get(asset, []):
                    if _ranges_overlap(vr, sub["range"], pad=1.0):
                        vcos = vc if vcos is None else max(vcos, vc)
                nk = _norm_kw(kw_sc, f["kwCeil"])
                nv = _norm_vis(vcos, f["visFloor"], f["visCeil"])
                fused = f["wKeyword"] * nk + (f["wVisual"] * nv if fusion_on else 0.0)
                kw_ranges.setdefault(asset, []).append(sub["range"])
                seg_has_cand.add(c["id"])
                candidates.append({
                    "source": "kw+vis" if vcos else "kw",
                    "overAroll": c["id"], "assetId": asset,
                    "freshKey": _fresh_key(asset, sub["range"]),
                    "range": sub["range"], "fused": round(fused, 4),
                    "kw": round(kw_sc, 2), "cos": vcos,
                    "hits": hits, "clip": c,
                })

        # (2) 視覺-only 候選（shot_index 沒標的畫面；最大價值）。分離度門檻擋下者不加。
        if fusion_on and sep_pass:
            concrete_matched = [m for m in matched if m not in GENERIC_KW]
            for asset, lst in vis_by_asset.items():
                if excluded(asset, c):
                    continue
                for (vr, vc) in lst:
                    if vc < f["minVisCos"]:
                        continue
                    # 已被同 asset 的關鍵詞候選涵蓋（時窗重疊）→ 由該候選帶視覺加成，不重覆加
                    if any(_ranges_overlap(vr, kr, pad=1.0) for kr in kw_ranges.get(asset, [])):
                        continue
                    nv = _norm_vis(vc, f["visFloor"], f["visCeil"])
                    fused = f["wVisual"] * nv
                    if fused < f["fusedThreshold"]:
                        continue
                    seg_has_cand.add(c["id"])
                    candidates.append({
                        "source": "vis",
                        "overAroll": c["id"], "assetId": asset,
                        "freshKey": _fresh_key(asset, vr),
                        "range": vr, "fused": round(fused, 4),
                        "kw": 0.0, "cos": vc,
                        "hits": concrete_matched or matched, "clip": c,
                    })

    # 融合門檻（關鍵詞候選 kw>=scoreThreshold → 必過；視覺-only 已在上方過濾）
    candidates = [c for c in candidates if c["fused"] >= f["fusedThreshold"] or c["source"] != "vis"]

    # 依融合分數排序，套用預算與新鮮度
    candidates.sort(key=lambda x: -x["fused"])
    broll, used_subs, per_clip = [], set(), {}
    for lk in locked:                                 # 鎖定條目先占 budget/per_clip/freshness
        per_clip[lk.get("overAroll")] = per_clip.get(lk.get("overAroll"), 0) + 1
    for cand in candidates:
        if len(broll) + len(locked) >= budget:
            break
        if not b["allowReuse"] and cand["freshKey"] in used_subs:
            continue
        cid = cand["overAroll"]
        if per_clip.get(cid, 0) >= b["maxPerClip"]:
            continue
        cdur = clip_duration(cand["clip"])
        rng = cand["range"]
        length = max(b["minLen"], min(b["maxLen"], round(cdur * 0.28, 1)))
        length = min(length, round(rng[1] - rng[0], 1))     # 不超過命中時窗
        start = round(min(1.3, cdur * 0.12), 1)
        if start + length > cdur - 0.3:
            length = round(cdur - 0.3 - start, 1)
        if length < b["minLen"]:
            continue
        nv = _norm_vis(cand["cos"], f["visFloor"], f["visCeil"])
        concrete = [h for h in cand["hits"] if h not in GENERIC_KW]
        if cand["source"] == "vis":
            conf = min(0.9, round(0.5 + 0.4 * nv, 2))
            hit_words = [h for h in cand["hits"][:3] if h]
            if hit_words:
                reason = (f"視覺檢索：畫面近似「{'、'.join(hit_words)}」(cos {cand['cos']:.2f}) "
                          f"→ 補 {cand['assetId']}（shot_index 未標）")
            else:
                reason = (f"視覺檢索：畫面近似（CLIP cos {cand['cos']:.2f}）"
                          f"→ 補 {cand['assetId']}（shot_index 未標）")
        else:
            conf = min(0.95, round(0.5 + 0.15 * len(concrete) + 0.2 * nv, 2))
            vtag = f"＋視覺確認(cos {cand['cos']:.2f})" if cand["cos"] else ""
            reason = f"台詞提到「{'、'.join(cand['hits'])}」{vtag} → 補 {cand['assetId']} 畫面"
        broll.append({
            "overAroll": cid, "startInClip": start, "duration": length,
            "assetId": cand["assetId"], "sourceIn": rng[0],
            "confidence": conf,
            "reason": reason,
        })
        used_subs.add(cand["freshKey"])
        per_clip[cid] = per_clip.get(cid, 0) + 1

    broll = locked + broll
    broll.sort(key=lambda x: x["overAroll"])
    n_vis = sum(1 for x in broll if "視覺檢索" in x.get("reason", ""))

    # ── 無高信心候選（寧缺勿錯）：可放 B-roll 但一個候選都沒過門檻的段 → 生成式/留白清單 ──
    picked_ids = {x["overAroll"] for x in broll}
    for cid, m in eligible_meta.items():
        if cid in seg_has_cand or cid in picked_ids:
            continue
        d = m.get("sepDiag")
        why = "檢索無合格候選"
        if d is not None:
            why = f"分離度不足 top1={d['top1']} 中位={d['median']} sep={d['sep']}（top1={d['top1Asset']}）"
        low_conf.append({"overAroll": cid, "text": m.get("text", ""), "why": why})
    low_conf.sort(key=lambda x: x["overAroll"])
    if low_conf:
        maybe_generative_fallback(low_conf, project=project, cfg=cfg)

    return broll, {"budget": budget, "totalSec": round(total_sec, 1),
                   "strategy": b.get("strategy"), "picked": len(broll),
                   "locked": len(locked), "visualOnly": n_vis,
                   "fusion": "on" if fusion_on else "off",
                   "indexKind": index_kind, "sepGate": "on" if sep_on else "off",
                   "candidates": len(candidates), "lowConf": low_conf}


def main():
    ap = argparse.ArgumentParser()
    project_paths.add_project_arg(ap)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--no-fusion", action="store_true", help="強制純關鍵詞（對照/退回）")
    ap.add_argument("--v2", action="store_true",
                    help="用多語/原生中文視覺索引 visual_index_v2.json（治本；預設仍用 v1 visual_index.json）"
                         "。亦可設環境變數 VEDIOCUT_VISUAL_V2=1。")
    args = ap.parse_args()

    with project_paths.cli_guard():
        project = project_paths.load(args.project)

    transcript = json.loads(project.transcript.read_text(encoding="utf-8"))
    edl = json.loads(project.edl.read_text(encoding="utf-8"))
    shot_index = load_shot_index(project)
    cfg = json.loads(project.config.read_text(encoding="utf-8"))

    fusion_enabled = (not args.no_fusion) and dict(
        cfg.get("broll", {}).get("fusion", {}) or {}).get("enabled", FUSION_DEFAULTS["enabled"])
    use_v2 = args.v2 or os.environ.get("VEDIOCUT_VISUAL_V2", "") not in ("", "0", "false")
    if use_v2 and project.language == "en":
        # v2（Chinese-CLIP）文字塔非英文原生，且 FUSION_V2_DEFAULTS 的 visFloor/
        # visCeil/sepMargin 全是依中文語料實測校準（見模組頂部註解）；英文查詢丟
        # 進去只會拿到失去校準意義的餘弦分佈，違反本專案「寧缺勿錯」的核心原則。
        # en 專案已有 v1 英文 CLIP 原生可用，故這裡自動退回 v1，而非硬照旗標執行。
        _log(f"[broll] 警告：專案 {project.id} language=en，但要求 --v2（Chinese-CLIP，"
             f"中文文字塔）。v2 的融合門檻依中文語料校準，套用到英文查詢會失準且違反"
             f"寧缺勿錯原則 → 自動退回 v1 英文 CLIP（如需強制 v2 請自行修改本檔或另開 issue）。")
        use_v2 = False
    index_path = project.visual_index.with_name("visual_index_v2.json") if use_v2 else None
    vi, idx = load_visual_index({"enabled": fusion_enabled}, project, index_path=index_path)

    broll, stats = generate(transcript, edl["aroll"], shot_index, cfg,
                            existing_broll=edl.get("broll"), vi=vi, idx=idx, project=project)
    print(f"策略={stats['strategy']} 融合={stats['fusion']}({stats['indexKind']}) "
          f"分離度門檻={stats['sepGate']} 預算={stats['budget']} "
          f"候選={stats['candidates']} 選中={stats['picked']}"
          f"（鎖定 {stats['locked']}、視覺新候選 {stats['visualOnly']}、總長 {stats['totalSec']}s）")
    for x in broll:
        lk = " [locked]" if x.get("locked") else ""
        print(f"  {x['overAroll']} +{x['startInClip']}s {x['duration']}s ← "
              f"{x['assetId']}@{x['sourceIn']}  (conf {x['confidence']:.2f}){lk} {x['reason']}")
    for seg in stats.get("lowConf", []):
        print(f"  {seg['overAroll']} → 候選不足，建議生成式/留白（{seg['why']}）"
              f"｜台詞「{seg['text'][:22]}」")

    if not args.dry:
        edl["broll"] = broll
        project.edl.write_text(
            json.dumps(edl, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"→ 已寫回 {project.edl} 的 broll[]（{len(broll)} 段）")


if __name__ == "__main__":
    main()
