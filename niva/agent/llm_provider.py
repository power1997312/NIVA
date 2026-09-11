# -*- coding: utf-8 -*-
"""NIVA 统一 LLM 网关（可插拔、必缓存、可降级）。

三项硬约束（架构方案 §4.5 / §7.2）：
    1. **可插拔** —— 云（OpenAI 兼容）与本地（vLLM / Ollama，同为 OpenAI 兼容）
       通过 base_url + model 切换，业务代码零改动。
    2. **必缓存** —— 按 (model, prompt) 内容哈希落盘缓存。同一输入两次运行必须
       命中缓存，这是"可复现性"指标成立的前提（也是断网重放演示的保险）。
    3. **可降级** —— 未配置密钥 / 服务不可用时，返回 ``ok=False`` 且**绝不抛异常**，
       由调用方切纯规则模式。降级事实必须传给 obs 留痕。

LLM 在本系统中的合法点位只有三个：歧义裁决、语义成文、任务分流。
本模块只提供通道，**不提供任何判定能力**——A3 公理：判定链必须确定性。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .. import config as C

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore


# ---------------------------------------------------------------------
# 供应商预设
# ---------------------------------------------------------------------
PRESETS: dict[str, dict[str, str]] = {
    "deepseek":   {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "opencode":   {"base_url": "https://opencode.ai/zen/go/v1", "model": "deepseek-v4-flash"},
    "local_vllm": {"base_url": "http://127.0.0.1:8000/v1", "model": "Qwen2.5-VL-7B-Instruct"},
    "offline":    {"base_url": "", "model": ""},
}


@dataclass
class LLMResult:
    """统一返回封套。与工具契约保持同构，便于编排层一套逻辑处理成败。"""
    ok: bool
    text: str = ""
    data: Optional[dict[str, Any]] = None
    cached: bool = False
    degraded: bool = False
    degrade_reason: str = ""
    error_code: str = ""
    model: str = ""
    latency_s: float = 0.0
    prompt_sha: str = ""
    usage: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "text": self.text, "data": self.data,
            "cached": self.cached, "degraded": self.degraded,
            "degrade_reason": self.degrade_reason, "error_code": self.error_code,
            "model": self.model, "latency_s": self.latency_s,
            "prompt_sha": self.prompt_sha, "usage": self.usage,
        }


class LLMProvider:
    """OpenAI 兼容的最小客户端。刻意不引入 openai SDK（少一个依赖、少一个失败点）。"""

    def __init__(
        self,
        preset: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        cache_dir: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        # 优先级：显式入参 > 环境变量 > 预设
        preset_name = (preset or os.environ.get("NIVA_LLM_PRESET")
                       or os.environ.get("TRACE_NL_LLM_PRESET") or "").strip().lower()
        pre = PRESETS.get(preset_name, {})
        self.base_url = (base_url or os.environ.get("TRACE_NL_LLM_BASE_URL")
                         or pre.get("base_url") or PRESETS["deepseek"]["base_url"]).rstrip("/")
        self.model = (model or os.environ.get("TRACE_NL_LLM_MODEL")
                      or pre.get("model") or PRESETS["deepseek"]["model"])
        self.api_key = api_key if api_key is not None else os.environ.get(
            "TRACE_NL_LLM_API_KEY", "")
        self.cache_dir = cache_dir or str(C.CACHE_DIR / "llm")
        os.makedirs(self.cache_dir, exist_ok=True)

        # 无密钥即视为未启用（与 Trace_NL 既有行为一致：自动禁用、行为不变）
        self.enabled = bool(self.api_key) if enabled is None else (enabled and bool(self.api_key))

        t = C.thresholds().llm
        self.timeout = int(t.timeout_s)
        self.max_retry = int(t.max_retry)
        self.max_tokens = int(t.max_tokens)
        self.max_tokens_cap = int(t.max_tokens_cap)
        self.temperature = float(t.temperature)
        self.user_agent = os.environ.get(
            "TRACE_NL_LLM_USER_AGENT",
            "NIVA-VnV-Agent/1.0 (nuclear I&C V&V traceability & test design worker)")

    # ------------------------------------------------------------------
    @property
    def is_available(self) -> bool:
        return bool(self.enabled and requests is not None)

    def describe(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "available": self.is_available,
            "base_url": self.base_url,
            "model": self.model,
            "has_key": bool(self.api_key),
            "cache_dir": self.cache_dir,
        }

    # ------------------------------------------------------------------
    # 缓存
    # ------------------------------------------------------------------
    def _cache_path(self, prompt: str, system: str, temperature: float) -> str:
        h = hashlib.sha256()
        for part in (self.model, system, prompt, f"{temperature:.3f}"):
            h.update(part.encode("utf-8"))
            h.update(b"\0")
        return os.path.join(self.cache_dir, h.hexdigest() + ".json")

    def _load_cache(self, path: str) -> Optional[dict[str, Any]]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def _save_cache(self, path: str, payload: dict[str, Any]) -> None:
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # 调用
    # ------------------------------------------------------------------
    def chat(
        self,
        prompt: str,
        system: str = "",
        temperature: Optional[float] = None,
        json_mode: bool = True,
        use_cache: bool = True,
        max_tokens: Optional[int] = None,
    ) -> LLMResult:
        """一次对话调用。**永不抛异常**——失败一律以 ok=False + error_code 返回。"""
        temp = self.temperature if temperature is None else float(temperature)
        prompt_sha = hashlib.sha256((system + "\0" + prompt).encode("utf-8")).hexdigest()[:16]

        if not self.enabled:
            return LLMResult(ok=False, degraded=True, error_code="LLM_NOT_CONFIGURED",
                             degrade_reason="未配置 TRACE_NL_LLM_API_KEY，已降级为纯规则模式",
                             model=self.model, prompt_sha=prompt_sha)
        if requests is None:
            return LLMResult(ok=False, degraded=True, error_code="REQUESTS_MISSING",
                             degrade_reason="requests 未安装", model=self.model,
                             prompt_sha=prompt_sha)

        cache_path = self._cache_path(prompt, system, temp)
        if use_cache:
            hit = self._load_cache(cache_path)
            if hit is not None:
                return LLMResult(ok=True, text=hit.get("text", ""), data=hit.get("data"),
                                 cached=True, model=self.model, prompt_sha=prompt_sha,
                                 usage=hit.get("usage", {}))

        url = f"{self.base_url}/chat/completions"
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temp,
            "max_tokens": int(max_tokens or self.max_tokens),
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
        }

        last_err = ""
        for attempt in range(self.max_retry + 1):
            t0 = time.time()
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
                if resp.status_code == 200:
                    body = resp.json()
                    choice = (body.get("choices") or [{}])[0]
                    text = ((choice.get("message") or {}).get("content") or "")
                    finish = choice.get("finish_reason")
                    # 输出被截断 → 逐步扩容重试（推理型模型思考占用输出 token）
                    if finish == "length" and payload["max_tokens"] < self.max_tokens_cap:
                        payload["max_tokens"] = min(self.max_tokens_cap,
                                                    payload["max_tokens"] * 2)
                        last_err = "OUTPUT_TRUNCATED"
                        continue
                    usage = body.get("usage") or {}
                    data = _maybe_json(text) if json_mode else None
                    rec = {"text": text, "data": data, "usage": usage,
                           "model": self.model, "saved_at": time.time()}
                    if use_cache:
                        self._save_cache(cache_path, rec)
                    return LLMResult(ok=True, text=text, data=data, cached=False,
                                     model=self.model, latency_s=round(time.time() - t0, 3),
                                     prompt_sha=prompt_sha, usage=usage)

                last_err = f"HTTP_{resp.status_code}"
                # 4xx 除 429 外不重试（参数/鉴权问题重试无意义）
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    break
            except Exception as exc:  # 网络/超时/解析
                last_err = f"{type(exc).__name__}"
            time.sleep(min(2 ** attempt * 0.5, 3.0))

        return LLMResult(ok=False, degraded=True, error_code=last_err or "LLM_FAILED",
                         degrade_reason=f"LLM 调用失败（{last_err}），本次降级为纯规则模式",
                         model=self.model, prompt_sha=prompt_sha)


def _maybe_json(text: str) -> Optional[dict[str, Any]]:
    """容错解析：剥掉可能存在的 ```json 围栏后再试。"""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else {"value": obj}
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------
_DEFAULT: Optional[LLMProvider] = None


def provider(reload: bool = False, **kw: Any) -> LLMProvider:
    global _DEFAULT
    if _DEFAULT is None or reload or kw:
        _DEFAULT = LLMProvider(**kw)
    return _DEFAULT
