#!/usr/bin/env python3
"""LLM 逐字稿後校正 pass —— 用 Haiku 級模型依上下文修正同音錯字與專有名詞。

背景與方案：見 docs/asr_accuracy_options.md 第4節「LLM 後校正」。轉錄（merge_transcript.py）
完成後，把每個 segment 的 {index, text}（不含時間碼）連同專案 manifest 的 glossary
（專有名詞表）送給 LLM，只允許它做「同音錯字/專有名詞」層級的小幅替換：不能動時間碼、
不能增刪段落、不能整句重寫。

寫回規則：text = 校正後文字，textRaw 永遠保留原始 ASR 輸出不動。已經校正過的段落
（text != textRaw）預設跳過，--force 才重新校正。寫檔前一律備份
transcript.json -> transcript.bak.json。

用法：
  python3 scripts/correct_transcript.py --project p2 --dry --provider mock \\
      --mock-replacements '{"鷹哥":"鶯歌"}'
  python3 scripts/correct_transcript.py --project p2 --dry              # 真的呼叫 API，只印 diff
  python3 scripts/correct_transcript.py --project p2                    # 呼叫 API 並寫回（有備份）

  --provider 預設 auto：有 ANTHROPIC_API_KEY 就用 Anthropic，否則 OPENAI_API_KEY 用 OpenAI，
  都沒有就印出清楚訊息並跳過（不會 crash，也不會硬造假資料）。

未帶 --project 時，走 app/project_paths.load() 的解析順序
（明確參數 > VEDIOCUT_PROJ > current_project 指標 > 唯一專案）。
"""
import argparse
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))   # 讓 `python3 scripts/correct_transcript.py` 也能 import app.*
import app.project_paths as project_paths  # noqa: E402  單一真相來源：路徑一律經此模組解析
import app.llm_providers as llm_providers  # noqa: E402

DEFAULT_BATCH = 60
MAX_LEN_RATIO = 0.4  # corrected 與原文長度差超過這個比例 -> 視為過度改寫，丟棄
REWRITE_WARN_RATIO = 0.02  # 長度差超過這個比例 -> 印警告要求人工覆核（純錯字替換應接近 0%）


# ---------------------------------------------------------------------------
# prompt 組裝
# ---------------------------------------------------------------------------

