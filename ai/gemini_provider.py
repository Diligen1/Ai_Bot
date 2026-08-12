"""Gemini implementation of AIProvider (AI Analyzer V1).

Uses only the Python stdlib (`urllib.request`) — no extra dependency. Sends a
compact JSON snapshot (see AIAnalyzer.build_snapshot), asks Gemini to score
the ALREADY-BUILT Trade Setup, and expects strict JSON back. The API key is
only ever held in memory for the duration of a request; it is never logged,
never included in an exception message, and never persisted anywhere.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from ai.provider import (
    AIProvider,
    AIProviderAPIError,
    AIProviderError,
    AIProviderInvalidResponse,
    AIProviderRateLimited,
    AIProviderTimeout,
)

REQUIRED_RESPONSE_FIELDS = ('direction', 'ai_score', 'decision', 'reasons', 'risk_flags')

PROMPT_TEMPLATE = """You are an advisory trading analyst reviewing an ALREADY-BUILT trade setup for a paper/virtual futures account. You do NOT choose the entry, stop loss, take profit, leverage, or position size — those are fixed inputs below and must not be changed or re-derived. Your only job is to judge whether this existing setup looks sound.

Setup snapshot (JSON):
{snapshot_json}

Respond with STRICT JSON only, no prose, no markdown fences, matching exactly this shape:
{{
  "direction": "LONG" | "SHORT" | "NEUTRAL",
  "ai_score": <integer 0-100>,
  "decision": "CONFIRM" | "REJECT" | "WAIT",
  "reasons": ["short reason", ...],
  "risk_flags": ["short risk flag", ...]
}}"""


class GeminiProvider(AIProvider):
    API_URL_TEMPLATE = 'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'

    def __init__(
        self,
        api_key: str,
        model: str = 'gemini-1.5-flash',
        timeout_seconds: float = 8.0,
        max_retries: int = 1,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries  # extra attempts on top of the first — never unbounded

    def analyze(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        prompt = self._build_prompt(snapshot)
        last_error: AIProviderError = AIProviderError('Gemini request failed')
        for _ in range(self.max_retries + 1):
            try:
                raw_text = self._call_api(prompt)
            except AIProviderInvalidResponse:
                raise  # a malformed API envelope won't fix itself on retry
            except AIProviderError as exc:
                last_error = exc
                continue
            return self._parse_response(raw_text)
        raise last_error

    def _build_prompt(self, snapshot: dict[str, Any]) -> str:
        return PROMPT_TEMPLATE.format(snapshot_json=json.dumps(snapshot, ensure_ascii=False))

    def _call_api(self, prompt: str) -> str:
        url = f'{self.API_URL_TEMPLATE.format(model=self.model)}?key={self.api_key}'
        body = json.dumps({
            'contents': [{'parts': [{'text': prompt}]}],
            'generationConfig': {'temperature': 0.2, 'responseMimeType': 'application/json'},
        }).encode('utf-8')
        request = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise AIProviderRateLimited(f'Gemini rate limited (HTTP {exc.code})') from exc
            raise AIProviderAPIError(f'Gemini API error (HTTP {exc.code})') from exc
        except TimeoutError as exc:
            raise AIProviderTimeout('Gemini request timed out') from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise AIProviderTimeout('Gemini request timed out') from exc
            raise AIProviderAPIError(f'Gemini request failed: {exc.reason}') from exc
        except json.JSONDecodeError as exc:
            raise AIProviderInvalidResponse('Gemini returned a non-JSON HTTP envelope') from exc

        try:
            return payload['candidates'][0]['content']['parts'][0]['text']
        except (KeyError, IndexError, TypeError) as exc:
            raise AIProviderInvalidResponse('Unexpected Gemini response shape') from exc

    def _parse_response(self, raw_text: str) -> dict[str, Any]:
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise AIProviderInvalidResponse(f'Gemini did not return valid JSON: {exc}') from exc
        if not isinstance(data, dict) or not all(field in data for field in REQUIRED_RESPONSE_FIELDS):
            raise AIProviderInvalidResponse('Gemini JSON is missing required fields')
        return data
