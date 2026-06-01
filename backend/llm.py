"""LM Studio (OpenAI-compatible) client + extraction prompt + JSON parser.

Provides:
  - LMClient.extract(text, *, hint, use_thinking) -> (LLMExtraction, used_thinking)
  - Strict JSON schema prompt with retry on parse error
  - Strips <think>...</think> blocks if thinking mode is on
  - Re-runs with thinking=true if quality_score < threshold (when enabled)
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import LLMConfig
from .schemas import LLMExtraction, NamedEntities

log = logging.getLogger("docregistrar.llm")


SYSTEM_PROMPT = """\
You are a meticulous document-cataloguing assistant. You extract structured
metadata from a single document and return ONLY a JSON object that conforms
to the schema below. No prose, no Markdown fences, no comments - JSON only.

You will be given:
  - The file's name and relative path
  - Hints from the file's embedded metadata (if any)
  - A truncated text sample of the document (head + middle + tail)

Schema (every field MUST be present, use empty string/list/0 if unknown):
{
  "title":               string,
  "description":         string,
  "summary":             string,
  "document_date":       string,
  "last_update_date":    string,
  "document_type":       string,
  "language":            string,
  "authors":             [string],
  "version":             string,
  "confidentiality":     string,
  "named_entities": {
    "persons":               [string],
    "organizations":         [string],
    "locations":             [string],
    "dates":                 [string],
    "products_technologies": [string]
  },
  "key_concepts":        [string],
  "key_phrases":         [string],
  "tags":                [string],
  "geographic_scope":    string,
  "industry_domain":     string,
  "quality_score":       number
}

Field rules:
  - "title": Best document title; if missing, derive from filename.
  - "description": ONE concise sentence (120-250 chars max), describing what the
        document IS and what it covers. Distinct from "summary": this is a
        compact tagline, not a detailed summary. NEVER exceed 250 characters.
  - "summary": 1500-2500 chars when content allows, factual, neutral, no marketing fluff.
  - "document_date" / "last_update_date": "YYYY-MM-DD" if a full date is known,
        else "YYYY-MM" if only month/year is known, else "YYYY", else "".
  - "document_type": e.g. "presentation", "white paper", "report", "spreadsheet",
        "policy", "manual", "memo", "proposal", "contract", "specification", "image".
  - "language": e.g. "English", "German".
  - "version": e.g. "1.2", "v3", "Draft 2".
  - "confidentiality": one of "Public", "Internal", "Confidential",
        "Strictly Confidential", or "Unknown". Infer from header/footer markers,
        watermarks, or content. If unsure, return "Unknown".
  - "named_entities.products_technologies": only real products/technologies/standards,
        not generic words. Examples: "SAP BTP", "Kubernetes", "Azure AD".
  - "key_phrases": at most 10 multi-word phrases that capture the document's substance.
  - "tags": 3-10 short categorical labels.
  - "geographic_scope": e.g. "Global", "EMEA", "Germany", "MENA", or "".
  - "industry_domain": e.g. "Banking", "Public Sector", "Pharma", or "".
  - "quality_score": 0.0-1.0, YOUR confidence in the extraction. Lower it if the text
        was very short, garbled, or you had to guess most fields.

Output rules:
  - Return ONE JSON object only. Do not wrap it in code fences or commentary.
  - Use double quotes for all keys and string values.
  - "summary" must be at most 2500 characters.