def build_system_prompt(glossary: list[str], lang: str = "zh") -> str:
    """依素材語言（Project.language）選校正策略。zh 用繁體中文同音錯字校對員；
    en（及其他尚無專用策略的語言碼，安全預設共用 en 版）用英文 ASR 校對員——
    輸出格式契約（JSON 陣列/欄位名）與 zh 版完全相同，只有系統提示語言與校正重點不同。"""
    if lang == "zh":
        glossary_txt = "、".join(glossary) if glossary else "（無——完全依你自己的知識與上下文判斷）"
        return (
            "你是影片逐字稿的校對員，任務是修正語音辨識（ASR）造成的同音錯字與專有名詞誤植。"
            "使用者會給你一批逐字稿段落（純文字，不含時間碼）。\n\n"
            "【第一步：先判斷內容的脈絡】\n"
            "動筆前先從逐字稿本身推斷這支影片的：所在地區（從地名、街道、交通、貨幣、"
            "店家型態、語言變體等線索判斷）、主題領域（飲食／3C／旅遊／房產…）、"
            "以及使用的詞彙圈。**不要預設任何特定地區**——文本會告訴你。\n\n"
            "【第二步：用該脈絡的世界知識還原可疑詞】\n"
            "遇到在上下文中不合理的詞，主動問自己：「這是不是某個真實專名的誤聽？」"
            "然後用你對**你剛判斷出的那個地區與領域**的知識還原它。推理模式：\n"
            "1. 這個詞在句子裡語意不通，或與前後文的主題不搭\n"
            "2. 唸起來像什麼？列出同音或近音的候選\n"
            "3. 候選中有沒有「在這個地區、這個領域真實存在」的專名"
            "（地名、車站、店家型態、菜名、食材、品牌、當地慣用語）？\n"
            "4. 有 → 替換；沒有 → 保留原文並標低信心\n"
            "當地的飲食詞彙、地理名稱、交通站名、連鎖品牌、流行用語都在你的知識範圍內，"
            "請大方運用；這比任何人工詞表都可靠、也更能跨地區通用。\n\n"
            f"本片已知的專有名詞表（若有，優先採用；但**不代表只能改這些**）：{glossary_txt}\n\n"
            "【信心分級——這是必填欄位】\n"
            "每筆修正都要標 confidence：\n"
            "- \"high\"：你確定這是某個真實專名/常見詞的誤聽（如上面四個範例）\n"
            "- \"medium\"：語意上合理但你不完全確定（例如可能是某家店的自訂商品名）\n"
            "- \"low\"：你猜的，需要人工用畫面或搜尋驗證\n"
            "medium/low 也要回報（下游會據此決定是否抽幀或搜尋驗證），不要因為不確定就整筆放棄。\n\n"
            "規則（務必嚴格遵守，違反視同任務失敗）：\n"
            "1. 只能替換字詞，不能改變語序、語氣、標點風格，不能新增或刪除任何字句"
            "（**特別是不能刪掉子句**，例如把「但如果要找過季的話 我推薦…」改成「所以我推薦…」"
            "就是違規），不能合併或拆分段落。\n"
            "2. 每個輸入段落是 {\"index\": 整數, \"text\": 原始文字}。\n"
            "3. 只回傳「有修改」的段落，格式必須是一個 JSON 陣列："
            "[{\"index\": 整數, \"corrected\": \"修正後文字\", \"confidence\": \"high|medium|low\", "
            "\"note\": \"一句話說明依據\"}, ...]。完全沒有需要修改的段落時回傳空陣列 []。\n"
            "4. 修改幅度應該很小（通常只換一兩個字）；絕對不要整句重寫或補充內容"
            "——那等於捏造講者沒說過的話。**修正前後的字數應該幾乎相同**。\n"
            "5. 明顯是多人重疊對話、環境音污染（鄰桌講話被錄進來）造成的無意義文字，"
            "**保留原文不要修**——那不是錯字，修了等於捏造。\n"
            "6. 講者自己講錯又當場更正的（「我剛剛講錯了，是XXX」），"
            "兩句都保留原文——那是內容不是錯誤。\n"
            "7. 只輸出 JSON 陣列本身，不要加任何說明文字，也不要用 markdown code fence 包起來。"
        )
    # en（以及其他未特別支援的語言碼，共用此版作為安全預設）
    glossary_txt = ", ".join(glossary) if glossary else "(none — use context to judge)"
    return (
        "You are a proofreader for video transcripts. Your task is to fix automatic "
        "speech recognition (ASR) mishearings and misspelled proper nouns. You will be "
        "given a batch of transcript segments (plain text, no timestamps).\n\n"
        "[Step 1: infer the context first]\n"
        "Before making any edit, infer from the transcript itself: the region it is set in "
        "(from place names, streets, transit, currency, business types, dialect markers), "
        "the subject domain (food / tech / travel / real estate / …), and the vocabulary "
        "world it draws on. **Do not assume any particular country or region** — the text "
        "will tell you.\n\n"
        "[Step 2: restore suspicious words using knowledge of that context]\n"
        "When a word does not make sense in context, ask yourself: \"is this a mishearing "
        "of a real proper noun?\" Then use your knowledge of **the region and domain you "
        "just inferred** to restore it. Reasoning pattern:\n"
        "1. The word is semantically off, or clashes with the surrounding topic\n"
        "2. What does it sound like? List same- or near-homophone candidates\n"
        "3. Does any candidate name something that really exists in this region and domain "
        "(place, station, dish, ingredient, brand, local idiom)?\n"
        "4. Yes -> replace; No -> keep the original and mark low confidence\n"
        "Local food vocabulary, geography, transit names, chain brands and slang are all "
        "within your knowledge — use them freely. This is more reliable than any hand-built "
        "word list, and it generalizes across regions.\n\n"
        f"Known proper nouns for this video (use them first if present, but this is **not** "
        f"the limit of what you may correct): {glossary_txt}\n\n"
        "[Confidence grading — this field is required]\n"
        "Every correction must carry a confidence value:\n"
        "- \"high\": you are sure this is a mishearing of a real proper noun / common term\n"
        "- \"medium\": plausible but not certain (e.g. it may be a shop's own product name)\n"
        "- \"low\": a guess; a human should verify it against the footage or a web search\n"
        "Report medium/low ones too (downstream will verify them) — do not silently give up "
        "on a segment just because you are unsure.\n\n"
        "Rules (follow strictly; violating any of these counts as a failed task):\n"
        "1. Only replace words. Do not change word order, tone, or punctuation style; do not "
        "add or remove any words or clauses (**especially do not drop a clause** — turning "
        "\"but if you want off-season stuff, I recommend...\" into \"so I recommend...\" is a "
        "violation); do not merge or split segments.\n"
        "2. Each input segment is {\"index\": integer, \"text\": original text}.\n"
        "3. Only return segments that have a correction, as a single JSON array: "
        "[{\"index\": integer, \"corrected\": \"corrected text\", \"confidence\": "
        "\"high|medium|low\", \"note\": \"one line on your basis\"}, ...]. "
        "Return an empty array [] if nothing needs changing.\n"
        "4. Edits should be small (usually one or two words) — never rewrite a whole sentence "
        "or add content the speaker didn't say. **Character count should stay nearly identical.**\n"
        "5. Text that is clearly garbled by overlapping speakers or ambient noise (a "
        "neighbouring table's conversation picked up by the mic) must be **left as-is** — "
        "that is not a typo, and \"fixing\" it fabricates content.\n"
        "6. When the speaker misspeaks and corrects themselves on camera (\"sorry, I meant "
        "X\"), keep both lines as-is — that is content, not error.\n"
        "7. Output only the JSON array itself — no explanation text, and do not wrap it "
        "in a markdown code fence."
    )


