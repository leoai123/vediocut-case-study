#!/usr/bin/env python3
"""LLM provider adapter —— 逐字稿後校正等任務共用的最小 LLM 呼叫介面。

零相依原則：全部用 stdlib（urllib）直接打 API，不引第三方 SDK/套件。

用法：
    try:
        import llm_providers                  # python3 app/llm_providers.py 直接執行
    except ModuleNotFoundError:
        from app import llm_providers          # 被其他模組以 app.X 匯入時

    provider = llm_providers.get_provider()    # 依環境變數自動選 Anthropic → OpenAI → None
    if provider is None:
        print("無 API key，跳過校正")
    else:
        text = provider.complete(system="...", user="...", max_tokens=4096)

介面：
    LLMProvider.complete(system: str, user: str, max_tokens: int = 4096) -> str
        送出一輪 system+user 對話，回傳模型輸出的純文字（第一個 text block／choice）。
        任何 HTTP、逾時、回應格式錯誤一律包成 LLMProviderError 往外丟——呼叫端自行決定
        要不要吞掉（例如逐批處理時「整批跳過並 log」）。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from abc import ABC, abstractmethod

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"

# 校正 pass 預設模型：2026-07-25 由 Haiku 升級為 Sonnet。原因——校正的關鍵能力是
# 「用世界知識還原專名誤聽」（地區性的地名/站名/菜名/品牌），這是 Haiku 的弱項：
# p6 實測 Haiku 未能還原兩處在地專名。成本從約 $0.09/支升到約 $0.5-1/支，
# 在品質優先的階段值得。無 API key 時的代理路徑也一併改派 Sonnet。
# 註：prompt 本身不預設任何地區——要求模型先從逐字稿推斷地區與領域再運用知識，
# 這樣同一套流程可跨地區/跨語言通用（見 scripts/correct_transcript.py 的第一步指示）。
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"

# 定價常數（USD / 1M tokens）。快取於 2026-06-24（見 docs/asr_accuracy_options.md 第4節「LLM 後校正」）。
# 只用來做費用估算，不影響呼叫行為；模型不在表內時呼叫端自行決定要不要略過估算。
PRICING_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "claude-sonnet-5": {"input": 2.00, "output": 10.00},  # 2026-08-31 前促銷價
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
}


class LLMProviderError(RuntimeError):
    """LLM 呼叫失敗（HTTP 錯誤、逾時、回應格式異常、缺 API key）。"""


class LLMProvider(ABC):
    """抽象介面：只留一個方法，方便未來換供應商或加新 provider。"""

    name: str = "base"
    model: str = ""
    last_usage: dict = {}

    @abstractmethod
    def complete(self, system: str, user: str, max_tokens: int = 4096) -> str:
        """送出 system+user 單輪對話，回傳模型輸出的純文字。"""
        raise NotImplementedError


class AnthropicProvider(LLMProvider):
    """用 stdlib urllib 直打 https://api.anthropic.com/v1/messages（POST）。"""

    name = "anthropic"

    def __init__(self, api_key: str | None = None, model: str | None = None,
                 timeout: float = 120.0):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise LLMProviderError("ANTHROPIC_API_KEY 未設定")
        self.model = model or os.environ.get("VEDIOCUT_LLM_MODEL") or DEFAULT_ANTHROPIC_MODEL
        self.timeout = timeout
        self.last_usage = {}

    def complete(self, system: str, user: str, max_tokens: int = 4096) -> str:
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        req = urllib.request.Request(
            ANTHROPIC_API_URL,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": ANTHROPIC_VERSION,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise LLMProviderError(f"Anthropic API HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise LLMProviderError(f"Anthropic API 連線失敗: {e}") from e
        self.last_usage = body.get("usage", {})
        for block in body.get("content", []):
            if block.get("type") == "text":
                return block.get("text", "")
        raise LLMProviderError(f"Anthropic 回應沒有 text block（可能是 refusal 或空回應）: {body}")


class OpenAIProvider(LLMProvider):
    """用 stdlib urllib 直打 https://api.openai.com/v1/chat/completions（POST）。"""

    name = "openai"

    def __init__(self, api_key: str | None = None, model: str | None = None,
                 timeout: float = 120.0):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not self.api_key:
            raise LLMProviderError("OPENAI_API_KEY 未設定")
        self.model = model or os.environ.get("VEDIOCUT_LLM_MODEL") or DEFAULT_OPENAI_MODEL
        self.timeout = timeout
        self.last_usage = {}

    def complete(self, system: str, user: str, max_tokens: int = 4096) -> str:
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        req = urllib.request.Request(
            OPENAI_API_URL,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise LLMProviderError(f"OpenAI API HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise LLMProviderError(f"OpenAI API 連線失敗: {e}") from e
        self.last_usage = body.get("usage", {})
        try:
            return body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as e:
            raise LLMProviderError(f"OpenAI 回應格式異常: {body}") from e


class MockProvider(LLMProvider):
    """離線測試用 provider——完全不打網路。

    預期呼叫端把待校正段落以 JSON 陣列字串 `[{"index": int, "text": str}, ...]`
    當作 `user` 傳入（correct_transcript.py 的實際送法）。`complete()` 解析後對每筆
    text 套用固定替換表，只回傳「有改動」的 `{"index", "corrected"}`。

    guardrail 測試旗標：
        force_invalid_json=True   → complete() 直接回傳非 JSON 字串（模擬解析失敗）
        force_long_rewrite={0, 3} → 指定 index 的 corrected 會被灌水成超長重寫
                                      （觸發「長度差 >40% 丟棄」guardrail）
    """

    name = "mock"
    model = "mock"

    def __init__(self, replacements: dict[str, str] | None = None,
                 force_invalid_json: bool = False,
                 force_long_rewrite: set[int] | None = None):
        self.replacements = dict(replacements or {})
        self.force_invalid_json = force_invalid_json
        self.force_long_rewrite = set(force_long_rewrite or ())
        self.last_usage = {}

    def complete(self, system: str, user: str, max_tokens: int = 4096) -> str:
        if self.force_invalid_json:
            return "（模擬壞掉的回應：這不是 JSON，用來測試 parse 失敗時整批跳過的 guardrail）"
        try:
            batch = json.loads(user)
        except (json.JSONDecodeError, TypeError):
            batch = []
        out = []
        for item in batch:
            idx = item.get("index")
            text = item.get("text", "")
            corrected = text
            for bad, good in self.replacements.items():
                corrected = corrected.replace(bad, good)
            if idx in self.force_long_rewrite:
                # 刻意灌水成好幾倍長，模擬 LLM「過度改寫」的失控情境
                corrected = "、".join([corrected] * 6) + "（測試用超長重寫）"
            if corrected != text:
                out.append({"index": idx, "corrected": corrected})
        return json.dumps(out, ensure_ascii=False)


def get_provider(name: str | None = None) -> LLMProvider | None:
    """依 name 或環境變數選 provider。

    name:
      - None / "auto"：ANTHROPIC_API_KEY 存在 → AnthropicProvider；
                        否則 OPENAI_API_KEY 存在 → OpenAIProvider；
                        都沒有 → None（呼叫端負責印出「無 API key，跳過校正」之類的清楚訊息，不是 crash）。
      - "anthropic" / "openai"：明確指定；對應 API key 不存在時同樣回傳 None（不 raise）。
      - "mock"：永遠回傳一個沒有替換規則的 MockProvider()（原文照傳）；
                需要注入替換表/guardrail 測試旗標時，呼叫端請直接 `MockProvider(...)`。
    """
    key = (name or "auto").lower()
    if key == "mock":
        return MockProvider()
    if key == "anthropic":
        return AnthropicProvider() if os.environ.get("ANTHROPIC_API_KEY") else None
    if key == "openai":
        return OpenAIProvider() if os.environ.get("OPENAI_API_KEY") else None
    if key == "auto":
        if os.environ.get("ANTHROPIC_API_KEY"):
            return AnthropicProvider()
        if os.environ.get("OPENAI_API_KEY"):
            return OpenAIProvider()
        return None
    raise ValueError(f"未知的 provider 名稱: {name!r}（可用: auto/mock/anthropic/openai）")


if __name__ == "__main__":
    # 快速自我測試（零依賴，不打網路）：確認 MockProvider 的替換/JSON 行為正常。
    demo = MockProvider(replacements={"鷹哥": "鶯歌", "生死去": "生鮮區"})
    sample = json.dumps(
        [{"index": 0, "text": "我們現在在鷹哥車站"}, {"index": 1, "text": "這句沒有錯字"}],
        ensure_ascii=False,
    )
    print("mock complete() ->", demo.complete("system prompt 略", sample))
    print("get_provider(auto) without any key ->", get_provider("auto"))