"""


class LLMError(RuntimeError):
    """Base class for all LLM-extraction failures."""
    pass


class LLMTransportError(LLMError):
    """Network / connection failure talking to the LLM server."""
    pass


class LLMHTTPError(LLMError):
    """LLM server returned a non-2xx HTTP response."""
    def __init__(self, status_code: int, body_snippet: str, base_url: str = "", model: str = ""):
        self.status_code = status_code
        self.body_snippet = body_snippet
        self.base_url = base_url
        self.model = model
        msg = (
            f"LM Studio returned HTTP {status_code} (model={model!r}, url={base_url!r}). "
            f"Body: {body_snippet}"
        )
        super().__init__(msg)


class LLMInvalidJSONError(LLMError):
    """Model returned text that is not valid JSON."""
    def __init__(self, raw_snippet: str):
        self.raw_snippet = raw_snippet
        super().__init__(f"LLM did not return valid JSON. First 200 chars: {raw_snippet!r}")


class LLMSchemaError(LLMError):
    """Model returned valid JSON but it failed Pydantic schema validation."""
    def __init__(self, details: str):
        self.details = details
        super().__init__(f"LLM JSON failed schema validation: {details}")


class LLMCancelled(LLMError):
    """The in-flight LLM call was cancelled (e.g. user pressed Stop)."""
    pass


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


class LMClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self._cancelled = False
        self._client = self._make_client()

    def _make_client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.cfg.base_url,
            timeout=self.cfg.request_timeout_seconds,
            headers={"Authorization": f"Bearer {self.cfg.api_key}"},
        )

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    def cancel(self) -> None:
        """Abort any in-flight request by closing the underlying client.

        Safe to call from any thread. After calling, this client is dead;
        the caller should create a new LMClient if it wants to do more work.
        """
        self._cancelled = True
        try:
            self._client.close()
        except Exception:
            pass

    # ---------- public ----------

    def extract(
        self,
        text: str,
        *,
        file_name: str,
        relative_path: str,
        use_thinking: Optional[bool] = None,
    ) -> tuple[LLMExtraction, bool]:
        """Run extraction. Returns (extraction, used_thinking).

        If `use_thinking` is None, uses cfg.thinking_default. If the resulting
        quality_score is below cfg.low_quality_threshold and
        cfg.thinking_on_low_quality is True, re-runs with thinking ON.
        """
        thinking = self.cfg.thinking_default if use_thinking is None else use_thinking
        result = self._extract_once(
            text=text,
            file_name=file_name,
            relative_path=relative_path,
            use_thinking=thinking,
        )
        if (
            self.cfg.thinking_on_low_quality
            and not thinking
            and result.quality_score < self.cfg.low_quality_threshold
        ):
            log.info(
                "Quality %.2f < %.2f for %s - re-running with thinking ON",
                result.quality_score,
                self.cfg.low_quality_threshold,
                relative_path,
            )
            try:
                result = self._extract_once(
                    text=text,
                    file_name=file_name,
                    relative_path=relative_path,
                    use_thinking=True,
                )
                thinking = True
            except Exception as e:
                log.warning("Thinking-mode rerun failed for %s: %s", relative_path, e)
        return result, thinking

    # ---------- internal ----------

    def _extract_once(
        self,
        *,
        text: str,
        file_name: str,
        relative_path: str,
        use_thinking: bool,
    ) -> LLMExtraction:
        user_prompt = (
            f"File name: {file_name}\n"
            f"Relative path: {relative_path}\n"
            f"--- BEGIN DOCUMENT TEXT ---\n"
            f"{text or '[empty document]'}\n"
            f"--- END DOCUMENT TEXT ---\n"
            f"Now produce the JSON object. JSON only."
        )

        # First attempt
        raw = self._chat(user_prompt, use_thinking=use_thinking)
        try:
            return self._parse(raw)
        except LLMError as e:
            log.warning("First-pass JSON parse failed for %s: %s", relative_path, e)

        # Retry once with explicit corrective instruction
        retry_msg = (
            "Your previous response was not valid JSON or did not match the schema. "
            "Return ONE JSON object only, no prose, no Markdown fences. "
            f"\n\nORIGINAL TASK:\n{user_prompt}"
        )
        raw2 = self._chat(retry_msg, use_thinking=use_thinking)
        return self._parse(raw2)

    @retry(
        reraise=True,
        retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        stop=stop_after_attempt(3),
    )
    def _chat(self, user_msg: str, *, use_thinking: bool) -> str:
        # Qwen3.5 supports a thinking mode. LM Studio's OpenAI-compatible
        # server accepts plain prompt directives like "/think" or "/no_think".
        # Belt-and-braces: put the directive in BOTH the system message AND
        # appended to the end of the user message — some Qwen3 builds in LM
        # Studio only honor it reliably when present at the end of the user
        # turn. We also pass `chat_template_kwargs.enable_thinking` for engines
        # that read it from the chat template (e.g. vLLM); LM Studio currently
        # ignores unknown fields, so this is harmless.
        directive = "/think" if use_thinking else "/no_think"
        user_msg_with_directive = f"{user_msg}\n{directive}"

        # Build a conservative request body. We intentionally OMIT fields that
        # some LM Studio / engine versions reject with HTTP 400:
        #   - presence_penalty
        #   - response_format (json_object)
        # If the user wants them, they can be re-enabled in config.
        body: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": f"{SYSTEM_PROMPT}\n{directive}"},
                {"role": "user", "content": user_msg_with_directive},
            ],
            "temperature": float(self.cfg.temperature),
            "top_p": float(self.cfg.top_p),
            "max_tokens": int(self.cfg.max_output_tokens),
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": bool(use_thinking)},
        }

        try:
            r = self._client.post("/chat/completions", json=body)
        except (httpx.HTTPError, RuntimeError) as e:
            if self._cancelled:
                raise LLMCancelled("cancelled") from e
            # Network/connection errors are LLMTransportError. tenacity may
            # still retry httpx.HTTPError below; final failures bubble up.
            raise LLMTransportError(f"{type(e).__name__}: {e}") from e

        if r.status_code >= 400:
            try:
                err_body = r.json()
                body_str = json.dumps(err_body, ensure_ascii=False)
            except Exception:
                body_str = r.text or ""
            snippet = (body_str or "")[:500]
            log.error(
                "LM Studio %s on %s. Request model=%r. Response: %r",
                r.status_code, r.request.url, self.cfg.model, body_str,
            )
            raise LLMHTTPError(
                status_code=r.status_code,
                body_snippet=snippet,
                base_url=self.cfg.base_url,
                model=self.cfg.model,
            )

        data = r.json()
        try:
            return data["choices"][0]["message"]["content"] or ""
        except Exception as e:
            raise LLMError(f"unexpected LM Studio response shape: {e}; body={data!r}")

    # ---------- parsing ----------

    @staticmethod
    def _strip_thinking(s: str) -> str:
        # Remove <think>...</think> blocks (Qwen3-style)
        return re.sub(r"<think>.*?</think>", "", s, flags=re.DOTALL).strip()

    @staticmethod
    def _extract_json_object(s: str) -> str:
        """Find the outermost JSON object in s and return it as a string."""
        s = re.sub(r"^```(?:json)?\s*", "", s.strip())
        s = re.sub(r"\s*```$", "", s)
        start = s.find("{")
        if start == -1:
            raise LLMError("no JSON object in model response")
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return s[start : i + 1]
        raise LLMError("unterminated JSON object")

    @classmethod
    def _parse(cls, raw: str) -> LLMExtraction:
        if not raw:
            raise LLMInvalidJSONError("(empty response)")
        cleaned = cls._strip_thinking(raw)
        try:
            json_str = cls._extract_json_object(cleaned)
        except LLMError:
            raise LLMInvalidJSONError(cleaned[:200])
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            raise LLMInvalidJSONError(f"{e}: {json_str[:200]}")

        # Defensive cleanup
        if isinstance(data.get("summary"), str) and len(data["summary"]) > 2500:
            data["summary"] = data["summary"][:2500]
        if isinstance(data.get("description"), str) and len(data["description"]) > 250:
            data["description"] = data["description"][:250]
        kp = data.get("key_phrases")
        if isinstance(kp, list) and len(kp) > 10:
            data["key_phrases"] = kp[:10]
        try:
            qs = float(data.get("quality_score", 0))
        except (TypeError, ValueError):
            qs = 0.0
        data["quality_score"] = max(0.0, min(1.0, qs))

        ne_raw = data.pop("named_entities", {}) or {}
        ne = NamedEntities(
            persons=_as_str_list(ne_raw.get("persons")),
            organizations=_as_str_list(ne_raw.get("organizations")),
            locations=_as_str_list(ne_raw.get("locations")),
            dates=_as_str_list(ne_raw.get("dates")),
            products_technologies=_as_str_list(ne_raw.get("products_technologies")),
        )

        # Coerce list-like fields
        list_fields = ["authors", "key_concepts", "key_phrases", "tags"]
        for k in list_fields:
            data[k] = _as_str_list(data.get(k))

        # Coerce string-like fields (use empty string for missing/None)
        str_fields = [
            "title", "description", "summary",
            "document_date", "last_update_date",
            "document_type", "language", "version", "confidentiality",
            "geographic_scope", "industry_domain",
        ]
        for k in str_fields:
            v = data.get(k)
            data[k] = "" if v is None else str(v).strip()

        try:
            return LLMExtraction(named_entities=ne, **data)
        except Exception as e:
            raise LLMSchemaError(f"{type(e).__name__}: {e}")
