"""LM Studio (OpenAI-compatible) client + extraction prompt + JSON parser.

Supports two extraction strategies:
  1. Single-shot (legacy): one LLM call on a head+middle+tail sample.
     Used when extracted text <= mapreduce.threshold_chars or mapreduce
     is disabled.
  2. Map-reduce: split the FULL extracted text into chunks, extract a
     partial JSON per chunk, deterministically merge entity/list fields,
     then optionally do a final LLM "reduce" call to consolidate the
     narrative fields (title/description/summary/...).
"""
from __future__ import annotations

import base64
import json
import logging
import re
from collections import Counter
from typing import Any, Callable, Optional, Union

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import LLMConfig, MapReduceConfig
from .extractors import ExtractedImage
from .schemas import LLMExtraction, NamedEntities

log = logging.getLogger("docregistrar.llm")


# Progress callback used by extract() to report chunk-level progress.
# Called as cb(unit, current, total). May raise LLMCancelled to abort.
ChunkProgressCB = Optional[Callable[[str, int, int], None]]


# --------------- prompt constants ---------------

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
        document IS and what it covers. NEVER exceed 250 characters.
  - "summary": 1500-2500 chars when content allows, factual, neutral, no marketing fluff.
  - "document_date" / "last_update_date": "YYYY-MM-DD" if a full date is known,
        else "YYYY-MM" if only month/year is known, else "YYYY", else "".
  - "document_type": e.g. "presentation", "white paper", "report", "spreadsheet",
        "policy", "manual", "memo", "proposal", "contract", "specification", "image".
  - "language": e.g. "English", "German".
  - "version": e.g. "1.2", "v3", "Draft 2".
  - "confidentiality": one of "Public", "Internal", "Confidential",
        "Strictly Confidential", or "Unknown".
  - "named_entities.products_technologies": only real products/technologies/standards.
  - "key_phrases": at most 10 multi-word phrases.
  - "tags": 3-10 short categorical labels.
  - "geographic_scope": e.g. "Global", "EMEA", "Germany", "MENA", or "".
  - "industry_domain": e.g. "Banking", "Public Sector", "Pharma", or "".
  - "quality_score": 0.0-1.0, YOUR confidence in the extraction.

Output rules:
  - Return ONE JSON object only. No code fences, no commentary.
  - Use double quotes for all keys and string values.
  - "summary" must be at most 2500 characters.
"""


CHUNK_SYSTEM_PROMPT = """\
You are a meticulous document-cataloguing assistant. You will be shown ONE
CHUNK (a contiguous slice) of a longer document, with metadata about which
chunk this is (e.g. "chunk 4 of 12"). Extract whatever structured metadata
you can see IN THIS CHUNK ONLY. Return ONLY a JSON object - no prose, no
Markdown fences.

The schema is identical to the full-document schema (title, description,
summary, document_date, last_update_date, document_type, language, authors,
version, confidentiality, named_entities {persons, organizations, locations,
dates, products_technologies}, key_concepts, key_phrases, tags,
geographic_scope, industry_domain, quality_score).

CHUNK-SPECIFIC RULES (very important):
  - You are seeing ONLY a slice of the document. If a field is NOT clearly
    visible in THIS chunk, leave it empty (string -> "", list -> [],
    number -> 0). DO NOT guess or invent values.
  - "title": ONLY fill if you see an explicit document title in this chunk.
  - "description" / "summary": describe ONLY content visible in this chunk.
    Keep them short (description <= 250 chars, summary <= 800 chars).
  - "document_date" / "last_update_date": only fill if explicitly visible
    in this chunk.
  - "named_entities": list every person/organization/location/date/product
    that genuinely appears in THIS chunk. No generic words.
  - "confidentiality": only fill if a marker is visible in this chunk.
  - "key_phrases" / "key_concepts" / "tags": at most 5 each per chunk.
  - "quality_score": your confidence in THIS chunk's extraction.

Output: ONE JSON object only.
"""


REDUCE_SYSTEM_PROMPT = """\
You are a meticulous document-cataloguing assistant. You will be given a list
of partial extractions, each from a different chunk of the SAME document,
plus a deterministically-merged "structural" view (entities, dates, lists).
Produce the FINAL consolidated metadata for the whole document. Return ONLY
a JSON object - no prose, no Markdown fences.

The schema is identical to the per-chunk schema.