def build_user_payload(segments: list[dict], indices: list[int]) -> str:
    batch = [{"index": i, "text": segments[i]["text"]} for i in indices]
    return json.dumps(batch, ensure_ascii=False)


def estimate_max_tokens(n_segments: int) -> int:
    return min(8192, max(1024, 200 + n_segments * 150))


_FENCE_START = re.compile(r"^```(?:json)?\s*", re.IGNORECASE)
_FENCE_END = re.compile(r"\s*```$")


def parse_llm_response(raw: str):
    """回傳 list（成功）或 None（解析失敗——呼叫端要整批跳過並 log）。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = _FENCE_START.sub("", text)
        text = _FENCE_END.sub("", text)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, list):
        return None
    return data


# ---------------------------------------------------------------------------
# guardrails
# ---------------------------------------------------------------------------

def apply_batch_result(segments: list[dict], batch_indices: list[int],
                        parsed_items: list, logs: list[str]):
    """把 LLM 回傳的修改套用到 segments（只改 text，其他欄位不動）。

    回傳 (changed_indices, dropped_count)。任何不符合 guardrail 的項目一律丟棄並 log。
    """
    valid_indices = set(batch_indices)
    changed_indices: list[int] = []
    dropped = 0
    for item in parsed_items:
        if not isinstance(item, dict):
            logs.append(f"跳過：項目不是物件 -> {item!r}")
            dropped += 1
            continue
        idx = item.get("index")
        corrected = item.get("corrected")
        if not isinstance(idx, int) or idx not in valid_indices:
            logs.append(f"跳過：index {idx!r} 不存在（不在這批範圍內）")
            dropped += 1
            continue
        if not isinstance(corrected, str) or not corrected.strip():
            logs.append(f"跳過：index {idx} 的 corrected 是空的/非字串")
            dropped += 1
            continue
        original = segments[idx]["text"]
        orig_len = max(1, len(original))
        ratio = abs(len(corrected) - len(original)) / orig_len
        if ratio > MAX_LEN_RATIO:
            logs.append(
                f"跳過：index {idx} 長度差 {ratio:.0%} 超過門檻 {MAX_LEN_RATIO:.0%}"
                f"（疑似整句重寫） 原文={original!r} 建議={corrected!r}")
            dropped += 1
            continue
        # 純同音字替換的長度變化幾乎為 0；超過 REWRITE_WARN_RATIO 就記錄警告供人工覆核。
        # p6 事故：Haiku 把「但如果要找過季的話 我推薦…」改成「所以我推薦…」刪掉整個子句，
        # 長度只差 18%、沒觸發 MAX_LEN_RATIO(40%) —— guardrail 抓不到刪子句，只能靠這裡標記。
        if ratio > REWRITE_WARN_RATIO:
            logs.append(
                f"⚠ 需人工覆核：index {idx} 長度變化 {ratio:.0%}（純錯字替換應接近 0%，"
                f"疑似增刪字句） 原文={original!r} 建議={corrected!r}")
        conf = item.get("confidence")
        if conf in ("medium", "low"):
            note = item.get("note") or ""
            logs.append(f"⚠ 低信心（{conf}）：index {idx} {original!r} → {corrected!r}"
                        + (f"｜依據：{note}" if note else ""))
            segments[idx]["correctionConfidence"] = conf
            if note:
                segments[idx]["correctionNote"] = note
        if corrected == original:
            continue
        segments[idx]["text"] = corrected
        changed_indices.append(idx)
    return changed_indices, dropped


def _snapshot(segments: list[dict]):
    """時間碼 + assetId + textRaw 的快照，用來 assert 這些欄位在校正前後完全一致。"""
    return tuple((s["assetId"], s["sourceStart"], s["sourceEnd"], s["textRaw"]) for s in segments)


# ---------------------------------------------------------------------------
# 核心流程
# ---------------------------------------------------------------------------

def usage_tokens(provider) -> tuple[int, int]:
    usage = getattr(provider, "last_usage", None) or {}
    if "input_tokens" in usage or "output_tokens" in usage:      # Anthropic 形狀
        return usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    if "prompt_tokens" in usage or "completion_tokens" in usage:  # OpenAI 形狀
        return usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
    return 0, 0


def estimate_cost(model: str, input_tokens: int, output_tokens: int):
    pricing = llm_providers.PRICING_PER_MTOK.get(model)
    if not pricing:
        return None
    return (input_tokens / 1_000_000 * pricing["input"]
            + output_tokens / 1_000_000 * pricing["output"])


def correct_segments(segments: list[dict], glossary: list[str], provider,
                      batch_size: int, force: bool, limit, lang: str = "zh") -> dict:
    stats = {
        "eligible": 0, "sent": 0, "batches": 0, "skipped_batches": 0,
        "changed": 0, "dropped": 0, "logs": [], "input_tokens": 0,
        "output_tokens": 0, "changed_indices": [],
    }
    eligible = [i for i, s in enumerate(segments) if force or s["text"] == s["textRaw"]]
    if limit is not None:
        eligible = eligible[:limit]
    stats["eligible"] = len(eligible)
    if not eligible:
        return stats

    before_snapshot = _snapshot(segments)
    system_prompt = build_system_prompt(glossary, lang)

    for start in range(0, len(eligible), batch_size):
        batch_indices = eligible[start:start + batch_size]
        batch_no = stats["batches"] + 1
        stats["batches"] = batch_no
        stats["sent"] += len(batch_indices)

        user_payload = build_user_payload(segments, batch_indices)
        max_tokens = estimate_max_tokens(len(batch_indices))
        try:
            raw = provider.complete(system_prompt, user_payload, max_tokens=max_tokens)
        except llm_providers.LLMProviderError as e:
            stats["skipped_batches"] += 1
            stats["logs"].append(f"batch {batch_no}: API 呼叫失敗，整批跳過 -> {e}")
            continue

        in_tok, out_tok = usage_tokens(provider)
        stats["input_tokens"] += in_tok
        stats["output_tokens"] += out_tok

        parsed = parse_llm_response(raw)
        if parsed is None:
            stats["skipped_batches"] += 1
            stats["logs"].append(
                f"batch {batch_no}: 回應不是合法 JSON 陣列，整批跳過。"
                f"原始回應前 200 字：{raw[:200]!r}")
            continue

        batch_logs: list[str] = []
        changed_indices, dropped = apply_batch_result(segments, batch_indices, parsed, batch_logs)
        stats["dropped"] += dropped
        stats["changed"] += len(changed_indices)
        stats["changed_indices"].extend(changed_indices)
        stats["logs"].extend(f"batch {batch_no}: {line}" for line in batch_logs)

    after_snapshot = _snapshot(segments)
    assert len(before_snapshot) == len(after_snapshot), "guardrail 違反：段數改變了！"
    assert before_snapshot == after_snapshot, (
        "guardrail 違反：assetId/sourceStart/sourceEnd/textRaw 其中之一被動到了！")
    return stats


def backup_path(transcript_path: Path) -> Path:
    return transcript_path.with_name(transcript_path.stem + ".bak" + transcript_path.suffix)


def write_with_backup(transcript_path: Path, doc: dict) -> Path:
    bak = backup_path(transcript_path)
    shutil.copy2(transcript_path, bak)
    transcript_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return bak


def print_diff(segments: list[dict], changed_indices: list[int]) -> None:
    if not changed_indices:
        print("  （沒有段落被修改）")
        return
    for idx in sorted(set(changed_indices)):
        seg = segments[idx]
        print(f"  [{idx}] {seg['assetId']} {seg['sourceStart']:.2f}-{seg['sourceEnd']:.2f}s")
        print(f"      原始: {seg['textRaw']}")
        print(f"      校正: {seg['text']}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_index_set(raw) -> set:
    if not raw:
        return set()
    return {int(x) for x in raw.split(",") if x.strip() != ""}


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    project_paths.add_project_arg(ap)
    ap.add_argument("--dry", action="store_true", help="只印 diff，不寫回 transcript.json")
    ap.add_argument("--batch", type=int, default=DEFAULT_BATCH,
                     help=f"每個 LLM 請求的段落數（預設 {DEFAULT_BATCH}）")
    ap.add_argument("--provider", choices=["auto", "mock", "anthropic", "openai"],
                     default="auto", help="LLM provider（預設 auto：偵測 API key 自動選）")
    ap.add_argument("--force", action="store_true",
                     help="已校正過（text != textRaw）的段落預設跳過，加這個旗標才重新校正")
    ap.add_argument("--limit", type=int, default=None,
                     help="只處理前 N 個可校正段落（供小規模測試用，例如 --limit 10）")
    ap.add_argument("--mock-replacements", default=None,
                     help='僅 --provider mock 生效：JSON 物件字串，例如 \'{"鷹哥":"鶯歌"}\'')
    ap.add_argument("--mock-force-invalid-json", action="store_true",
                     help="僅 --provider mock 生效：測試「回應解析失敗→整批跳過」guardrail")
    ap.add_argument("--mock-force-long-rewrite", default=None,
                     help="僅 --provider mock 生效：逗號分隔的段落 index，"
                          "模擬該段被過度改寫以觸發「長度差 guardrail」，例如 '0,3'")
    # Claude Code 離線流轉：LLM 步驟由 Claude Code 內的 Haiku 代理執行，不需 API key。
    # 流程：--emit-batches 匯出批次 → Haiku 代理讀檔產出 corrections JSON → --apply 寫回
    #（--apply 走與 provider 路徑完全相同的 guardrails：批次成員、長度差、段數/時間碼 assert）
    ap.add_argument("--emit-batches", metavar="FILE", default=None,
                     help="不呼叫 LLM，把待校正批次（system+payload）匯出成 JSON 供外部 LLM 處理")
    ap.add_argument("--apply", metavar="FILE", default=None,
                     help='讀入外部 LLM 產出的 corrections JSON（[{"index":int,"corrected":str}]）'
                          "，經全部 guardrails 後寫回（可配 --dry 先看 diff）")
    return ap


def resolve_provider(args):
    if args.provider == "mock":
        replacements = json.loads(args.mock_replacements) if args.mock_replacements else None
        return llm_providers.MockProvider(
            replacements=replacements,
            force_invalid_json=args.mock_force_invalid_json,
            force_long_rewrite=parse_index_set(args.mock_force_long_rewrite),
        )
    return llm_providers.get_provider(args.provider)


def eligible_indices(segments: list[dict], force: bool, limit) -> list[int]:
    """與 correct_segments 相同的可校正段落挑選邏輯（emit/apply 共用）。"""
    idx = [i for i, s in enumerate(segments) if force or s["text"] == s["textRaw"]]
    return idx[:limit] if limit is not None else idx


def emit_batches(project, segments, glossary, args) -> int:
    idx = eligible_indices(segments, args.force, args.limit)
    if not idx:
        print("沒有可校正段落，無批次可匯出")
        return 0
    batches = []
    for start in range(0, len(idx), args.batch):
        bi = idx[start:start + args.batch]
        batches.append({"batch": len(batches), "indices": bi,
                        "payload": build_user_payload(segments, bi)})
    out = Path(args.emit_batches)
    out.write_text(json.dumps({
        "project": project.id,
        "system": build_system_prompt(glossary, project.language),
        "responseSpec": ("對每一批：只回傳需要修改的段落，格式為 JSON 陣列 "
                         '[{"index": <int>, "corrected": "<修正後全文>"}]。'
                         "全部批次的結果合併成單一陣列寫入 corrections 檔。"),
        "batches": batches,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已匯出 {len(batches)} 批 / {len(idx)} 段 → {out}")
    return 0


def apply_corrections(project, doc, segments, args) -> int:
    raw = json.loads(Path(args.apply).read_text(encoding="utf-8"))
    corrections = raw.get("corrections", raw) if isinstance(raw, dict) else raw
    if not isinstance(corrections, list):
        print("corrections 檔格式錯誤：需要 JSON 陣列", file=sys.stderr)
        return 1
    allowed = eligible_indices(segments, args.force, args.limit)
    before_snapshot = _snapshot(segments)
    changed, dropped = apply_batch_result(
        segments, allowed, corrections, logs := [])
    after_snapshot = _snapshot(segments)
    assert len(before_snapshot) == len(after_snapshot), "guardrail 違反：段數改變了！"
    assert before_snapshot == after_snapshot, (
        "guardrail 違反：assetId/sourceStart/sourceEnd/textRaw 其中之一被動到了！")
    print(f"外部 corrections：{len(corrections)} 筆 → 採用 {len(changed)} 筆"
          f"（guardrail 丟棄 {dropped} 筆）")
    for line in logs:
        print(f"  - {line}")
    print("\n變更段落 diff：")
    print_diff(segments, changed)
    if args.dry:
        print("\n--dry：只印 diff，未寫回 transcript.json")
        return 0
    if changed:
        bak = write_with_backup(project.transcript, doc)
        print(f"\n已寫回 {project.transcript}（備份於 {bak}）")
    else:
        print("\n無變更，未寫檔")
    return 0


def main() -> int:
    args = build_arg_parser().parse_args()

    with project_paths.cli_guard():
        project = project_paths.load(args.project)
    glossary = project.manifest.get("glossary", [])

    # Claude Code 離線流轉（emit/apply 不需要 provider/API key）
    if args.emit_batches or args.apply:
        doc = json.loads(project.transcript.read_text(encoding="utf-8"))
        segments = doc.get("segments", [])
        if not segments:
            print(f"{project.transcript} 沒有 segments，略過", file=sys.stderr)
            return 1
        if args.emit_batches:
            return emit_batches(project, segments, glossary, args)
        return apply_corrections(project, doc, segments, args)

    provider = resolve_provider(args)
    if provider is None:
        # fallback 鏈：有 key → 直接 API；無 key → Claude Code 內的 Haiku 代理離線流轉。
        # 本腳本無法自行召喚代理，由 Claude Code 協調層看到這個訊息後接手三步驟。
        print(f"無 API key（provider={args.provider}）→ 請走 Claude Code Haiku 代理流轉：\n"
              f"  1. python3 scripts/correct_transcript.py --project {project.id} "
              f"--emit-batches /tmp/{project.id}_batches.json\n"
              f"  2. 由 Claude Code 派 Haiku 代理讀批次檔、產出 corrections JSON\n"
              f"  3. python3 scripts/correct_transcript.py --project {project.id} "
              f"--apply <corrections.json> --dry 檢視後正式寫回")
        return 2

    doc = json.loads(project.transcript.read_text(encoding="utf-8"))
    segments = doc.get("segments", [])
    if not segments:
        print(f"{project.transcript} 沒有 segments，略過", file=sys.stderr)
        return 1

    print(f"專案 {project.id}：{len(segments)} 段逐字稿；"
          f"provider={provider.name} model={getattr(provider, 'model', '-')}")
    if glossary:
        print(f"專有名詞表（{len(glossary)} 個）：{'、'.join(glossary)}")
    else:
        print("（manifest 沒有 glossary，僅靠 LLM 自行依上下文判斷）")

    stats = correct_segments(segments, glossary, provider,
                              batch_size=args.batch, force=args.force, limit=args.limit,
                              lang=project.language)

    force_note = "（--force：連已校正過的段落也重校）" if args.force else "（已校正過的段落預設跳過）"
    print(f"\n可校正段落：{stats['eligible']} {force_note}")
    print(f"送出：{stats['sent']} 段 / {stats['batches']} 批"
          f"（呼叫或解析失敗而整批跳過：{stats['skipped_batches']} 批）")
    print(f"改動：{stats['changed']} 段（guardrail 丟棄：{stats['dropped']} 筆）")

    if stats["logs"]:
        print("\nguardrail / 例外紀錄：")
        for line in stats["logs"]:
            print(f"  - {line}")

    in_tok, out_tok = stats["input_tokens"], stats["output_tokens"]
    if in_tok or out_tok:
        cost = estimate_cost(getattr(provider, "model", ""), in_tok, out_tok)
        cost_txt = f"${cost:.4f}" if cost is not None else "（模型不在定價表內，無法估算）"
        print(f"\ntoken 用量：input={in_tok} output={out_tok}  估算費用：{cost_txt}")
    elif provider.name != "mock" and stats["batches"] > 0:
        print("\n（沒有量到 usage，可能所有批次都被跳過了——看上面的 guardrail 紀錄）")

    print("\n變更段落 diff：")
    print_diff(segments, stats["changed_indices"])

    if args.dry:
        print("\n--dry：只印 diff，未寫回 transcript.json")
        return 0

    if stats["changed"] == 0:
        print("\n沒有任何段落被改動，略過寫檔")
        return 0

    doc["segments"] = segments
    bak = write_with_backup(project.transcript, doc)
    print(f"\n已寫回 {project.transcript}（備份於 {bak}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