REDUCE RULES (very important):
  - DO NOT INVENT entities or facts. The "named_entities" lists you produce
    MUST be a subset of the deterministic merged entities provided. You may
    dedupe near-duplicates (e.g. "SAP SE" / "SAP") and pick the canonical
    form, but do not add anything not in the input.
  - "title": pick the best title from the partials, OR derive a short title
    from the filename if all partials are empty.
  - "description": ONE concise sentence (120-250 chars max), about the WHOLE
    document. NEVER exceed 250 characters.
  - "summary": 1500-2500 chars, factual, neutral, derived from partials. Do
    not introduce new facts.
  - "document_date" / "last_update_date": pick the most plausible date from
    partials and the merged dates list.
  - "document_type" / "language" / "geographic_scope" / "industry_domain":
    pick the most-frequently-occurring non-empty value from partials.
  - "confidentiality": if any partial reports a marker, use the strictest
    seen ("Strictly Confidential" > "Confidential" > "Internal" > "Public").
    Otherwise "Unknown".
  - "key_phrases": at most 10 multi-word phrases, deduped from partials.
  - "tags": 3-10 short categorical labels, deduped from partials.
  - "quality_score": your confidence in the FINAL consolidated extraction.

Output: ONE JSON object only.
"""


# --------------- exception types ---------------

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
            "LM Studio returned HTTP " + str(status_code)
            + " (model=" + repr(model) + ", url=" + repr(base_url) + "). "
            + "Body: " + (body_snippet or "")
        )
        super().__init__(msg)


class LLMInvalidJSONError(LLMError):
    """Model returned text that is not valid JSON."""
    def __init__(self, raw_snippet: str):
        self.raw_snippet = raw_snippet
        super().__init__("LLM did not return valid JSON. First 200 chars: " + repr(raw_snippet))


class LLMSchemaError(LLMError):
    """Model returned valid JSON but it failed Pydantic schema validation."""
    def __init__(self, details: str):
        self.details = details
        super().__init__("LLM JSON failed schema validation: " + details)


class LLMCancelled(LLMError):
    """The in-flight LLM call was cancelled (e.g. user pressed Stop)."""
    pass


# --------------- helpers ---------------

def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def _dedup_preserve_order(items: list[str], cap: Optional[int] = None) -> list[str]:
    """Case-insensitive dedupe preserving first-seen order. Optionally cap."""
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if not it:
            continue
        key = it.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(it.strip())
        if cap is not None and len(out) >= cap:
            break
    return out


def _most_common_nonempty(values: list[str]) -> str:
    """Return the most frequent non-empty value (case-insensitive count,
    but original casing preserved); ties broken by first occurrence."""
    cleaned = [v.strip() for v in values if v and v.strip()]
    if not cleaned:
        return ""
    counts: Counter = Counter()
    first_seen: dict[str, str] = {}
    for v in cleaned:
        k = v.lower()
        counts[k] += 1
        if k not in first_seen:
            first_seen[k] = v
    best_key = max(counts.keys(), key=lambda k: (counts[k], -list(first_seen.keys()).index(k)))
    return first_seen[best_key]


_CONFIDENTIALITY_RANK = {
    "strictly confidential": 4,
    "confidential": 3,
    "internal": 2,
    "public": 1,
    "unknown": 0,
    "": 0,
}


def _strictest_confidentiality(values: list[str]) -> str:
    """Return the strictest non-empty confidentiality marker seen."""
    best = ""
    best_rank = -1
    for v in values:
        key = (v or "").strip().lower()
        rank = _CONFIDENTIALITY_RANK.get(key, -1)
        if rank > best_rank and key:
            best_rank = rank
            best = v.strip()
    return best


def _pick_date(values: list[str], prefer_earliest: bool = True) -> str:
    """Pick the most plausible date string from a list of partials.

    Strategy: take the most frequently occurring non-empty value; on a tie,
    prefer earliest (for document_date) or latest (for last_update_date).
    """
    cleaned = [v.strip() for v in values if v and v.strip()]
    if not cleaned:
        return ""
    counts: Counter = Counter()
    for v in cleaned:
        counts[v] += 1
    max_count = max(counts.values())
    top = [v for v, c in counts.items() if c == max_count]
    if len(top) == 1:
        return top[0]
    return min(top) if prefer_earliest else max(top)


# --------------- chunking ---------------

# Marker patterns inserted by extractors at semantic boundaries.
_PAGE_MARKER_RE = re.compile(r"(?m)^---\s+(?:Page|Slide|Sheet:)\b.*?---\s*$")


def split_text_into_chunks(text: str, cfg: MapReduceConfig) -> list[str]:
    """Split text into chunks of ~cfg.chunk_chars characters, preferring
    natural boundaries (page/slide/sheet markers, blank lines).

    If the result has more chunks than cfg.max_chunks, sample chunks
    uniformly across the document so the LLM still sees coverage from
    start to end.
    """
    text = text or ""
    target = max(1000, int(cfg.chunk_chars))
    overlap = max(0, int(cfg.chunk_overlap_chars))
    if overlap >= target:
        overlap = max(0, target // 4)

    if not text:
        return []
    if len(text) <= target:
        return [text]

    # Prefer page/slide markers as cut points when present.
    cut_points = [m.start() for m in _PAGE_MARKER_RE.finditer(text)]
    chunks: list[str] = []

    if cut_points:
        # Greedy pack: accumulate sections (between consecutive markers)
        # until the next addition would exceed `target`.
        boundaries = cut_points + [len(text)]
        start = 0
        cur_start = 0
        i = 0
        while i < len(boundaries):
            end = boundaries[i]
            if end - cur_start >= target or i == len(boundaries) - 1:
                # Emit chunk from cur_start to end (or up to last boundary
                # within range).
                chunk_end = end if i == len(boundaries) - 1 else end
                chunk = text[cur_start:chunk_end]
                if chunk.strip():
                    chunks.append(chunk)
                if i == len(boundaries) - 1:
                    break
                # Set up next chunk start with overlap from previous tail.
                ov = max(0, overlap)
                cur_start = max(chunk_end - ov, chunk_end)
            i += 1
        if not chunks:
            chunks = [text]
    else:
        # No markers - split on paragraph boundaries within the target window.
        pos = 0
        n = len(text)
        while pos < n:
            end = min(pos + target, n)
            if end < n:
                # Try to back off to a paragraph break inside the last 20% of
                # the chunk.
                lookback_start = pos + int(target * 0.8)
                back = text.rfind("\n\n", lookback_start, end)
                if back > pos:
                    end = back
                else:
                    back2 = text.rfind("\n", lookback_start, end)
                    if back2 > pos:
                        end = back2
            chunk = text[pos:end]
            if chunk.strip():
                chunks.append(chunk)
            if end >= n:
                break
            pos = max(end - overlap, end)

    # Cap the number of chunks. If exceeded, sample uniformly.
    max_chunks = max(1, int(cfg.max_chunks))
    if len(chunks) > max_chunks:
        log.warning(
            "Chunk count %d exceeded max_chunks %d; sampling uniformly.",
            len(chunks), max_chunks,
        )
        step = len(chunks) / max_chunks
        sampled = [chunks[min(int(i * step), len(chunks) - 1)] for i in range(max_chunks)]
        chunks = sampled

    return chunks


# --------------- deterministic merge ---------------

def merge_partials_deterministic(partials: list[LLMExtraction]) -> LLMExtraction:
    """Merge per-chunk partial extractions into a single extraction using
    deterministic rules. Narrative fields (title/description/summary) are
    populated with conservative fallbacks; the LLM reduce step (if enabled)
    will overwrite them with a coherent narrative.
    """
    if not partials:
        return LLMExtraction()

    # Lists: union + dedup
    all_persons: list[str] = []
    all_orgs: list[str] = []
    all_locs: list[str] = []
    all_dates: list[str] = []
    all_products: list[str] = []
    all_authors: list[str] = []
    all_keyconcepts: list[str] = []
    all_keyphrases: list[str] = []
    all_tags: list[str] = []

    for p in partials:
        ne = p.named_entities
        all_persons.extend(ne.persons)
        all_orgs.extend(ne.organizations)
        all_locs.extend(ne.locations)
        all_dates.extend(ne.dates)
        all_products.extend(ne.products_technologies)
        all_authors.extend(p.authors)
        all_keyconcepts.extend(p.key_concepts)
        all_keyphrases.extend(p.key_phrases)
        all_tags.extend(p.tags)

    persons = _dedup_preserve_order(all_persons)
    orgs = _dedup_preserve_order(all_orgs)
    locs = _dedup_preserve_order(all_locs)
    dates = _dedup_preserve_order(all_dates)
    products = _dedup_preserve_order(all_products)
    authors = _dedup_preserve_order(all_authors)
    key_concepts = _dedup_preserve_order(all_keyconcepts, cap=20)
    key_phrases = _dedup_preserve_order(all_keyphrases, cap=10)
    tags = _dedup_preserve_order(all_tags, cap=10)

    # Scalar fields: pick most-common non-empty
    document_type = _most_common_nonempty([p.document_type for p in partials])
    language = _most_common_nonempty([p.language for p in partials])
    geo_scope = _most_common_nonempty([p.geographic_scope for p in partials])
    industry = _most_common_nonempty([p.industry_domain for p in partials])
    version = _most_common_nonempty([p.version for p in partials])
    confidentiality = _strictest_confidentiality([p.confidentiality for p in partials])

    document_date = _pick_date([p.document_date for p in partials], prefer_earliest=True)
    last_update_date = _pick_date([p.last_update_date for p in partials], prefer_earliest=False)

    # Title: longest non-empty (proxy for "most informative")
    titles = [p.title.strip() for p in partials if p.title and p.title.strip()]
    title = max(titles, key=len) if titles else ""

    # Description: first non-empty (will be replaced by reduce LLM if enabled)
    descriptions = [p.description.strip() for p in partials if p.description and p.description.strip()]
    description = descriptions[0] if descriptions else ""

    # Summary fallback: concatenate top partial summaries up to 2500 chars
    summaries = [p.summary.strip() for p in partials if p.summary and p.summary.strip()]
    summary = ""
    for s in summaries:
        if len(summary) + len(s) + 2 > 2500:
            break
        summary = (summary + "\n\n" + s).strip() if summary else s
    summary = summary[:2500]

    return LLMExtraction(
        title=title,
        description=description[:250],
        summary=summary,
        document_date=document_date,
        last_update_date=last_update_date,
        document_type=document_type,
        language=language,
        authors=authors,
        version=version,
        confidentiality=confidentiality,
        named_entities=NamedEntities(
            persons=persons,
            organizations=orgs,
            locations=locs,
            dates=dates,
            products_technologies=products,
        ),
        key_concepts=key_concepts,
        key_phrases=key_phrases,
        tags=tags,
        geographic_scope=geo_scope,
        industry_domain=industry,
    )


# --------------- LMClient ---------------

class LMClient:
    def __init__(self, cfg: LLMConfig, mapreduce_cfg: Optional[MapReduceConfig] = None):
        self.cfg = cfg
        self.mapreduce_cfg = mapreduce_cfg or MapReduceConfig()
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
        progress_cb: ChunkProgressCB = None,
        image: Optional[ExtractedImage] = None,
    ) -> tuple[LLMExtraction, bool]:
        """Run extraction. Returns (extraction, used_thinking).

        Routes to either the single-shot fast path (small docs / images)
        or the map-reduce path (large docs), based on cfg.mapreduce.

        When `image` is provided AND `cfg.vision.enabled` is True, the
        image is attached as a multi-modal content part on the
        single-shot user message; map-reduce is bypassed (the text hint
        is always tiny and shouldn't trigger chunking, but we force the
        single-shot path defensively).

        `progress_cb(unit, current, total)` is invoked for chunk-level
        progress; only called for the map-reduce path. May raise to
        cancel.
        """
        thinking = self.cfg.thinking_default if use_thinking is None else use_thinking
        mr = self.mapreduce_cfg
        text = text or ""

        # When an image is attached we always run single-shot; map-reduce
        # over text hints would just split filename/EXIF lines pointlessly
        # and lose the image attachment on the chunk calls.
        has_image = image is not None and bool(self.cfg.vision.enabled)
        use_mapreduce = (
            bool(mr.enabled)
            and len(text) > int(mr.threshold_chars)
            and not has_image
        )

        if has_image:
            log.info(
                "LLM vision call: file=%s mime=%s dims=%dx%d bytes=%d "
                "vision_model=%r text_model=%r include_text_hints=%s "
                "detail=%s",
                relative_path,
                image.mime, image.width, image.height, len(image.data),
                self.cfg.vision.model or "(reuse llm.model)",
                self.cfg.model,
                self.cfg.vision.include_text_hints,
                self.cfg.vision.detail,
            )

        if not use_mapreduce:
            result = self._extract_single_shot(
                text=text,
                file_name=file_name,
                relative_path=relative_path,
                use_thinking=thinking,
                image=image if has_image else None,
            )
            # Mirror legacy quality_score into min/avg
            result.quality_score_min = result.quality_score
            result.quality_score_avg = result.quality_score

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
                    result = self._extract_single_shot(
                        text=text,
                        file_name=file_name,
                        relative_path=relative_path,
                        use_thinking=True,
                        image=image if has_image else None,
                    )
                    result.quality_score_min = result.quality_score
                    result.quality_score_avg = result.quality_score
                    thinking = True
                except Exception as e:
                    log.warning("Thinking-mode rerun failed for %s: %s", relative_path, e)
            return result, thinking

        # Map-reduce path
        result = self._extract_mapreduce(
            text=text,
            file_name=file_name,
            relative_path=relative_path,
            use_thinking=thinking,
            progress_cb=progress_cb,
        )

        if (
            self.cfg.thinking_on_low_quality
            and not thinking
            and result.quality_score < self.cfg.low_quality_threshold
        ):
            log.info(
                "Map-reduce quality %.2f < %.2f for %s - re-running with thinking ON",
                result.quality_score,
                self.cfg.low_quality_threshold,
                relative_path,
            )
            try:
                result = self._extract_mapreduce(
                    text=text,
                    file_name=file_name,
                    relative_path=relative_path,
                    use_thinking=True,
                    progress_cb=progress_cb,
                )
                thinking = True
            except Exception as e:
                log.warning("Thinking-mode rerun (mapreduce) failed for %s: %s", relative_path, e)

        return result, thinking

    # ---------- internal: single-shot ----------

    def _extract_single_shot(
        self,
        *,
        text: str,
        file_name: str,
        relative_path: str,
        use_thinking: bool,
        image: Optional[ExtractedImage] = None,
    ) -> LLMExtraction:
        # If we have an image AND vision is enabled, attach it to the
        # user message as a multi-modal content part. The text body is
        # the (small) hint blob built by the image extractor; the model
        # sees both.
        vision_active = image is not None and bool(self.cfg.vision.enabled)

        if vision_active and self.cfg.vision.include_text_hints:
            text_block = (
                f"File name: {file_name}\n"
                f"Relative path: {relative_path}\n"
                f"--- BEGIN IMAGE METADATA ---\n"
                f"{text or '[no metadata]'}\n"
                f"--- END IMAGE METADATA ---\n"
                f"An image is attached. Look at it directly. Use the file "
                f"name and EXIF metadata only as supporting hints. Set "
                f"\"document_type\" to a specific image category when "
                f"possible (e.g. \"photograph\", \"screenshot\", "
                f"\"diagram\", \"scan\", \"chart\"). Fill \"description\" "
                f"and \"summary\" based on what you actually see in the "
                f"image.\n"
                f"Now produce the JSON object. JSON only."
            )
        elif vision_active:
            # No text hints requested.
            text_block = (
                f"File name: {file_name}\n"
                f"Relative path: {relative_path}\n"
                f"An image is attached. Look at it directly. Set "
                f"\"document_type\" to a specific image category when "
                f"possible (e.g. \"photograph\", \"screenshot\", "
                f"\"diagram\", \"scan\", \"chart\").\n"
                f"Now produce the JSON object. JSON only."
            )
        else:
            # Legacy text-only behaviour.
            text_block = (
                f"File name: {file_name}\n"
                f"Relative path: {relative_path}\n"
                f"--- BEGIN DOCUMENT TEXT ---\n"
                f"{text or '[empty document]'}\n"
                f"--- END DOCUMENT TEXT ---\n"
                f"Now produce the JSON object. JSON only."
            )

        # Build the (text, image) pair we'll hand to _chat. _chat decides
        # whether to send a string or a multi-modal list based on the
        # image argument.
        attached = image if vision_active else None

        # First attempt
        try:
            raw = self._chat(
                SYSTEM_PROMPT, text_block,
                use_thinking=use_thinking,
                max_tokens=self.cfg.max_output_tokens,
                image=attached,
            )
        except LLMHTTPError as e:
            # Vision-specific safety net: if the configured (vision) model
            # rejects the multi-modal request with a 4xx, fall back once
            # to a text-only call so the file at least gets some metadata.
            # 5xx is left to the normal retry path.
            if (
                attached is not None
                and 400 <= e.status_code < 500
                and self.cfg.vision.fallback_to_text_on_error
            ):
                log.warning(
                    "Vision call failed with HTTP %d for %s "
                    "(model=%r, body=%s); retrying text-only.",
                    e.status_code, relative_path,
                    self.cfg.vision.model or self.cfg.model,
                    e.body_snippet[:200],
                )
                # Drop the image, keep the text hints.
                raw = self._chat(
                    SYSTEM_PROMPT, text_block,
                    use_thinking=use_thinking,
                    max_tokens=self.cfg.max_output_tokens,
                    image=None,
                )
                log.info(
                    "Vision fallback: text-only call succeeded for %s.",
                    relative_path,
                )
            else:
                raise
        try:
            return self._parse(raw)
        except LLMError as e:
            log.warning("First-pass JSON parse failed for %s: %s", relative_path, e)

        # Retry once with explicit corrective instruction. Re-attach the
        # image if we had one (and vision is still enabled); the parse
        # failure was about the JSON shape, not the image.
        retry_msg = (
            "Your previous response was not valid JSON or did not match the schema. "
            "Return ONE JSON object only, no prose, no Markdown fences. "
            f"\n\nORIGINAL TASK:\n{text_block}"
        )
        raw2 = self._chat(
            SYSTEM_PROMPT, retry_msg,
            use_thinking=use_thinking,
            max_tokens=self.cfg.max_output_tokens,
            image=attached,
        )
        return self._parse(raw2)

    # ---------- internal: map-reduce ----------

    def _extract_mapreduce(
        self,
        *,
        text: str,
        file_name: str,
        relative_path: str,
        use_thinking: bool,
        progress_cb: ChunkProgressCB = None,
    ) -> LLMExtraction:
        chunks = split_text_into_chunks(text, self.mapreduce_cfg)
        total = len(chunks)
        log.info("Map-reduce: %s -> %d chunks", relative_path, total)

        # Notify the UI that we're entering the map phase.
        if progress_cb is not None:
            try:
                progress_cb("chunk", 0, total)
            except LLMCancelled:
                raise

        partials: list[LLMExtraction] = []
        for i, chunk in enumerate(chunks, 1):
            if self._cancelled:
                raise LLMCancelled("cancelled mid-mapreduce")
            partial = self._extract_chunk_once(
                chunk_text=chunk,
                chunk_index=i,
                total_chunks=total,
                file_name=file_name,
                relative_path=relative_path,
                use_thinking=use_thinking,
            )
            partials.append(partial)
            if progress_cb is not None:
                try:
                    progress_cb("chunk", i, total)
                except LLMCancelled:
                    raise

        # Deterministic merge of structural / list fields.
        merged = merge_partials_deterministic(partials)

        # Quality scores: compute min and average across all chunks that
        # produced any signal (i.e. quality_score > 0).
        scored = [p.quality_score for p in partials if p.quality_score > 0]
        if scored:
            merged.quality_score_min = min(scored)
            merged.quality_score_avg = sum(scored) / len(scored)
        else:
            merged.quality_score_min = 0.0
            merged.quality_score_avg = 0.0
        # Legacy quality_score field == min (so existing UI/Excel work).
        merged.quality_score = merged.quality_score_min

        # Optional final reduce LLM call to consolidate narrative fields.
        if self.mapreduce_cfg.reduce_with_llm and partials:
            if self._cancelled:
                raise LLMCancelled("cancelled before reduce")
            if progress_cb is not None:
                try:
                    progress_cb("reduce", 0, 1)
                except LLMCancelled:
                    raise
            try:
                reduced = self._reduce_narrative(
                    partials=partials,
                    deterministic_merged=merged,
                    file_name=file_name,
                    relative_path=relative_path,
                    use_thinking=use_thinking,
                )
                # Overlay narrative fields from reduce; keep deterministic
                # entity/list fields (so the LLM cannot invent new
                # entities).
                merged.title = reduced.title or merged.title
                merged.description = (reduced.description or merged.description)[:250]
                merged.summary = (reduced.summary or merged.summary)[:2500]
                if reduced.document_type:
                    merged.document_type = reduced.document_type
                if reduced.language:
                    merged.language = reduced.language
                if reduced.geographic_scope:
                    merged.geographic_scope = reduced.geographic_scope
                if reduced.industry_domain:
                    merged.industry_domain = reduced.industry_domain
                if reduced.version:
                    merged.version = reduced.version
                if reduced.confidentiality:
                    merged.confidentiality = reduced.confidentiality
                if reduced.document_date:
                    merged.document_date = reduced.document_date
                if reduced.last_update_date:
                    merged.last_update_date = reduced.last_update_date
                # Reduce can dampen quality if it had to guess a lot.
                if 0 < reduced.quality_score < merged.quality_score_min:
                    merged.quality_score_min = reduced.quality_score
                    merged.quality_score = reduced.quality_score
            except LLMCancelled:
                raise
            except Exception as e:
                log.warning(
                    "Reduce step failed for %s; keeping deterministic merge. (%s)",
                    relative_path, e,
                )
            if progress_cb is not None:
                try:
                    progress_cb("reduce", 1, 1)
                except LLMCancelled:
                    raise

        return merged

    def _extract_chunk_once(
        self,
        *,
        chunk_text: str,
        chunk_index: int,
        total_chunks: int,
        file_name: str,
        relative_path: str,
        use_thinking: bool,
    ) -> LLMExtraction:
        user_prompt = (
            f"File name: {file_name}\n"
            f"Relative path: {relative_path}\n"
            f"This is chunk {chunk_index} of {total_chunks}.\n"
            f"--- BEGIN CHUNK TEXT ---\n"
            f"{chunk_text or '[empty chunk]'}\n"
            f"--- END CHUNK TEXT ---\n"
            f"Now produce the JSON object describing only what is visible in this chunk. JSON only."
        )
        max_tokens = max(512, int(self.mapreduce_cfg.per_chunk_max_output_tokens))

        try:
            raw = self._chat(CHUNK_SYSTEM_PROMPT, user_prompt,
                             use_thinking=use_thinking, max_tokens=max_tokens)
            return self._parse(raw)
        except LLMCancelled:
            raise
        except (LLMInvalidJSONError, LLMSchemaError) as e:
            # On a parse/schema failure for ONE chunk, retry once with a
            # corrective instruction. If it fails again, fall back to an
            # empty extraction for this chunk (don't poison the whole file).
            log.warning(
                "Chunk %d/%d JSON parse failed for %s: %s; retrying once.",
                chunk_index, total_chunks, relative_path, e,
            )
            retry_msg = (
                "Your previous response was not valid JSON or did not match the schema. "
                "Return ONE JSON object only, no prose, no Markdown fences. "
                f"\n\nORIGINAL TASK:\n{user_prompt}"
            )
            try:
                raw2 = self._chat(CHUNK_SYSTEM_PROMPT, retry_msg,
                                  use_thinking=use_thinking, max_tokens=max_tokens)
                return self._parse(raw2)
            except LLMCancelled:
                raise
            except Exception as e2:
                log.warning(
                    "Chunk %d/%d retry failed for %s: %s; using empty partial.",
                    chunk_index, total_chunks, relative_path, e2,
                )
                return LLMExtraction()

    def _reduce_narrative(
        self,
        *,
        partials: list[LLMExtraction],
        deterministic_merged: LLMExtraction,
        file_name: str,
        relative_path: str,
        use_thinking: bool,
    ) -> LLMExtraction:
        """Run the final reduce LLM call. Receives compact summaries of each
        partial extraction (just the narrative-relevant fields) plus the
        deterministic merge as a guard rail. Returns a parsed LLMExtraction;
        on any failure, the caller falls back to the deterministic merge.
        """
        # Build a compact summary of each partial: only the fields useful for
        # consolidating the narrative. We deliberately omit long entity lists
        # because the deterministic merged view already has them.
        partial_briefs: list[dict] = []
        for i, p in enumerate(partials, 1):
            partial_briefs.append({
                "chunk": i,
                "title": p.title or "",
                "description": p.description or "",
                "summary": p.summary or "",
                "document_type": p.document_type or "",
                "language": p.language or "",
                "document_date": p.document_date or "",
                "last_update_date": p.last_update_date or "",
                "version": p.version or "",
                "confidentiality": p.confidentiality or "",
                "geographic_scope": p.geographic_scope or "",
                "industry_domain": p.industry_domain or "",
                "quality_score": round(p.quality_score, 3),
            })

        det = deterministic_merged
        det_view = {
            "named_entities": {
                "persons": det.named_entities.persons,
                "organizations": det.named_entities.organizations,
                "locations": det.named_entities.locations,
                "dates": det.named_entities.dates,
                "products_technologies": det.named_entities.products_technologies,
            },
            "authors": det.authors,
            "key_concepts": det.key_concepts,
            "key_phrases": det.key_phrases,
            "tags": det.tags,
        }

        user_prompt = (
            f"File name: {file_name}\n"
            f"Relative path: {relative_path}\n"
            f"Total chunks: {len(partials)}\n\n"
            "Per-chunk partial extractions (narrative fields only):\n"
            + json.dumps(partial_briefs, ensure_ascii=False, indent=2)
            + "\n\nDeterministically merged structural fields (DO NOT add anything not "
              "already in here for named_entities / authors / key_concepts / "
              "key_phrases / tags):\n"
            + json.dumps(det_view, ensure_ascii=False, indent=2)
            + "\n\nNow produce the FINAL consolidated JSON object. JSON only."
        )

        max_tokens = int(self.cfg.max_output_tokens)
        raw = self._chat(REDUCE_SYSTEM_PROMPT, user_prompt,
                         use_thinking=use_thinking, max_tokens=max_tokens)
        try:
            return self._parse(raw)
        except LLMError as e:
            # One retry with corrective instruction; otherwise let the caller
            # fall back to the deterministic merge.
            log.warning("Reduce-pass JSON parse failed for %s: %s", relative_path, e)
            retry_msg = (
                "Your previous response was not valid JSON or did not match the schema. "
                "Return ONE JSON object only, no prose, no Markdown fences. "
                f"\n\nORIGINAL TASK:\n{user_prompt}"
            )
            raw2 = self._chat(REDUCE_SYSTEM_PROMPT, retry_msg,
                              use_thinking=use_thinking, max_tokens=max_tokens)
            return self._parse(raw2)

    # ---------- low-level chat ----------

    @retry(
        reraise=True,
        retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        stop=stop_after_attempt(3),
    )
    def _chat(self, system_prompt: str, user_msg: str, *,
              use_thinking: bool, max_tokens: int,
              image: Optional[ExtractedImage] = None) -> str:
        # Qwen3.5 supports a thinking mode. LM Studio's OpenAI-compatible
        # server accepts plain prompt directives like "/think" or "/no_think".
        # Belt-and-braces: put the directive in BOTH the system message AND
        # appended to the end of the user message - some Qwen3 builds in LM
        # Studio only honor it reliably when present at the end of the user
        # turn. We also pass `chat_template_kwargs.enable_thinking` for engines
        # that read it from the chat template (e.g. vLLM); LM Studio currently
        # ignores unknown fields, so this is harmless.
        directive = "/think" if use_thinking else "/no_think"
        user_msg_with_directive = f"{user_msg}\n{directive}"

        # If an image is attached, build OpenAI-style multi-modal user
        # content (a list of typed parts). Otherwise the user content is
        # a plain string (legacy behaviour, identical to before).
        user_content: Union[str, list[dict[str, Any]]]
        model_to_use = self.cfg.model
        if image is not None:
            try:
                b64 = base64.b64encode(image.data).decode("ascii")
            except Exception as e:
                # Defense in depth: if base64 somehow fails, fall back to
                # text-only and log loudly.
                log.error(
                    "base64-encode of image failed (%s: %s); "
                    "falling back to text-only.",
                    type(e).__name__, e,
                )
                user_content = user_msg_with_directive
            else:
                data_url = f"data:{image.mime};base64,{b64}"
                detail = (self.cfg.vision.detail or "auto").lower()
                if detail not in ("auto", "low", "high"):
                    detail = "auto"
                user_content = [
                    {"type": "text", "text": user_msg_with_directive},
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url, "detail": detail},
                    },
                ]
                # Use the dedicated vision model when configured, else
                # reuse the default text model.
                if self.cfg.vision.model:
                    model_to_use = self.cfg.vision.model
                log.debug(
                    "Vision payload: model=%r prompt_chars=%d "
                    "image_b64_chars=%d mime=%s detail=%s",
                    model_to_use, len(user_msg_with_directive),
                    len(b64), image.mime, detail,
                )
        else:
            user_content = user_msg_with_directive

        body: dict[str, Any] = {
            "model": model_to_use,
            "messages": [
                {"role": "system", "content": f"{system_prompt}\n{directive}"},
                {"role": "user", "content": user_content},
            ],
            "temperature": float(self.cfg.temperature),
            "top_p": float(self.cfg.top_p),
            "max_tokens": int(max_tokens),
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": bool(use_thinking)},
        }

        try:
            r = self._client.post("/chat/completions", json=body)
        except (httpx.HTTPError, RuntimeError) as e:
            if self._cancelled:
                raise LLMCancelled("cancelled") from e
            raise LLMTransportError(f"{type(e).__name__}: {e}") from e

        if r.status_code >= 400:
            try:
                err_body = r.json()
                body_str = json.dumps(err_body, ensure_ascii=False)
            except Exception:
                body_str = r.text or ""
            snippet = (body_str or "")[:500]
            log.error(
                "LM Studio %s on %s. Request model=%r (vision=%s). Response: %r",
                r.status_code, r.request.url, model_to_use,
                "yes" if image is not None else "no", body_str,
            )
            raise LLMHTTPError(
                status_code=r.status_code,
                body_snippet=snippet,
                base_url=self.cfg.base_url,
                model=model_to_use,
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
                    return s[start: i + 1]
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

        # Drop any extra keys that are not part of LLMExtraction so the
        # constructor does not error on stray fields the model might have
        # invented.
        allowed = {
            "title", "description", "summary",
            "document_date", "last_update_date",
            "document_type", "language", "authors", "version",
            "confidentiality",
            "key_concepts", "key_phrases", "tags",
            "geographic_scope", "industry_domain",
            "quality_score",
        }
        data = {k: v for k, v in data.items() if k in allowed}

        try:
            return LLMExtraction(named_entities=ne, **data)
        except Exception as e:
            raise LLMSchemaError(f"{type(e).__name__}: {e}")
