import os
import unicodedata
from collections import OrderedDict
from typing import Optional, Sequence
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Form, Request
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from trends import trend_boost
from .parts_lookup import search_parts_catalog as _catalog_lookup
from langchain_pinecone import Pinecone
from langchain.chains import RetrievalQA
from langchain_core.prompts import PromptTemplate
import logging
import platform
import json
import random
import hmac
import hashlib
import base64
try:
    # Debug: write module file path so we can confirm which 'main.py' uvicorn loads
    _import_path = os.path.abspath(__file__)
    _import_dir = os.path.dirname(_import_path)
    with open(os.path.join(_import_dir, 'import_path.txt'), 'w') as _f:
        _f.write(_import_path)
except Exception:
    pass
import re
import pickle
import time
from urllib.parse import urlparse
import subprocess
from datetime import datetime
import threading

# ... (Configuración inicial y carga de dotenv) ...
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Carga .env y, si existe, sobrescribe con secrets.local.txt (compat con nombre anterior)
try:
    load_dotenv(override=False)
    base_dir = os.path.dirname(__file__)
    candidates = [
        "secrets.local.txt",
        os.path.join(base_dir, "secrets.local.txt"),
        "secrets.loca.txt",  # compatibilidad legado
        os.path.join(base_dir, "secrets.loca.txt"),
    ]
    for cand in candidates:
        if os.path.exists(cand):
            load_dotenv(cand, override=True)
            break
except Exception:
    load_dotenv(override=True)
app = FastAPI(title="Higer RAG API")

# CORS (configurable)
_cors = (os.getenv('CORS_ORIGINS') or '').strip()
if _cors:
    if _cors == '*':
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    else:
        origins = [o.strip() for o in _cors.split(',') if o.strip()]
        if origins:
            app.add_middleware(
                CORSMiddleware,
                allow_origins=origins,
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )

# Embeddings config helpers
def _embeddings_model() -> str:
    return os.getenv("EMBEDDINGS_MODEL", "text-embedding-3-small")

def _embeddings_dim(model: str) -> int | None:
    m = (model or "").strip().lower()
    if m in {"text-embedding-3-small", "text-embedding-3-small@latest"}:
        return 1536
    if m in {"text-embedding-3-large", "text-embedding-3-large@latest"}:
        return 3072
    return 1536

def _index_dimension(name: str) -> int | None:
    try:
        from pinecone import Pinecone as _PC  # type: ignore
        _pc = _PC(api_key=os.getenv("PINECONE_API_KEY"))
        desc = _pc.describe_index(name)
        if isinstance(desc, dict):
            return desc.get('dimension')
        return getattr(desc, 'dimension', None)
    except Exception:
        return None

def _public_base_url() -> str | None:
    # Prefer explícito PUBLIC_BASE_URL; si no, usa NGROK_DOMAIN si está
    p = os.getenv("PUBLIC_BASE_URL")
    if p and p.strip():
        return p.rstrip('/')
    dom = os.getenv("NGROK_DOMAIN")
    if dom and dom.strip():
        return f"https://{dom.strip()}"
    return None

# Fallback tolerante: si .env/secrets no están en formato KEY=VALUE, intenta extraer claves.
def _fallback_parse_secrets_env():
    try:
        path = os.path.join(os.path.dirname(__file__), 'secrets.local.txt')
        if not os.path.exists(path):
            return
        txt = open(path, 'r', errors='ignore').read()
        import re as _re
        # OpenAI: primera clave sk- válida
        if not os.getenv('OPENAI_API_KEY'):
            m = _re.search(r"\bsk-[A-Za-z0-9_-]{20,}\b", txt)
            if m:
                os.environ['OPENAI_API_KEY'] = m.group(0)
        # Pinecone
        if not os.getenv('PINECONE_API_KEY'):
            m = _re.search(r"\bpcsk_[A-Za-z0-9_-]{20,}\b", txt)
            if m:
                os.environ['PINECONE_API_KEY'] = m.group(0)
        # Twilio SID/TOKEN
        if not os.getenv('TWILIO_SID'):
            m = _re.search(r"\bAC[a-f0-9]{32}\b", txt)
            if m:
                os.environ['TWILIO_SID'] = m.group(0)
        if not os.getenv('TWILIO_AUTH_TOKEN'):
            # si hay etiqueta 'Auth Token' seguida de la cadena
            m = _re.search(r"Auth Token\s*\n\s*([0-9a-f]{32})", txt, flags=_re.IGNORECASE)
            if m:
                os.environ['TWILIO_AUTH_TOKEN'] = m.group(1)
        # Defaults útiles
        os.environ.setdefault('PINECONE_ENV', 'us-east-1-aws')
        os.environ.setdefault('PINECONE_INDEX', 'SSOT-HIGER')
        os.environ.setdefault('PINECONE_INDEX_DIAGRAMS', 'ssot-higer-diagramas-elect')
    except Exception:
        pass

_fallback_parse_secrets_env()

# --- Utilidades de módulo ---
_parts_equivalences_cache: dict | None = None


def _normalize_ascii(text: str | None) -> str:
    if not text:
        return ""
    return unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii')


def _wants_equivalences(texts: list[str] | None) -> bool:
    if not texts:
        return False
    keywords = [
        'equivalencia', 'equivalencias',
        'refaccion', 'refacciones',
        'comprar', 'conseguir', 'proveedor', 'proveedores',
        'alternativa', 'alternativas',
        'donde compro', 'donde consigo', 'donde puedo comprar', 'donde puedo conseguir',
        'buscar refaccion', 'buscar refacción', 'buscar pieza',
    ]
    for raw in texts:
        if not raw:
            continue
        txt = _normalize_ascii(raw).lower()
        for kw in keywords:
            if kw in txt:
                return True
    return False


def _load_parts_equivalences() -> dict:
    global _parts_equivalences_cache
    if _parts_equivalences_cache is not None:
        return _parts_equivalences_cache
    candidates = [
        os.getenv("PARTS_EQUIVALENCES_FILE", os.path.join("data", "parts_equivalences.json")),
        os.path.join(os.path.dirname(__file__), "data", "parts_equivalences.json"),
    ]
    data = {}
    for path in candidates:
        try:
            if path and os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    break
        except Exception:
            continue
    _parts_equivalences_cache = data if isinstance(data, dict) else {}
    return _parts_equivalences_cache


def _match_equivalences(query: str, top_k: int = 5) -> list[dict]:
    try:
        import re as _re
    except Exception:
        _re = None  # type: ignore
    if not query or not query.strip():
        return []
    data = _load_parts_equivalences()
    if not data:
        return []

    def token_set(text: str) -> set[str]:
        if not text or not _re:
            return set()
        normalized = _normalize_ascii(text).lower()
        base_tokens = set(_re.findall(r"\w+", normalized))
        extended = set(base_tokens)
        for tok in list(base_tokens):
            if tok.endswith('es') and len(tok) > 3:
                extended.add(tok[:-2])
            if tok.endswith('s') and len(tok) > 3:
                extended.add(tok[:-1])
        return extended

    q_lower = _normalize_ascii(query).lower()
    q_tokens = token_set(query)
    scored = []

    for ref, info in data.items():
        equivalents = info.get("equivalents") or []
        has_oem = any((eq or {}).get("type") == "oem" for eq in equivalents)
        has_aftermarket = any((eq or {}).get("type") == "aftermarket" for eq in equivalents)
        best = 0.0

        # Direct match by OEM in query
        if ref.lower() in q_lower:
            best = 1.0

        # Direct match by provider part number
        for eq in equivalents:
            part_num = (eq or {}).get("part_number") or ""
            if part_num and part_num.lower() in q_lower:
                best = max(best, 1.0)

        name = (info.get("name") or "").strip()
        texts = [name]
        for eq in equivalents:
            provider = (eq or {}).get("provider") or ""
            part_num = (eq or {}).get("part_number") or ""
            desc = (eq or {}).get("description") or ""
            texts.extend([
                provider,
                part_num,
                desc,
                f"{provider} {part_num}".strip(),
                f"{provider} {desc}".strip(),
            ])

        for txt in texts:
            if not txt:
                continue
            if not q_tokens:
                continue
            t_tokens = token_set(txt)
            if not t_tokens:
                continue
            inter = len(q_tokens & t_tokens)
            if not inter:
                continue
            score = inter / len(q_tokens | t_tokens)
            if score > best:
                best = score

        if best >= 0.2:
            scored.append({
                "internal_ref": ref,
                "name": name or None,
                "score": round(min(best, 1.0), 4),
                "has_oem": has_oem,
                "has_aftermarket": has_aftermarket,
                "equivalents": equivalents,
                "sources": info.get("sources") or {},
            })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[: max(1, min(top_k, 10))]


def _format_equivalences_block(items: list[dict], max_entries: int = 3, max_variants: int = 4) -> str:
    if not items:
        return ""
    lines = ["Equivalencias sugeridas:"]
    seen = set()
    for eq in items:
        ref = eq.get('internal_ref')
        if not ref or ref in seen:
            continue
        seen.add(ref)
        display_name = eq.get('name') or 'sin nombre'
        variants = []
        for variant in (eq.get('equivalents') or [])[:max_variants]:
            if not isinstance(variant, dict):
                continue
            provider = (variant.get('provider') or '').strip()
            part_num = (variant.get('part_number') or '').strip()
            vtype = (variant.get('type') or '').strip()
            pieces = [provider]
            if part_num:
                pieces.append(part_num)
            if vtype == 'oem':
                pieces.append('OEM')
            elif vtype:
                pieces.append(vtype)
            label = " ".join([p for p in pieces if p])
            if label:
                variants.append(label)
        info = ", ".join(variants) if variants else 'sin alternativas registradas'
        lines.append(f"- {ref} ({display_name}): {info}")
        if len(lines) - 1 >= max_entries:
            break
    return "\n".join(lines)


def _collect_equivalence_suggestions(primary: str | None, extras: list[str] | None = None, limit: int = 3, force: bool = False) -> list[dict]:
    candidates: list[str] = []
    if primary and primary.strip():
        candidates.append(primary)
    for extra in extras or []:
        if extra and extra.strip() and extra not in candidates:
            candidates.append(extra)
    if not candidates:
        return []
    if not force and not _wants_equivalences(candidates):
        return []
    expanded = []
    for cand in list(candidates):
        norm = _normalize_ascii(cand).lower()
        for token in norm.split():
            token = token.strip()
            if len(token) >= 4:
                expanded.append(token)
    for item in expanded:
        if item not in candidates:
            candidates.append(item)
    suggestions: list[dict] = []
    seen: set[str] = set()
    for cand in candidates:
        for eq in _match_equivalences(cand, top_k=limit):
            ref = eq.get('internal_ref')
            if ref and ref not in seen:
                suggestions.append(eq)
                seen.add(ref)
            if len(suggestions) >= limit:
                break
        if len(suggestions) >= limit:
            break
    return suggestions


def roman_to_int(s: str):
    """Convierte números romanos simples a enteros; devuelve None si no aplica."""
    if not s:
        return None
    s = str(s).strip().lower()
    roman_map = {'i': 1, 'v': 5, 'x': 10, 'l': 50, 'c': 100, 'd': 500, 'm': 1000}
    if any(ch not in roman_map for ch in s):
        return None
    total, prev = 0, 0
    for ch in reversed(s):
        val = roman_map.get(ch, 0)
        if val < prev:
            total -= val
        else:
            total += val
        prev = val
    return total if total > 0 else None

# --- Prompt del asistente (para RetrievalQA tipo "stuff") ---
PROMPT = PromptTemplate(
    input_variables=["context", "question"],
    template=(
        "Eres un técnico de postventa Higer hablando con transportistas. "
        "Responde en español con tono cercano, práctico y humano (no robótico). "
        "Usa el contexto SOLO para fundamentar lo que digas. "
        "Si el modelo exacto no cambia el procedimiento (p. ej. H6C vs H5C salvo asientos), no pidas modelo para avanzar. "
        "Si falta info crítica (p. ej. DTC), primero da pasos generales seguros y al final pide ese dato para afinar. "
        "Si detectas síntomas de riesgo (frenos, aceite, temperatura, humo), incluye una Alerta de seguridad y pide detener la vagoneta si aplica.\n\n"
        "Entrega la respuesta así:\n"
        "- Resumen breve (1-2 líneas, directo).\n"
        "- Pasos prácticos (numerados, accionables).\n"
        "- Nota: si aplica, aclara que H6C/H5C comparten la mecánica base.\n"
        "- Fuente: indica página(s) si el contexto lo provee.\n"
        "- Cierre: si NO es crítico, termina con '¿Te ayudo con algo más?'.\n\n"
        "Contexto:\n{context}\n\n"
        "Pregunta:\n{question}\n\n"
        "Respuesta:"
    ),
)

# --- Utilidades de longitud y resumen ---
def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default

def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


_MEDIA_CACHE_LIMIT = int(os.getenv("MEDIA_CACHE_MAX", "256"))
_MEDIA_ANALYSIS_CACHE: "OrderedDict[str, dict]" = OrderedDict()
_media_cache_lock = threading.Lock()

_SUPPORTED_IMAGE_TYPES = {
    'image/jpeg',
    'image/png',
    'image/webp',
}

_SUPPORTED_AUDIO_TYPES = {
    'audio/ogg',
    'audio/mpeg',
    'audio/mp3',
    'audio/wav',
    'audio/x-wav',
    'audio/mp4',
    'audio/x-m4a',
}

_EMBED_CACHE_LIMIT = int(os.getenv("EMBED_CACHE_MAX", "512"))
_EMBEDDING_CACHE: "OrderedDict[str, list[float]]" = OrderedDict()
_embed_cache_lock = threading.Lock()

_OEM_PATTERN = re.compile(r"\b[A-Z0-9]{2,}(?:[\-–][A-Z0-9]{2,})+\b")


def _mask_vin(value: str | None) -> str | None:
    if not value:
        return None
    val = str(value).strip()
    if not val:
        return None
    visible = 4 if len(val) > 4 else max(1, len(val))
    hidden = '*' * max(0, len(val) - visible)
    return hidden + val[-visible:]


def _mask_plate(value: str | None) -> str | None:
    if not value:
        return None
    val = str(value).strip()
    if not val:
        return None
    visible = 3 if len(val) > 3 else len(val)
    hidden = '*' * max(0, len(val) - visible)
    return hidden + val[-visible:]


def _sanitize_ocr_for_log(payload: dict | None) -> dict:
    data = payload or {}
    return {
        'odo_km': data.get('odo_km'),
        'vin': _mask_vin(data.get('vin')),
        'plate': _mask_plate(data.get('plate')),
        'evidence_type': data.get('evidence_type'),
    }


def _media_cache_get(url: str) -> Optional[dict]:
    if not url:
        return None
    with _media_cache_lock:
        entry = _MEDIA_ANALYSIS_CACHE.get(url)
        if entry:
            _MEDIA_ANALYSIS_CACHE.move_to_end(url)
            return dict(entry)
    return None


def _media_cache_set(url: str, data: dict):
    if not url:
        return
    with _media_cache_lock:
        _MEDIA_ANALYSIS_CACHE[url] = dict(data)
        _MEDIA_ANALYSIS_CACHE.move_to_end(url)
        while len(_MEDIA_ANALYSIS_CACHE) > _MEDIA_CACHE_LIMIT:
            _MEDIA_ANALYSIS_CACHE.popitem(last=False)


def _media_max_bytes() -> Optional[int]:
    val = os.getenv("MEDIA_MAX_BYTES")
    if not val:
        return None
    try:
        return int(val)
    except Exception:
        return None


def _fetch_content_length(url: str, allow_auth: bool) -> Optional[int]:
    try:
        import httpx  # type: ignore
    except Exception:
        return None

    headers = {'Accept': '*/*'}
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True) as cli:
            resp = cli.head(url or '', headers=headers)
            if resp.status_code < 400:
                length = resp.headers.get('Content-Length')
                if length:
                    try:
                        return int(length)
                    except Exception:
                        return None
            elif allow_auth and resp.status_code in (401, 403):
                sid = os.getenv('TWILIO_SID') or os.getenv('TWILIO_ACCOUNT_SID')
                token = os.getenv('TWILIO_AUTH_TOKEN')
                if sid and token:
                    try:
                        with httpx.Client(timeout=10.0, follow_redirects=True) as cli_auth:
                            resp_auth = cli_auth.head(url or '', headers=headers, auth=(sid, token))
                            if resp_auth.status_code < 400:
                                length = resp_auth.headers.get('Content-Length')
                                if length:
                                    try:
                                        return int(length)
                                    except Exception:
                                        return None
                    except Exception:
                        return None
    except Exception:
        return None
    return None


def _validate_media(url: str, ctype_hint: str | None) -> tuple[str | None, Optional[str]]:
    ctype = (ctype_hint or '').lower().split(';')[0].strip()
    try:
        if not ctype:
            ctype = (_guess_content_type(url, ctype_hint) or '').lower()
    except Exception:
        pass
    base = ctype.split('/')[0] if ctype else ''
    if base not in {'image', 'audio'}:
        return ctype, 'unsupported_type'
    if base == 'image' and ctype not in _SUPPORTED_IMAGE_TYPES:
        return ctype, 'unsupported_image_type'
    if base == 'audio' and ctype not in _SUPPORTED_AUDIO_TYPES:
        return ctype, 'unsupported_audio_type'
    max_bytes = _media_max_bytes()
    if max_bytes:
        domain = urlparse(url or '').netloc or ''
        size = _fetch_content_length(url, allow_auth=('twilio' in domain.lower()))
        if size is not None and size > max_bytes:
            return ctype, 'too_large'
    return ctype, None


def _catalog_hits_for_question(question: str, top_k: Optional[int] = None) -> list:
    if not question or not _OEM_PATTERN.search(question):
        return []
    limit = top_k or _int_env('CATALOG_TOP_K', 3)
    try:
        hits = _catalog_lookup(question, top_k=limit) or []
    except Exception:
        hits = []
    docs = []
    seen = set()
    for item in hits:
        oem = (item.get('oem') or '').upper()
        if not oem or oem in seen:
            continue
        seen.add(oem)
        part_name = item.get('part_name') or ''
        page_label = item.get('page_label') or 'N/A'
        text = f"OEM {oem} — {part_name} (pág {page_label})"
        docs.append({
            'id': f"catalog:{oem}",
            'metadata': {
                'source': 'catalog',
                'doc_id': 'parts_catalog',
                'page_label': page_label,
                'oem': oem,
                'part_name': part_name,
                'text': text,
            },
            'hybrid': 1.25,
        })
    return docs


def _prepend_catalog_hits(question: str, docs: list) -> list:
    try:
        hits = _catalog_hits_for_question(question)
    except Exception:
        hits = []
    if not hits:
        return docs
    existing = set()
    for d in docs or []:
        md = d.get('metadata', {}) if isinstance(d, dict) else {}
        if (md.get('source') or '').lower() == 'catalog' and md.get('oem'):
            existing.add(md.get('oem').upper())
    filtered = []
    for h in hits:
        oem = (h.get('metadata') or {}).get('oem')
        if oem and oem.upper() in existing:
            continue
        filtered.append(h)
        if oem:
            existing.add(oem.upper())
    return (filtered + (docs or [])) if filtered else docs


def _format_citations(sources: list | None) -> str:
    if not sources:
        return ""
    manual = []
    cases = []
    catalog = []
    seen = set()
    for src in sources:
        if not isinstance(src, dict):
            continue
        md = src.get('metadata', {}) or {}
        source = (md.get('source') or '').lower()
        key = (source, md.get('doc_id'), md.get('page_label'), md.get('oem'))
        if key in seen:
            continue
        seen.add(key)
        if source == 'catalog':
            oem = md.get('oem') or 'OEM'
            part = md.get('part_name') or ''
            page = md.get('page_label') or 'N/A'
            catalog.append(f"- [Catálogo] OEM {oem} · {part} (pág {page})")
        elif source == 'case':
            ev = md.get('evidence_type') or 'evidencia'
            cases.append(f"- [Caso] {ev}")
        else:
            doc = md.get('doc_id') or 'Manual'
            page = md.get('page_label') or 'N/A'
            manual.append(f"- [Manual] {doc} pág {page}")
    blocks = manual[:3] + cases[:3] + catalog[:3]
    return "\n".join(blocks)


def _process_media_items(contact: str, media_items: list[dict], category: str | None, neon_case_id: str | None, case: dict | None) -> dict:
    from . import storage as _st  # noqa: F401

    provided_items: list[str] = []
    transcripts: list[str] = []
    ocr_list: list[dict] = []
    part_class_list: list[dict] = []
    rec_checks_agg: list[str] = []
    oem_hits_agg: list[dict] = []
    case_local = case
    cat_norm = (category or 'general' or '').lower()

    for m in media_items or []:
        url = (m or {}).get('url')
        if not url:
            continue
        ctype_hint = (m or {}).get('content_type')
        ctype, validation_error = _validate_media(url, ctype_hint)
        if validation_error:
            try:
                _st.log_event(
                    kind="media_skipped",
                    payload={
                        "from": contact,
                        "url": url,
                        "content_type": ctype_hint,
                        "reason": validation_error,
                    },
                )
            except Exception:
                pass
            continue

        ctype = ctype or ''
        kind_guess = 'evidencia'
        cache_entry = _media_cache_get(url)
        ocr = None
        cls = None
        transcript = None
        provided_from_media: list[str] = []
        recommended_checks: list[str] = []
        oem_hits_local: list[dict] = []

        if cache_entry:
            ocr = cache_entry.get('ocr')
            cls = cache_entry.get('classification')
            transcript = cache_entry.get('transcript')
            provided_from_media = cache_entry.get('provided_items', []) or []
            recommended_checks = cache_entry.get('recommended_checks', []) or []
            oem_hits_local = cache_entry.get('oem_hits', []) or []
            kind_guess = cache_entry.get('kind_guess', kind_guess)
        else:
            try:
                if ctype.startswith('audio'):
                    from .audio_transcribe import transcribe_audio_from_url as _tx

                    tx = _tx(url)
                    if tx:
                        transcript = tx
                        try:
                            _st.log_event(
                                kind="asr_detected",
                                payload={
                                    "from": contact,
                                    "url": url,
                                    "content_type": ctype,
                                    "transcript": tx,
                                },
                            )
                        except Exception:
                            pass
                else:
                    from .vision_openai import ocr_image_openai as _ocr
                    from .vision_openai import classify_part_image as _clf

                    ocr = _ocr(url, kind_guess)
                    if isinstance(ocr, dict):
                        try:
                            _st.log_event(
                                kind="ocr_detected",
                                payload={
                                    "from": contact,
                                    "url": url,
                                    "content_type": ctype,
                                    "ocr": _sanitize_ocr_for_log(ocr),
                                },
                            )
                        except Exception:
                            pass
                        if ocr.get('vin'):
                            kind_guess = 'vin_plate'
                            provided_from_media.append('foto_vin')
                        if ocr.get('odo_km') is not None:
                            kind_guess = 'odometro'
                            provided_from_media.append('foto_odometro')
                        if ocr.get('plate'):
                            provided_from_media.append('foto_placa_unidad')
                        ev = (ocr.get('evidence_type') or '').lower()
                        if 'fuga' in ev:
                            if cat_norm == 'brakes':
                                provided_from_media.append('fotos_fugas_frenos')
                            elif cat_norm == 'cooling':
                                provided_from_media.append('fotos_fugas_cooling')
                    try:
                        cls = _clf(url)
                    except Exception:
                        cls = None
                    if isinstance(cls, dict):
                        recommended_checks = [rc for rc in (cls.get('recommended_checks') or [])[:6] if isinstance(rc, str)]
                        oem_hits_local = [h for h in (cls.get('oem_hits') or [])[:5] if isinstance(h, dict)]
                        try:
                            _st.log_event(
                                kind="vision_classified",
                                payload={
                                    "from": contact,
                                    "url": url,
                                    "classification": {
                                        "part_guess": cls.get('part_guess'),
                                        "brand": cls.get('brand'),
                                        "fluid": cls.get('fluid_guess'),
                                        "confidence": cls.get('confidence'),
                                    },
                                },
                            )
                        except Exception:
                            pass
            except Exception:
                pass

            _media_cache_set(
                url,
                {
                    'ocr': ocr,
                    'classification': cls,
                    'transcript': transcript,
                    'provided_items': provided_from_media,
                    'recommended_checks': recommended_checks,
                    'oem_hits': oem_hits_local,
                    'kind_guess': kind_guess,
                },
            )

        if transcript and transcript not in transcripts:
            transcripts.append(transcript)
            try:
                sig_tx = _st.extract_signals(transcript)
                if isinstance(sig_tx, dict):
                    new_cat = sig_tx.get('category')
                    if new_cat and new_cat not in ((case_local or {}).get('categories') or []):
                        cats = ((case_local or {}).get('categories') or []) + [new_cat]
                        case_local = _st.update_case(str(contact), {'categories': cats})
                    sev_map = {'normal': 0, 'urgent': 1, 'critical': 2}
                    if sev_map.get(sig_tx.get('severity', 'normal'), 0) > sev_map.get((case_local or {}).get('severity', 'normal'), 0):
                        case_local = _st.update_case(str(contact), {'severity': sig_tx.get('severity')})
            except Exception:
                pass

        if isinstance(ocr, dict):
            ocr_list.append(ocr)
            if ocr.get('delivered_at') and neon_case_id:
                try:
                    from . import db_cases as _dbc
                    _dbc.upsert_case_meta(neon_case_id, delivered_at=ocr['delivered_at'])
                except Exception:
                    pass
            if neon_case_id and not ocr.get('delivered_at') and ocr.get('vin'):
                try:
                    from .warranty import resolve_delivered_at_by_vin as _dl
                    iso = _dl(ocr.get('vin'))
                    if iso:
                        from . import db_cases as _dbc
                        _dbc.upsert_case_meta(neon_case_id, delivered_at=iso)
                except Exception:
                    pass

        if isinstance(cls, dict):
            part_class_list.append(cls)
            for rc in recommended_checks:
                if rc not in rec_checks_agg:
                    rec_checks_agg.append(rc)
            for h in oem_hits_local:
                oem_hits_agg.append(h)

        if provided_from_media:
            provided_items.extend(provided_from_media)

        try:
            if neon_case_id:
                from . import db_cases as _dbc
                _dbc.add_attachment(neon_case_id, kind_guess, url, ocr=ocr, meta={'content_type': ctype})
                if isinstance(ocr, dict):
                    fields = {}
                    if ocr.get('vin'):
                        fields['vin'] = ocr['vin']
                    if ocr.get('plate'):
                        fields['plate'] = ocr['plate']
                    if ocr.get('odo_km') is not None:
                        fields['odo_km'] = ocr['odo_km']
                    if fields:
                        _dbc.upsert_case_meta(neon_case_id, **fields)
        except Exception:
            pass
    return {
        'provided_items': provided_items,
        'transcripts': transcripts,
        'ocr_list': ocr_list,
        'part_class_list': part_class_list,
        'rec_checks': rec_checks_agg,
        'oem_hits': oem_hits_agg,
        'case': case_local,
    }

def _effective_limit(meta: dict | None, channel: str | None = None) -> int:
    base = _int_env("ANSWER_MAX_CHARS", 1200)
    if meta and isinstance(meta, dict):
        mc = meta.get("max_chars")
        try:
            if mc:
                base = int(mc)
        except Exception:
            pass
        sev = str(meta.get('severity')).lower() if meta.get('severity') is not None else ""
        if sev == 'critical':
            base = min(base, _int_env("CRITICAL_MAX_CHARS", 900))
        elif sev == 'urgent':
            base = min(base, _int_env("URGENT_MAX_CHARS", 1200))
    if (channel or "").lower() == "whatsapp":
        wa = _int_env("WHATSAPP_MAX_CHARS", 1600)
        base = min(base, wa)
    return max(200, base)

def _bool_env(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return str(val).strip().lower() in {"1","true","yes","y"}


def _twilio_validation_enabled() -> bool:
    """Determina si se debe validar la firma de Twilio (por defecto sí)."""
    return _bool_env('TWILIO_VALIDATE', True)


def _twilio_expected_signature(token: str, url: str, params: Sequence[tuple[str, str]]) -> str:
    payload = url + ''.join(f"{k}{str(v)}" for k, v in params)
    digest = hmac.new(token.encode('utf-8'), payload.encode('utf-8'), hashlib.sha1).digest()
    return base64.b64encode(digest).decode('utf-8')


def _log_twilio_signature_issue(kind: str, path: str, signature: str | None, error: str | None = None) -> None:
    try:
        from . import storage as _st
        payload = {
            'endpoint': path,
            'kind': kind,
            'signature': signature,
        }
        if error:
            payload['error'] = error
        _st.log_event('twilio_signature_failed', payload)
    except Exception:
        pass
    logger.warning("Twilio signature check failed: kind=%s path=%s", kind, path)


async def _enforce_twilio_signature(request: Request, path: str = "/twilio/whatsapp") -> None:
    """Valida la firma Twilio si está habilitada; lanza HTTP 403 si no coincide."""
    if not _twilio_validation_enabled():
        return
    token = os.getenv('TWILIO_AUTH_TOKEN')
    signature = request.headers.get('X-Twilio-Signature') or request.headers.get('x-twilio-signature')
    base = _public_base_url()
    if not token or not signature or not base:
        _log_twilio_signature_issue('invalid_config', path, signature)
        raise HTTPException(status_code=403, detail="twilio signature invalid (config)")
    url = base.rstrip('/') + (path if path.startswith('/') else f"/{path}")
    try:
        form = await request.form()
        items = sorted((k, str(v)) for k, v in form.multi_items())
        expected = _twilio_expected_signature(token, url, items)
    except HTTPException:
        raise
    except Exception as exc:
        _log_twilio_signature_issue('error', path, signature, str(exc))
        raise HTTPException(status_code=403, detail="twilio signature error") from exc
    if not hmac.compare_digest(expected, signature):
        _log_twilio_signature_issue('invalid_signature', path, signature)
        raise HTTPException(status_code=403, detail="twilio signature invalid")

def _warranty_requested(texts: list[str]) -> bool:
    import re as _re
    t = " ".join([str(x or '') for x in texts])
    return bool(_re.search(r"\b(garant[ií]a|warranty|cobertura|claim)\b", t, flags=_re.IGNORECASE))

def _detect_topic_switch(body: str | None, ocr_list: list | None = None, transcripts: list | None = None, cls_list: list | None = None) -> bool:
    """Detecta cambio de tema para reducir/ignorar historia en esta interacción."""
    try:
        txt = (body or '').lower()
        # Palabras/frases típicas de cambio de tema o consulta de producto
        keys = [
            'otro tema', 'no es problema', 'este es el bueno', 'checa la imagen', 'dime que ves',
            'es de la marca', 'garrafa', 'bidón', 'refrigerante', 'anticongelante', 'valucraft'
        ]
        if any(k in txt for k in keys):
            return True
        # Revisar notas OCR (si describen insumos/grafas)
        if ocr_list:
            for oc in ocr_list:
                if isinstance(oc, dict):
                    snippet = " ".join(str(oc.get(field) or '') for field in ('notes', 'raw'))
                else:
                    snippet = str(oc or '')
                snippet_l = snippet.lower()
                if any(k in snippet_l for k in ['garrafa', 'valucraft', 'refrigerante', 'bidón', 'bidon', 'anticongelante']):
                    return True
        # Si la clasificación detecta insumo/brand/fluido, asumir tema nuevo (consulta de producto)
        for c in (cls_list or []) or []:
            if isinstance(c, dict) and (c.get('brand') or c.get('fluid_guess')):
                return True
        # Si transcripción menciona explícitamente "no es sobre ...", también
        for tx in (transcripts or []) or []:
            tl = (tx or '').lower()
            if 'otro tema' in tl or 'no es problema' in tl:
                return True
            if 'checa la imagen' in tl or 'revisa la imagen' in tl:
                return True
    except Exception:
        return False
    return False

def _quick_replies_line(actions: list[str]) -> str:
    try:
        if not actions:
            return ""
        return "Respuestas rápidas: " + "  •  ".join(actions)
    except Exception:
        return ""

def _is_quick_parts(text: str | None) -> bool:
    t = (text or '').strip().lower()
    return any(p in t for p in ["buscar refacción", "buscar refaccion", "buscar refacciones"]) or t == "refacciones" or t == "refacción" or t == "refaccion"

def _extractive_summary(text: str, limit: int, max_sentences: int = 4) -> str:
    if not text:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    summary_parts: list[str] = []
    total = 0
    for sentence in sentences:
        s = sentence.strip()
        if not s:
            continue
        # Evitar sumar más de lo necesario
        if summary_parts and any(s.lower().startswith(prefix) for prefix in {"-", "•"}):
            continue
        summary_parts.append(s)
        total += len(s) + 1
        if total >= limit or len(summary_parts) >= max_sentences:
            break
    if not summary_parts:
        snippet = text[:limit]
        return snippet
    joined = " ".join(summary_parts)
    if len(joined) > limit:
        trimmed = joined[:limit].rsplit(' ', 1)[0]
        return trimmed + '…'
    return joined


def _summarize_to_limit(text: str, question: str, sources: list | None, limit: int) -> str:
    if not text:
        return text
    if len(text) <= limit:
        return text
    force_llm = _bool_env('SUMMARY_FORCE_LLM', False)
    extractive = _extractive_summary(text, limit)
    if extractive and len(extractive) <= limit and (not force_llm):
        return extractive
    # Intentar condensar con LLM manteniendo tono cercano y bullets
    try:
        summary_model = os.getenv("LLM_SUMMARY_MODEL") or os.getenv("SUMMARIZER_MODEL") or "gpt-4o-mini"
        llm = ChatOpenAI(model=summary_model, temperature=0.1)
        pages = []
        try:
            for s in (sources or [])[:5]:
                pl = s.get('page_label') if isinstance(s, dict) else None
                if pl is not None:
                    pages.append(str(pl))
        except Exception:
            pass
        pages_txt = (", ".join(pages)) if pages else ""
        prompt = (
            "Condensa la siguiente respuesta para WhatsApp en español, tono cercano a transportistas. "
            f"Máximo {limit} caracteres. Mantén estructura clara:\n"
            "- Resumen breve (1 línea).\n"
            "- Puntos clave (3–6 viñetas).\n"
            "- Siguiente paso (1 línea).\n"
            + (f"- Fuente: páginas {pages_txt}.\n" if pages_txt else "") +
            "Si falta contexto, no inventes, mantén lo cierto.\n\n"
            f"Pregunta: {question}\n\n"
            f"Respuesta original:\n{text}\n\n"
            "Ahora entrega solo la versión condensada."
        )
        out = llm.invoke(prompt)
        condensed = getattr(out, 'content', None) or str(out)
        # Asegurar límite duro
        if condensed and len(condensed) > limit:
            return condensed[: max(0, limit - 1)] + "…"
        return condensed or (text[: max(0, limit - 1)] + "…")
    except Exception:
        # Fallback a truncado elegante por párrafos
        extractive = extractive or _extractive_summary(text, limit)
        if extractive:
            trimmed = extractive[:limit]
            return trimmed + ('…' if len(trimmed) < len(text or "") else '')
        parts = (text or "").split("\n\n")
        out = []
        total = 0
        for p in parts:
            if total + len(p) + 2 > limit:
                break
            out.append(p)
            total += len(p) + 2
        joined = "\n\n".join(out)
        if not joined:
            joined = (text or "")[: max(0, limit - 1)]
        return joined + ("…" if len(joined) < len(text or "") else "")


# --- Idempotencia Webhook (dedupe por MessageSid) ---
_SID_STORE = os.path.join(os.path.dirname(__file__), 'logs', 'processed_sids.json')

def _sid_store_load() -> dict:
    try:
        base = os.path.dirname(_SID_STORE)
        if base:
            os.makedirs(base, exist_ok=True)
        if os.path.exists(_SID_STORE):
            import json as _json
            with open(_SID_STORE, 'r', encoding='utf-8') as f:
                return _json.load(f) or {}
    except Exception:
        pass
    return {}

def _sid_store_save(data: dict):
    try:
        import json as _json
        base = os.path.dirname(_SID_STORE)
        if base:
            os.makedirs(base, exist_ok=True)
        tmp = _SID_STORE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            _json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, _SID_STORE)
    except Exception:
        pass

def _sid_get_answer(message_sid: str, ttl_sec: int = 600):
    if not message_sid:
        return None
    try:
        data = _sid_store_load()
        rec = data.get(message_sid)
        if not isinstance(rec, dict):
            return None
        ts = float(rec.get('ts') or 0)
        if (time.time() - ts) <= max(60, int(ttl_sec)):
            return rec.get('answer')
    except Exception:
        return None
    return None

def _sid_remember(message_sid: str, answer: str):
    if not message_sid or not answer:
        return
    try:
        data = _sid_store_load()
        data[message_sid] = {'ts': time.time(), 'answer': answer}
        _sid_store_save(data)
    except Exception:
        pass

# --- Detección de tipo de media (fallback) ---
def _guess_content_type(url: str | None, hint: str | None) -> str | None:
    try:
        ct = (hint or "").lower().split(";")[0].strip()
        if ct:
            return ct
        # Por extensión
        p = urlparse(url or '').path.lower()
        ext_map = {
            '.ogg': 'audio/ogg', '.oga': 'audio/ogg', '.opus': 'audio/ogg', '.mp3': 'audio/mpeg',
            '.m4a': 'audio/mp4', '.wav': 'audio/wav', '.amr': 'audio/amr',
            '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp'
        }
        for ext, c in ext_map.items():
            if p.endswith(ext):
                return c
        # HEAD a la URL (sin auth) para Content-Type
        try:
            import httpx  # type: ignore
            with httpx.Client(timeout=10.0, follow_redirects=True) as cli:
                r = cli.head(url or '', headers={'Accept': '*/*'})
                if r.status_code < 400:
                    cth = r.headers.get('Content-Type')
                    if cth:
                        return cth.split(';')[0].strip().lower()
        except Exception:
            pass
        # Fallback: HEAD con auth si es media de Twilio
        try:
            from urllib.parse import urlparse as _up
            pr = _up(url or '')
            if 'twilio.com' in (pr.netloc or ''):
                sid = os.getenv('TWILIO_SID') or os.getenv('TWILIO_ACCOUNT_SID')
                token = os.getenv('TWILIO_AUTH_TOKEN')
                if sid and token:
                    import httpx  # type: ignore
                    with httpx.Client(timeout=10.0, follow_redirects=True) as cli:
                        r = cli.head(url or '', auth=(sid, token), headers={'Accept': '*/*'})
                        if r.status_code < 400:
                            cth = r.headers.get('Content-Type')
                            if cth:
                                return cth.split(';')[0].strip().lower()
        except Exception:
            pass
    except Exception:
        return hint
    return hint


# --- Señales agregadas de la conversación + turno actual ---
def _aggregate_signals(texts: list[str]) -> dict:
    try:
        from . import storage as _st
    except Exception:
        _st = None  # type: ignore
    cats = []
    worst = 'normal'
    order = {'normal': 0, 'urgent': 1, 'critical': 2}
    for t in texts or []:
        if not _st:
            continue
        try:
            s = _st.extract_signals(t)
            if isinstance(s, dict):
                if s.get('category'):
                    cats.append(s['category'])
                sev = s.get('severity') or 'normal'
                if order.get(sev, 0) > order.get(worst, 0):
                    worst = sev
        except Exception:
            continue
    cats_u = []
    seen = set()
    for c in cats:
        if c not in seen:
            seen.add(c)
            cats_u.append(c)
    return {'severity': worst, 'categories': cats_u}

# --- Modelos Pydantic ---
class QueryRequest(BaseModel):
    question: str
    meta: dict | None = None

# MEJORA: Modelo de respuesta definido
class QueryResponse(BaseModel):
    question: str
    answer: str
    sources: list | None = None

# --- Global variables y Prompt (Asumiendo que tienes tu PromptTemplate definido) ---
qa_chain = None
# PROMPT = ... 

@app.on_event("startup")
def startup_event():
    global qa_chain
    INDEX_NAME = os.getenv("PINECONE_INDEX", "ssot-higer")
    BRAND_NAME = os.getenv("BRAND_NAME")
    MODEL_NAME = os.getenv("MODEL_NAME")
    FORCE_MODEL_FILTER = (os.getenv("FORCE_MODEL_FILTER", "").strip().lower() in {"1","true","yes"})
    # Validaciones básicas de entorno
    missing = []
    if not os.getenv("OPENAI_API_KEY"):
        missing.append("OPENAI_API_KEY")
    if not os.getenv("PINECONE_API_KEY"):
        missing.append("PINECONE_API_KEY")
    if not os.getenv("PINECONE_ENV"):
        missing.append("PINECONE_ENV")
    if missing:
        logger.error(f"Faltan variables de entorno requeridas: {', '.join(missing)}")
        return
    logger.info("Initializing RAG components...")

    try:
        emb_model = _embeddings_model()
        expected_dim = _embeddings_dim(emb_model)
        # Verificar dimensión del índice (si es posible)
        try:
            idx_dim = _index_dimension(INDEX_NAME)
            if idx_dim is not None and expected_dim is not None and int(idx_dim) != int(expected_dim):
                logger.error(f"Dimensión del índice '{INDEX_NAME}' ({idx_dim}) != embeddings ({expected_dim}) [model={emb_model}]. Reingesta requerida o ajusta EMBEDDINGS_MODEL.")
                return
        except Exception:
            pass
        embeddings = OpenAIEmbeddings(model=emb_model)

        # CORRECCIÓN Problema 2: Usar ChatOpenAI y modelo moderno (gpt-4o)
        llm = ChatOpenAI(model=os.getenv("LLM_MODEL", "gpt-4o"), temperature=0.1)

        vectorstore = Pinecone.from_existing_index(INDEX_NAME, embeddings)

        retriever_k = int(os.getenv("RETRIEVER_K", "8"))
        pinecone_filter = None
        if BRAND_NAME or (MODEL_NAME and FORCE_MODEL_FILTER):
            pinecone_filter = {}
            if BRAND_NAME:
                pinecone_filter["brand"] = BRAND_NAME
            # Solo forzar modelo si el usuario lo activó explícitamente
            if MODEL_NAME and FORCE_MODEL_FILTER:
                pinecone_filter["model"] = MODEL_NAME
        qa_chain = RetrievalQA.from_chain_type(
            llm=llm,
            chain_type="stuff",
            retriever=vectorstore.as_retriever(search_kwargs={"k": retriever_k, "filter": pinecone_filter} if pinecone_filter else {"k": retriever_k}),
            chain_type_kwargs={"prompt": PROMPT},
            return_source_documents=True
        )
        logger.info("RAG initialized successfully.")
    except Exception as e:
        logger.error(f"Error initializing RAG: {e}")


# CORRECCIÓN Problema 3: Usar 'def' (síncrono) en lugar de 'async def'
@app.post("/query", response_model=QueryResponse)
def query_rag(request: QueryRequest):
    """
    Endpoint to query the RAG system.
    """
    _t0 = time.perf_counter()
    if qa_chain is None:
        raise HTTPException(status_code=503, detail="RAG system is not initialized.")

    try:
        # Ejecutar la cadena usando 'invoke'. RetrievalQA espera la clave "query".
        result_dict = qa_chain.invoke({"query": request.question})

        # Extraer la respuesta (la clave para RetrievalQA es "result")
        answer = result_dict.get("result", "Error en el procesamiento.")
        docs = result_dict.get("source_documents", []) or []

        # Formatear fuentes de forma ligera
        def trim_tokens(text: str, max_tokens: int = 60) -> str:
            toks = (text or '').split()
            if len(toks) <= max_tokens:
                return ' '.join(toks)
            return ' '.join(toks[:max_tokens])

        sources_unsorted = []
        for d in docs[:5]:
            meta = getattr(d, 'metadata', {}) if hasattr(d, 'metadata') else {}
            page_label = meta.get('page_label', meta.get('page', 'N/A'))
            # snippet preferentemente del contenido, si no del metadata['text']
            content = getattr(d, 'page_content', '')
            if not content:
                content = meta.get('text', '')
            snippet = trim_tokens((content or '').replace('\n', ' '), 60)

            # Normalizar número de página si es posible (arábigo o romano)
            page_number = None
            try:
                page_number = int(str(page_label))
            except Exception:
                pass
            if page_number is None:
                rn = roman_to_int(page_label)
                if rn is not None:
                    page_number = rn

            sources_unsorted.append({
                'page_label': page_label,
                'page_number': page_number,
                'snippet': snippet,
                'doc_id': meta.get('doc_id'),
                'chunk_index': meta.get('chunk_index')
            })

        # Orden por página con fallback (arábigos -> romanos -> alfanuméricos)

        import re as _re
        def natural_key(text: str):
            parts = _re.split(r'(\d+)', str(text))
            key = []
            for p in parts:
                if p.isdigit():
                    key.append((1, int(p)))
                else:
                    key.append((0, p.lower()))
            return key

        def page_order_key(src):
            p = src.get('page_label')
            # 1) números arábigos
            try:
                num = int(str(p))
                return (0, num)
            except Exception:
                pass
            # 2) romanos
            r = roman_to_int(p)
            if r is not None:
                return (1, r)
            # 3) alfanumérico natural
            return (2, natural_key(p))

        sources = sorted(sources_unsorted, key=page_order_key)

        # Resumen inteligente si excede el límite
        # Señales y límite según severidad
        try:
            from . import storage as _st
            sig = _st.extract_signals(request.question)
        except Exception:
            sig = {}
        meta_for_len = dict(getattr(request, 'meta', {}) or {})
        if isinstance(sig, dict):
            meta_for_len.setdefault('severity', sig.get('severity'))
        effective_limit = _effective_limit(meta_for_len)
        final_answer = _summarize_to_limit(answer, request.question, None, effective_limit)
        try:
            equiv_extras: list[str] = []
            if isinstance(request.meta, dict):
                for val in (request.meta or {}).values():
                    if isinstance(val, str):
                        equiv_extras.append(val)
            force_equiv = _wants_equivalences([request.question] + equiv_extras)
            suggestions = _collect_equivalence_suggestions(request.question, equiv_extras, force=force_equiv)
            if suggestions:
                final_answer += "\n\n" + _format_equivalences_block(suggestions)
        except Exception:
            pass
        resp = QueryResponse(
            question=request.question,
            answer=final_answer,
            sources=sources
        )

        # Log de consulta API (/query)
        try:
            from . import storage
            latency_ms = (time.perf_counter() - _t0) * 1000.0
            response_bytes = len((resp.answer or '').encode('utf-8'))
            storage.log_event(
                kind="api_query",
                payload={
                    "endpoint": "/query",
                    "question": request.question,
                    "meta": request.meta or {},
                    "classification": storage.classify(request.question),
                    "signals": storage.extract_signals(request.question),
                    "answer": resp.answer,
                    "sources": resp.sources,
                    "latency_ms": latency_ms,
                    "response_bytes": response_bytes,
                }
            )
        except Exception:
            pass

        return resp
    except Exception as e:
        logger.error(f"Error during query execution: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error.")


# ==========================
#    HÍBRIDO: /query_hybrid
# ==========================

def _env(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    return v if (v is not None and str(v).strip() != "") else default

def load_bm25_pack():
    candidates = []
    bm25_file = _env("BM25_INDEX_FILE", "bm25_index.pkl")
    if bm25_file:
        candidates.append(bm25_file)
    try:
        from pathlib import Path as _Path
        base = _Path(__file__).parent
        if bm25_file:
            candidates.append(str((base / bm25_file).resolve()))
        candidates.append(str((base / 'bm25_index_unstructured.pkl').resolve()))
    except Exception:
        pass
    candidates.append('bm25_index_unstructured.pkl')
    for p in candidates:
        try:
            if p and os.path.exists(p):
                with open(p, 'rb') as f:
                    return pickle.load(f)
        except Exception:
            continue
    return None

def bm25_top(query_text: str, bm25_pack, top_k: int = 16):
    if not bm25_pack:
        return []
    try:
        from rank_bm25 import BM25Okapi
    except Exception:
        return []
    bm25 = bm25_pack.get('bm25')
    chunks = bm25_pack.get('chunks', [])
    if not bm25 or not chunks:
        return []
    tokens = (query_text or '').split()
    scores = bm25.get_scores(tokens)
    idxs = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]
    out = []
    for i in idxs:
        doc = chunks[i]
        meta = getattr(doc, 'metadata', {}) if hasattr(doc, 'metadata') else {}
        out.append({
            'id': f'bm25-{i}',
            'score': float(scores[i]),
            'metadata': {
                'text': getattr(doc, 'page_content', ''),
                'prev_window': meta.get('prev_window', ''),
                'next_window': meta.get('next_window', ''),
                'page_label': meta.get('page_label', meta.get('page', 'N/A')),
                'doc_id': meta.get('doc_id'),
                'chunk_index': meta.get('chunk_index')
            }
        })
    return out

def build_pinecone_filter(query_text: str | None = None) -> dict | None:
    flt = {}
    brand = _env('BRAND_NAME')
    model_env = _env('MODEL_NAME', '') or ''
    force_model = (_env('FORCE_MODEL_FILTER', '') or '').strip().lower() in {"1","true","yes"}
    if brand:
        flt['brand'] = brand
    # Incluir modelo solo si el query lo menciona o si hay forzado
    ql = (query_text or '').lower()
    model_from_q = None
    for m in ["h6c", "h5c", "klq6540"]:
        if m in ql:
            model_from_q = m.upper()
            break
    if model_from_q:
        flt['model'] = model_from_q
    elif force_model and model_env:
        flt['model'] = model_env
    return flt or None

def retrieve_from_pinecone(query_embedding, top_k=16):
    try:
        from pinecone import Pinecone as PineconeClient
        pc = PineconeClient(api_key=os.getenv("PINECONE_API_KEY"))
        idx = pc.Index(os.getenv("PINECONE_INDEX", "ssot-higer"))
        flt = build_pinecone_filter()
        res = idx.query(vector=query_embedding, top_k=top_k, include_metadata=True, filter=flt)
        return res.get('matches', [])
    except Exception as e:
        logger.warning(f"Vector retrieve disabled: {e}")
        return []

def is_diagram_intent(text: str) -> bool:
    t = (text or '').lower()
    keys = ['diagrama', 'esquema', 'fusible', 'pinout', 'conector', 'circuito', 'alambrado', 'eléctrico', 'electrico']
    return any(k in t for k in keys)

def retrieve_diagrams(query_embedding, top_k=6):
    try:
        from pinecone import Pinecone as PineconeClient
        idx_name = os.getenv("PINECONE_INDEX_DIAGRAMS")
        if not idx_name:
            return []
        pc = PineconeClient(api_key=os.getenv("PINECONE_API_KEY"))
        idx = pc.Index(idx_name)
        res = idx.query(vector=query_embedding, top_k=top_k, include_metadata=True)
        matches = res.get('matches', [])
        # etiquetar como diagram y bandera de intención
        for m in matches:
            try:
                md = m.setdefault('metadata', {})
                if md is not None:
                    md.setdefault('source', 'diagram')
                    md['diagram_intent'] = True
            except Exception:
                pass
        return matches
    except Exception as e:
        logger.warning(f"Diagram retrieve disabled: {e}")
        return []

def retrieve_cases(query_embedding, top_k=8, filters: dict | None = None):
    try:
        from pinecone import Pinecone as PineconeClient
        idx_name = os.getenv("PINECONE_INDEX_CASES", "ssot-higer-cases")
        if not idx_name:
            return []
        pc = PineconeClient(api_key=os.getenv("PINECONE_API_KEY"))
        idx = pc.Index(idx_name)
        res = idx.query(vector=query_embedding, top_k=top_k, include_metadata=True, filter=filters or None)
        matches = res.get('matches', [])
        for m in matches:
            try:
                md = m.setdefault('metadata', {})
                if md is not None:
                    md.setdefault('source', 'case')
            except Exception:
                pass
        return matches
    except Exception as e:
        logger.warning(f"Cases retrieve disabled: {e}")
        return []

# Infiere un tipo de evidencia desde texto libre (mejor esfuerzo)
def _infer_evidence_type(text: str | None) -> str | None:
    t = (text or '').lower()
    if not t:
        return None
    def anyw(words):
        return any(w in t for w in words)
    if anyw(["rin", "rín", "llanta", "neumát", "neumat"]) and anyw(["burbuja", "burbuj", "espuma", "jabón", "jabon", "agua jabonosa", "burbujeo"]):
        return 'fuga_llanta'
    if anyw(["grieta", "fisura", "rajadura"]) and anyw(["rin", "rín", "aro"]):
        return 'grieta_rin'
    if anyw(["tablero", "testigo", "warning", "luz de advertencia"]):
        return 'tablero'
    if anyw(["odómetro", "odometro"]) or (" km" in t and not anyw(["km/h"])):
        return 'odometro'
    if anyw(["vin", "número de serie", "numero de serie", "num de serie"]):
        return 'vin_plate'
    if anyw(["placa de circulación", "placa unidad", "placa de la unidad", "placa" ]):
        return 'placa_unidad'
    if anyw(["fuga", "goteo", "derrama", "chorrea"]) and anyw(["aceite", "refrigerante", "frenos", "líquido", "liquido"]):
        return 'fuga_liquido'
    return None

def _normalize(scores: list[float]) -> list[float]:
    if not scores:
        return []
    mn, mx = min(scores), max(scores)
    if mx - mn < 1e-9:
        return [1.0 for _ in scores]
    return [(s - mn) / (mx - mn) for s in scores]

def _sig(meta: dict, fallback_id: str) -> str:
    txt = (meta or {}).get('text') or ''
    if txt:
        return txt
    prev_next = ((meta or {}).get('prev_window', '') or '') + ' ' + ((meta or {}).get('next_window', '') or '')
    return prev_next or fallback_id

def _jaccard(a: str, b: str) -> float:
    def tok(s: str):
        return set(re.findall(r"\w+", (s or '').lower()))
    sa, sb = tok(a), tok(b)
    if not sa and not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return (inter / union) if union else 0.0

def hybrid_merge(vector_hits, bm25_hits, alpha=0.6, top_k=8, dedupe_threshold=0.75, query_text: str | None = None):
    v_scores = _normalize([h.get('score', 0.0) for h in vector_hits])
    b_scores = _normalize([h.get('score', 0.0) for h in bm25_hits])
    combined = []
    for h, ns in zip(vector_hits, v_scores):
        combined.append({'id': h.get('id', 'vec'), 'metadata': h.get('metadata', {}), 'hybrid': alpha * float(ns)})
    for h, ns in zip(bm25_hits, b_scores):
        combined.append({'id': h.get('id', 'bm25'), 'metadata': h.get('metadata', {}), 'hybrid': (1 - alpha) * float(ns)})
    # Boosts por tipo de fuente
    try:
        cases_boost = float(os.getenv('CASES_BOOST', '0.3'))
        tables_boost = float(os.getenv('TABLES_BOOST', '0.25'))
        trend_boost_max = float(os.getenv('TREND_BOOST_MAX', '0.35'))
        trend_enabled = (os.getenv('TREND_ENABLE', '1').strip().lower() in {"1","true","yes","on"})
    except Exception:
        cases_boost, tables_boost, trend_boost_max, trend_enabled = 0.3, 0.25, 0.35, True
    for it in combined:
        try:
            md = it.get('metadata', {}) or {}
            if md.get('source') == 'table':
                it['hybrid'] += tables_boost
            if md.get('source') == 'case' and (md.get('evidence_type') or ''):
                it['hybrid'] += cases_boost
            if trend_enabled and trend_boost_max > 0:
                tb = trend_boost(md, query_text)
                if tb > 0:
                    it['hybrid'] += trend_boost_max * float(tb)
        except Exception:
            pass
    combined.sort(key=lambda x: x['hybrid'], reverse=True)
    deduped = []
    sigs = []
    for it in combined:
        sig = _sig(it.get('metadata', {}), it.get('id', ''))
        if any(_jaccard(sig, s) >= dedupe_threshold for s in sigs):
            continue
        deduped.append(it)
        sigs.append(sig)
    return deduped[:top_k]

def lexical_rerank(query_text: str, documents: list):
    try:
        from rank_bm25 import BM25Okapi
    except Exception:
        return documents
    texts = []
    for d in documents:
        m = d.get('metadata', {})
        txt = m.get('text') or ''
        if not txt:
            txt = (m.get('prev_window', '') or '') + ' ' + (m.get('next_window', '') or '')
        texts.append(txt)
    if not texts:
        return documents
    tokenized = [t.split() for t in texts]
    bm = BM25Okapi(tokenized)
    scores = bm.get_scores(query_text.split())
    paired = list(zip(documents, scores))
    paired.sort(key=lambda x: x[1], reverse=True)
    return [p[0] for p in paired]

def rewrite_query(q: str) -> str:
    base = f"Manual técnico Higer {q}".strip()
    brand = _env('BRAND_NAME', 'Higer')
    extra = []
    # Solo añade modelo si el texto lo menciona (para no filtrar de más)
    if brand and brand.lower() not in base.lower():
        extra.append(brand)
    if re.search(r"\b(h6c|h5c|klq6540)\b", q, re.IGNORECASE):
        extra.append(re.findall(r"\b(h6c|h5c|klq6540)\b", q, re.IGNORECASE)[0])
    # OEM y partes
    if re.search(r"\b[A-Z0-9]{2,}(?:[\-–][A-Z0-9]{2,})+\b", q) or ('oem' in q.lower()):
        extra.extend(["número de parte", "OEM"]) 
    # Códigos OIL xxx → reforzar términos de aceite
    if re.search(r"\boil\s*\d{3}\b", q, re.IGNORECASE):
        extra.extend(["aceite", "nivel de aceite", "baja presión de aceite", "testigo de aceite", "mantenimiento de aceite", "fuga de aceite"])
    return (base + ' ' + ' '.join(dict.fromkeys(extra))).strip()

def build_context(docs: list) -> str:
    parts = []
    for i, d in enumerate(docs, start=1):
        m = d.get('metadata', {})
        parts.append(
            (
                f"--- Fuente {i} (Pagina: {m.get('page_label','N/A')}) ---\n"
                f"Contexto Previo: ...{(m.get('prev_window','') or '')}\n"
                f"Contenido Principal: {(m.get('text','') or '')}\n"
                f"Contexto Posterior: {(m.get('next_window','') or '')}...\n"
                f"------------------------------------"
            )
        )
    return "\n".join(parts)

def system_prompt_hybrid(
    severity: str = "normal",
    categories: Sequence[str] | None = None,
    missing_evidence: Sequence[str] | None = None,
) -> str:
    sev = (severity or "normal").strip().lower()
    cats = [c.strip().lower() for c in (categories or []) if c]
    pending = [p.strip() for p in (missing_evidence or []) if p]

    sev_rules = {
        "critical": (
            "Severidad crítica: usa plantilla CRÍTICA → abre con '⚠️ ALERTA:' + síntoma, ordena detener la vagoneta de inmediato, luego 'Pasos inmediatos' con 2-3 acciones seguras y cierra pidiendo subirla a taller/grúa y confirmación de recibido."
        ),
        "urgent": (
            "Severidad urgente: plantilla URGENTE → arranca con '⏱ Urgente:' + resumen, indica si puede circular solo para llegar al taller, agenda seguimiento en las próximas horas y pide confirmar disponibilidad."
        ),
        "normal": (
            "Severidad normal: plantilla NORMAL → comienza con 'Resumen:', guía con tono empático paso a paso y ofrece seguimiento opcional."
        ),
    }
    sev_text = sev_rules.get(sev, sev_rules["normal"])

    cat_rules = {
        "brakes": "Categoría frenos: incluye chequeo de fugas, prueba de pedal, torque de birlos y recalca distancia segura.",
        "oil": "Categoría aceite: verifica nivel, fugas visibles, presión en marcha y condición del filtro.",
        "cooling": "Categoría enfriamiento: advierte riesgo de sobrecalentamiento, revisa fugas, purga de aire y estado del ventilador.",
        "electrical": "Categoría eléctrica: revisa bornes, fusibles, tierra y descarta corto antes de sugerir reemplazos.",
        "fuel": "Categoría combustible: inspecciona líneas/fugas, filtro y presión de riel; sugiere purga si aplica.",
        "transmission": "Categoría transmisión: enfatiza niveles, fugas, calibración de cambios y prueba corta sin carga.",
        "tires": "Categoría llantas: valida presión, desgaste irregular y reapriete de birlos en cruz.",
    }
    cat_text_parts = []
    for cat in cats:
        rule = cat_rules.get(cat)
        if rule and rule not in cat_text_parts:
            cat_text_parts.append(rule)
    cat_text = ". ".join(cat_text_parts) if cat_text_parts else "Aterriza los pasos al sistema detectado usando vocabulario sencillo."

    pending_text = ""
    if pending:
        pending_fmt = ", ".join(dict.fromkeys(pending) or [])
        pending_text = (
            f"Evidencia pendiente: enumera {pending_fmt} antes del cierre, explica por qué se necesita cada ítem y pide que la comparta para poder cerrar el caso."
        )

    citation_text = (
        "Cita fuentes por tipo: usa [Manual pág X] únicamente para el manual técnico, [Catálogo ref X] para tablas OEM y [Caso #ID] cuando cites antecedentes. No mezcles etiquetas ni inventes citas."
    )

    parts = [
        "Eres un técnico de postventa Higer hablando con transportistas.",
        "Usa tono cercano y práctico (no robótico), habla de tú (tuteo) y cuando te refieras al vehículo di 'vagoneta'. Fundamenta tus respuestas SOLO en el contexto dado.",
        "Estilo: frases cortas y coloquiales; agradece la foto/nota con naturalidad (p. ej. 'gracias por la foto/nota'); confirma lo leído/escuchado con '¿correcto?'. Haz 1–2 preguntas por turno (no cuestionarios).",
        "Evita formularios rígidos ('he recibido la evidencia adjunta'); prefiere 'gracias por la foto', 'ok, veo…'.",
        "Si el modelo exacto no cambia el procedimiento (H6C/H5C comparten mecánica base), avanza con pasos generales seguros y al final pide el dato si hace falta.",
        "Si detectas síntomas críticos (frenos sin respuesta, baja presión de aceite, sobrecalentamiento, humo/fuego), agrega una Alerta de seguridad clara y pide detener la vagoneta.",
        sev_text,
        cat_text,
        pending_text,
        citation_text,
        "Formato base: 1) resumen/alerta según plantilla de severidad, 2) pasos numerados concretos, 3) bloque 'Evidencia pendiente' si aplica, 4) cita de fuentes y 5) cierre con pregunta natural ('¿Seguimos?', '¿Te late?').",
        "Si existe bloque 'Evidencia detectada', PRIORIZA esa evidencia por encima de la 'Conversación Reciente'. No repitas respuestas anteriores: responde a lo más reciente y pide confirmación/precisiones cortas si hace falta.",
        "Si el modo de entrada es 'solo_imagenes', NO uses la historia: confirma lo leído (odómetro/VIN/placa) y pide al usuario el síntoma principal en 1–2 líneas (cuándo empezó, intermitente/constante, testigos).",
        "Si el modo es 'solo_audio', basa tu respuesta en la transcripción (resumen 1 línea), confirma lo esencial (síntoma, testigos, si puede circular) y pide el dato mínimo para avanzar. Mantén la historia al mínimo.",
        "Incluye: resumen breve, pasos numerados, fuente/páginas si hay y, si NO es crítico, cierra con una frase corta y natural (p. ej., '¿Seguimos?', '¿Te late?', '¿Te va?').",
    ]
    return " ".join([p for p in parts if p])

@app.post("/query_hybrid", response_model=QueryResponse)
def query_hybrid(request: QueryRequest):
    try:
        _t0 = time.perf_counter()
        # 1) Preparar consulta
        rewritten = rewrite_query(request.question)
        emb_model = _embeddings_model()
        # Check dimension para el endpoint híbrido
        try:
            idx_dim = _index_dimension(INDEX_NAME)
            if idx_dim is not None and _embeddings_dim(emb_model) is not None and int(idx_dim) != int(_embeddings_dim(emb_model)):
                logger.warning(f"/query_hybrid: dimensión índice {idx_dim} != embeddings {_embeddings_dim(emb_model)}")
        except Exception:
            pass
        embeddings = OpenAIEmbeddings(model=emb_model)
        cache_key_main = hashlib.sha256((rewritten or '').encode('utf-8', 'ignore')).hexdigest()
        q_vec = _embedding_cache_get(cache_key_main)
        if q_vec is None:
            q_vec = embeddings.embed_query(rewritten)
            _embedding_cache_set(cache_key_main, q_vec)

        # 2) Recuperación vectorial + BM25
        vec_hits = retrieve_from_pinecone(q_vec, top_k=min(16, int(_env('HYBRID_TOP_K', '8')) * 2))
        # Diagram intent: consultar índice de diagramas
        try:
            if (os.getenv('USE_DIAGRAMS','1').strip().lower() in {"1","true","yes"}) and is_diagram_intent(rewritten):
                d_topk = int(_env('DIAGRAM_TOP_K','6') or '6')
                d_hits = retrieve_diagrams(q_vec, top_k=d_topk)
                if d_hits:
                    vec_hits = (vec_hits or []) + d_hits
        except Exception:
            pass
        # Casos: índice derivado de chats/evidencias
        try:
            case_filter = {}
            try:
                from . import storage as _st
                sig = _st.extract_signals(request.question)
                cat = (sig.get('category') if isinstance(sig, dict) else None)
                if cat:
                    case_filter['category'] = cat
            except Exception:
                pass
            # Infiere evidencia y prueba filtro específico primero; si no hay matches, cae a categoría
            ev = _infer_evidence_type(request.question)
            cache_key_cases = hashlib.sha256((request.question or '').encode('utf-8', 'ignore')).hexdigest()
            q_vec_cases = _embedding_cache_get(cache_key_cases)
            if q_vec_cases is None:
                q_vec_cases = embeddings.embed_query(request.question)
                _embedding_cache_set(cache_key_cases, q_vec_cases)
            c_hits = []
            if ev:
                f_ev = dict(case_filter)
                # Pinecone: usar $in para compatibilidad
                f_ev['evidence_type'] = {'$in': [ev]}
                c_hits = retrieve_cases(q_vec_cases, top_k=min(8, int(_env('HYBRID_TOP_K','8'))), filters=f_ev)
            if not c_hits:
                c_hits = retrieve_cases(q_vec_cases, top_k=min(8, int(_env('HYBRID_TOP_K','8'))), filters=(case_filter or None))
            if c_hits:
                vec_hits = (vec_hits or []) + c_hits
        except Exception:
            pass
        bm25_pack = load_bm25_pack()
        bm_hits = bm25_top(rewritten, bm25_pack, top_k=min(16, int(_env('HYBRID_TOP_K', '8')) * 2))
        alpha = float(_env('HYBRID_ALPHA', '0.6'))
        dedupe = float(_env('HYBRID_DEDUPE_THRESHOLD', '0.75'))
        top_k = int(_env('HYBRID_TOP_K', '8'))
        merged = hybrid_merge(vec_hits, bm25_hits=bm_hits, alpha=alpha, top_k=top_k, dedupe_threshold=dedupe, query_text=request.question)
        reranked = lexical_rerank(rewritten, merged)
        reranked = _prepend_catalog_hits(request.question, reranked)
        # Case-first si evidencia coincide: mueve al frente resultados 'case' con evidence_type inferido
        try:
            strict = (os.getenv('CASE_FIRST_STRICT','1').strip().lower() in {"1","true","yes","on"})
            if strict:
                evq = _infer_evidence_type(request.question)
                if evq:
                    matched = []
                    rest = []
                    for d in reranked:
                        md = d.get('metadata', {}) or {}
                        if (md.get('source') == 'case') and (str(md.get('evidence_type') or '').lower() == evq.lower()):
                            matched.append(d)
                        else:
                            rest.append(d)
                    if matched:
                        reranked = matched + rest
        except Exception:
            pass

        # 3) Construir contexto y conversación previa (si hay contacto/meta)
        history_text = ""
        try:
            contact = None
            if request.meta and isinstance(request.meta, dict):
                contact = request.meta.get('contact') or request.meta.get('from') or request.meta.get('session_id')
            if contact:
                from . import storage as _st
                hist = _st.get_conversation(str(contact), limit=_int_env('HISTORY_LIMIT', 8))
                if hist:
                    lines = ["### Conversación Reciente:"]
                    for m in hist:
                        role = 'Usuario' if m.get('role') == 'user' else 'Agente'
                        txt = (m.get('text') or '').replace('\n', ' ')
                        lines.append(f"- {role}: {txt}")
                    history_text = "\n" + "\n".join(lines) + "\n"
        except Exception:
            history_text = ""

        # 4) Llamar LLM con contexto + historia + resumen de caso
        context = build_context(reranked)
        llm = ChatOpenAI(model=os.getenv("LLM_MODEL", "gpt-4o"), temperature=0.1)
        from langchain_core.messages import SystemMessage, HumanMessage
        scenario_txt = ""
        agg = {}
        agg_severity = 'normal'
        agg_categories: list[str] = []
        try:
            hx_user_texts = [m.get('text') for m in (hist or []) if m.get('role') == 'user'] if 'hist' in locals() else []
            agg = _aggregate_signals(hx_user_texts + [request.question]) or {}
            agg_severity = (agg.get('severity') or 'normal') if isinstance(agg, dict) else 'normal'
            agg_categories = [c for c in (agg.get('categories') or []) if c] if isinstance(agg, dict) else []
            cats_txt = ", ".join(agg_categories) or 'general'
            scenario_txt = f"### Resumen del Caso (inferido):\n- Severidad: {agg_severity}\n- Categorías: {cats_txt}\n- Asume la misma vagoneta que mensajes previos salvo que se indique lo contrario.\n\n"
        except Exception:
            agg_severity = 'normal'
            agg_categories = []
        pending_evidence: list[str] = []
        try:
            contact_val = None
            if request.meta and isinstance(request.meta, dict):
                contact_val = request.meta.get('contact') or request.meta.get('from') or request.meta.get('session_id')
            if contact_val:
                from . import storage as _st
                case_ctx = _st.get_or_create_case(str(contact_val))
                sig_case = _st.extract_signals(request.question)
                cat_hint = (sig_case.get('category') if isinstance(sig_case, dict) else None)
                pb = _st.load_playbooks()
                ask = (pb.get(cat_hint or (agg_categories[0] if agg_categories else 'general')) or {}).get('ask_for') or []
                provided = set((case_ctx or {}).get('provided') or [])
                pending_evidence = [item for item in ask if item not in provided]
        except Exception:
            pending_evidence = []
        human = (
            f"### Contexto de Manuales Técnicos:\n{context}\n\n" + history_text + scenario_txt +
            f"### Pregunta del Usuario:\n{request.question}"
        )
        response = llm.invoke([
            SystemMessage(content=system_prompt_hybrid(
                severity=agg_severity,
                categories=agg_categories,
                missing_evidence=pending_evidence,
            )),
            HumanMessage(content=human),
        ])
        answer_text = response.content if hasattr(response, 'content') else str(response)

        # 4) Formatear fuentes
        def trim_tokens(text: str, max_tokens: int = 60) -> str:
            toks = (text or '').split()
            if len(toks) <= max_tokens:
                return ' '.join(toks)
            return ' '.join(toks[:max_tokens])

        sources_unsorted = []
        for d in reranked[:5]:
            m = d.get('metadata', {})
            page_label = m.get('page_label', 'N/A')
            snippet = trim_tokens((m.get('text','') or '').replace('\n', ' '), 60)
            page_number = None
            try:
                page_number = int(str(page_label))
            except Exception:
                pass
            if page_number is None:
                rn = roman_to_int(page_label)
                if rn is not None:
                    page_number = rn
            sources_unsorted.append({
                'page_label': page_label,
                'page_number': page_number,
                'snippet': snippet,
                'source': m.get('source'),
                'doc_id': m.get('doc_id'),
                'chunk_index': m.get('chunk_index')
            })

        import re as _re
        def natural_key(text: str):
            parts = _re.split(r'(\d+)', str(text))
            key = []
            for p in parts:
                if p.isdigit():
                    key.append((1, int(p)))
                else:
                    key.append((0, p.lower()))
            return key
        def page_order_key(src):
            p = src.get('page_label')
            try:
                num = int(str(p))
                return (0, num)
            except Exception:
                pass
            r = roman_to_int(p)
            if r is not None:
                return (1, r)
            return (2, natural_key(p))

        sources = sorted(sources_unsorted, key=page_order_key)

        try:
            from . import storage as _st
            sig = _st.extract_signals(request.question)
        except Exception:
            sig = {}
        meta_for_len = dict(getattr(request, 'meta', {}) or {})
        if isinstance(sig, dict):
            meta_for_len.setdefault('severity', sig.get('severity'))
        effective_limit = _effective_limit(meta_for_len)
        final_answer = _summarize_to_limit(answer_text, request.question, sources, effective_limit)
        # 5) Integrar Case/Playbook si hay contacto
        enriched_answer = final_answer
        citation_block = _format_citations(reranked)
        if citation_block:
            enriched_answer += "\n\nFuentes:\n" + citation_block
        missing_items: list[str] = []
        try:
            equiv_extras: list[str] = []
            if 'user_text' in locals() and (user_text or '').strip():
                equiv_extras.append(user_text)
            part_cls = locals().get('part_class_list')
            if isinstance(part_cls, list):
                for cls in part_cls:
                    if isinstance(cls, dict):
                        guess = (cls.get('part_guess') or '').strip()
                        if guess:
                            equiv_extras.append(guess)
                        for syn in cls.get('synonyms') or []:
                            if isinstance(syn, str) and syn.strip():
                                equiv_extras.append(syn)
            force_equiv = _wants_equivalences([request.question] + equiv_extras)
            suggestions = _collect_equivalence_suggestions(request.question, equiv_extras, force=force_equiv)
            if suggestions:
                enriched_answer += "\n\n" + _format_equivalences_block(suggestions)
        except Exception:
            pass
        try:
            contact_val = None
            channel_val = None
            if request.meta and isinstance(request.meta, dict):
                contact_val = request.meta.get('contact') or request.meta.get('from') or request.meta.get('session_id')
                channel_val = request.meta.get('channel')
            if contact_val:
                from . import storage as _st
                # señales del turno actual
                sig2 = _st.extract_signals(request.question)
                cat2 = (sig2.get('category') if isinstance(sig2, dict) else None) or 'general'
                sev2 = (sig2.get('severity') if isinstance(sig2, dict) else None) or 'normal'
                # actualizar caso
                case = _st.get_or_create_case(str(contact_val))
                # agregar required del playbook
                pb = _st.load_playbooks()
                ask = (pb.get(cat2) or {}).get('ask_for') or []
                _st.add_required(str(contact_val), ask)
                # combinar categorías y severidad (mantener la peor)
                cats_existing = case.get('categories') or []
                if cat2 not in cats_existing:
                    cats_existing.append(cat2)
                worst = case.get('severity', 'normal')
                order = {'normal': 0, 'urgent': 1, 'critical': 2}
                if order.get(sev2, 0) > order.get(worst, 0):
                    worst = sev2
                case = _st.update_case(str(contact_val), {'categories': cats_existing, 'severity': worst})
                # construir header + pendientes
                case_header = f"Caso {case.get('id')} — Severidad: {case.get('severity','normal')}"
                provided = set(case.get('provided') or [])
                missing_items = [i for i in ask if i not in provided]
                if case_header:
                    enriched_answer = f"{case_header}\n\n" + enriched_answer
        except Exception:
            pass

        if missing_items:
            enriched_answer += "\n\nPendiente (evidencia mínima): " + ", ".join(missing_items[:5])

        resp = QueryResponse(question=request.question, answer=enriched_answer, sources=sources)

        # Log de consulta API (/query_hybrid)
        try:
            from . import storage
            latency_ms = (time.perf_counter() - _t0) * 1000.0
            response_bytes = len((resp.answer or '').encode('utf-8'))
            storage.log_event(
                kind="api_query",
                payload={
                    "endpoint": "/query_hybrid",
                    "question": request.question,
                    "meta": request.meta or {},
                    "from": contact,
                    "channel": (request.meta or {}).get('channel') if isinstance(request.meta, dict) else None,
                    "classification": storage.classify(request.question),
                    "signals": storage.extract_signals(request.question),
                    "answer": resp.answer,
                    "sources": resp.sources,
                    "latency_ms": latency_ms,
                    "response_bytes": response_bytes,
                }
            )
        except Exception:
            pass

        return resp
    except Exception as e:
        logger.error(f"Error in /query_hybrid: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error.")


@app.get("/health")
def health():
    """Health check extendido con diagnóstico básico de configuración."""
    idx = os.getenv("PINECONE_INDEX", "ssot-higer")
    emb_model = _embeddings_model()
    expected_dim = _embeddings_dim(emb_model)
    actual_dim = None
    try:
        actual_dim = _index_dimension(idx)
    except Exception:
        actual_dim = None
    bm25 = os.getenv("BM25_INDEX_FILE", "bm25_index.pkl")
    bm25_exists = False
    try:
        candidates = [bm25, os.path.join(os.path.dirname(__file__), bm25)]
        for p in candidates:
            if p and os.path.exists(p):
                bm25_exists = True
                break
    except Exception:
        pass
    diag_idx = os.getenv("PINECONE_INDEX_DIAGRAMS", "ssot-higer-diagramas-elect")
    use_diagrams = (os.getenv('USE_DIAGRAMS','1').strip().lower() in {"1","true","yes"})
    diag_dim = None
    if use_diagrams:
        try:
            diag_dim = _index_dimension(diag_idx)
        except Exception:
            pass
    return {
        "status": "ok",
        "initialized": qa_chain is not None,
        "llm_model": os.getenv("LLM_MODEL", "gpt-4o"),
        "ocr_model": os.getenv("OCR_MODEL", "gpt-4o-mini"),
        "asr_model": os.getenv("ASR_MODEL", "whisper-1"),
        "embeddings": {"model": emb_model, "expected_dim": expected_dim, "index": idx, "actual_dim": actual_dim},
        "bm25_present": bm25_exists,
        "diagrams": {"enabled": use_diagrams, "index": diag_idx if use_diagrams else None, "actual_dim": diag_dim if use_diagrams else None},
    }


@app.get("/version")
def version():
    """Metadata básica de versión/build para trazabilidad."""
    return {
        "app": "Higer RAG API",
        "version": os.getenv("APP_VERSION", "0.1.0"),
        "build_sha": os.getenv("BUILD_SHA") or None,
        "build_time": os.getenv("BUILD_TIME") or None,
        "python": platform.python_version(),
    }


# -------------------- Admin / Maintenance (protected) --------------------
def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1","true","yes","on"}

def _admin_allowed(request: Request) -> bool:
    if not _truthy(os.getenv("MAINTENANCE_ENABLE", "0")):
        return False
    # local-only unless explicitly allowed
    allow_nonlocal = _truthy(os.getenv("ALLOW_NONLOCAL_ADMIN", "0"))
    try:
        host = (request.client.host if request and request.client else None) or ""
    except Exception:
        host = ""
    if not allow_nonlocal and host not in {"127.0.0.1", "::1", "localhost"}:
        return False
    # token via Authorization: Bearer or query param token
    expected = os.getenv("ADMIN_TOKEN", "").strip()
    if not expected:
        return False
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    bearer = None
    if auth and auth.lower().startswith("bearer "):
        bearer = auth.split(" ", 1)[1].strip()
    token = request.query_params.get("token") or bearer
    return token == expected

def _index_status_dict(index_name: str) -> dict:
    d = {"index": index_name, "embeddings_model": _embeddings_model(), "expected_dim": _embeddings_dim(_embeddings_model())}
    try:
        from pinecone import Pinecone as _PC  # type: ignore
        _pc = _PC(api_key=os.getenv("PINECONE_API_KEY"))
        desc = _pc.describe_index(index_name)
        stats = None
        try:
            stats = _pc.Index(index_name).describe_index_stats()
        except Exception:
            pass
        dim = (desc.get('dimension') if isinstance(desc, dict) else getattr(desc, 'dimension', None))
        d.update({"actual_dim": dim, "describe": desc, "stats": stats})
        # list other indexes with same prefix
        try:
            names = list(_pc.list_indexes().names())
            prefix = index_name.split('-')[0]
            siblings = [n for n in names if n.startswith(prefix)]
            d["siblings"] = siblings
        except Exception:
            pass
    except Exception:
        d["error"] = "pinecone_client_unavailable"
    return d

# --------- Pinecone alias helpers (best-effort; fallback si no soportado) ---------
def _pc_client():
    from pinecone import Pinecone as _PC  # type: ignore
    return _PC(api_key=os.getenv("PINECONE_API_KEY"))

def _alias_supported(pc) -> bool:
    try:
        names = {n.lower() for n in dir(pc)}
        return any('alias' in n for n in names)
    except Exception:
        return False

def _alias_set(alias_name: str, target_index: str) -> dict:
    pc = _pc_client()
    if not _alias_supported(pc):
        return {"ok": False, "reason": "alias_api_not_supported"}
    # probar varios métodos posibles del cliente
    attempts = []
    # common forms observed across client versions
    for method in [
        'configure_index_alias', 'create_index_alias', 'upsert_index_alias',
        'configure_alias', 'create_alias', 'upsert_alias']:
        if hasattr(pc, method):
            attempts.append(method)
    for m in attempts:
        try:
            fn = getattr(pc, m)
            # intentar llamadas con kwargs
            try:
                fn(name=alias_name, target=target_index)
                return {"ok": True, "method": m}
            except TypeError:
                pass
            # intentar con dict payload
            try:
                fn({"name": alias_name, "target": target_index})
                return {"ok": True, "method": m}
            except TypeError:
                pass
        except Exception as e:
            last = str(e)
    return {"ok": False, "reason": "alias_calls_failed", "methods_tried": attempts}

@app.get("/admin/index/status")
def admin_index_status(request: Request):
    if not _admin_allowed(request):
        raise HTTPException(status_code=403, detail="forbidden")
    name = os.getenv("PINECONE_INDEX", "ssot-higer")
    return _index_status_dict(name)

@app.post("/admin/index/rotate")
def admin_index_rotate(request: Request, include_diagrams: bool = False, update_env: bool = False, dry_run: bool = True, confirm: str = "", use_alias: bool = False):
    """Crea un índice nuevo con la dimensión correcta y (opcional) reingesta.
    Seguridad: requiere MAINTENANCE_ENABLE=1 y ADMIN_TOKEN válido.
    Param confirm debe ser "ROTATE" para ejecutar.
    """
    if not _admin_allowed(request):
        raise HTTPException(status_code=403, detail="forbidden")
    if not dry_run and confirm != "ROTATE":
        raise HTTPException(status_code=400, detail="confirm requerido")
    base = os.getenv("PINECONE_INDEX", "ssot-higer")
    emb = _embeddings_model()
    dim = _embeddings_dim(emb)
    ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    new_index = f"{base}-{ts}-{dim}"
    plan = {
        "base_index": base,
        "new_index": new_index,
        "embeddings_model": emb,
        "expected_dim": dim,
        "dry_run": dry_run,
        "include_diagrams": include_diagrams,
        "update_env": update_env,
        "use_alias": use_alias,
    }
    if dry_run:
        plan["status"] = "planned"
        return plan

    # Crear índice nuevo
    try:
        from pinecone import Pinecone as _PC, ServerlessSpec as _Spec  # type: ignore
        region, cloud = os.getenv("PINECONE_ENV", "us-east-1-aws").rsplit('-', 1)
        _pc = _PC(api_key=os.getenv("PINECONE_API_KEY"))
        if new_index in _pc.list_indexes().names():
            return {**plan, "status": "exists", "note": "el índice nuevo ya existe"}
        _pc.create_index(name=new_index, dimension=dim, metric="cosine", spec=_Spec(cloud=cloud, region=region))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al crear índice: {e}")

    # Ejecutar ingesta principal (proceso separado para no bloquear el worker)
    env = os.environ.copy()
    env["PINECONE_INDEX"] = new_index
    procs = []
    try:
        p = subprocess.Popen(["python3", "-m", "app.ingesta_unificada", "--ocr"], env=env)
        procs.append(("ingesta", p.pid))
        if include_diagrams:
            p2 = subprocess.Popen(["python3", os.path.join(os.path.dirname(__file__), "ingesta_diagramas.py")], env=env)
            procs.append(("ingesta_diagramas", p2.pid))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error lanzando ingestas: {e}")

    # Opcional: actualizar alias nativo o .env local
    updated_env = False
    alias_switched = None
    if use_alias:
        alias_name = os.getenv("PINECONE_INDEX_ALIAS")
        if not alias_name:
            return {**plan, "status": "started", "processes": procs, "env_updated": False, "alias": {"ok": False, "reason": "missing_env_PINECONE_INDEX_ALIAS"}, "next": "configura PINECONE_INDEX_ALIAS o usa update_env=true"}
        try:
            alias_switched = _alias_set(alias_name, new_index)
        except Exception as e:
            alias_switched = {"ok": False, "error": str(e)}
    elif update_env:
        try:
            env_path = os.path.join(os.path.dirname(__file__), ".env")
            if os.path.exists(env_path):
                lines = []
                with open(env_path, 'r', encoding='utf-8', errors='ignore') as f:
                    lines = f.readlines()
                found = False
                for i, ln in enumerate(lines):
                    if ln.strip().startswith("PINECONE_INDEX="):
                        lines[i] = f"PINECONE_INDEX=\"{new_index}\"\n"
                        found = True
                        break
                if not found:
                    lines.append(f"\nPINECONE_INDEX=\"{new_index}\"\n")
                with open(env_path, 'w', encoding='utf-8') as f:
                    f.writelines(lines)
                updated_env = True
        except Exception:
            updated_env = False
    return {**plan, "status": "started", "processes": procs, "env_updated": updated_env, "alias": alias_switched, "next": "espera a que ingesta termine y luego make restart"}


def _serialize_value(obj):
    if isinstance(obj, dict):
        return {k: _serialize_value(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize_value(v) for v in obj]
    if hasattr(obj, "isoformat"):
        try:
            return obj.isoformat()
        except Exception:
            return str(obj)
    return obj


@app.get("/admin/cases/{contact}")
def admin_case_detail(contact: str, request: Request):
    if not _admin_allowed(request):
        raise HTTPException(status_code=403, detail="forbidden")
    contact = (contact or "").strip()
    if not contact:
        raise HTTPException(status_code=400, detail="contact requerido")

    try:
        from . import storage as _st
        case_state = _st.get_case_state(contact)
    except Exception:
        case_state = None
    if not case_state:
        return {"contact": contact, "found": False}

    try:
        requirements = _st.case_requirements_snapshot(contact)
    except Exception:
        requirements = {"required": [], "provided": [], "missing": []}

    db_case = None
    attachments_db = []
    db_case_id = case_state.get('db_case_id')
    if db_case_id:
        try:
            from . import db_cases as _dbc
            raw_case = _dbc.get_case(db_case_id)
            db_case = _serialize_value(raw_case) if raw_case else None
            attachments_db = _serialize_value(_dbc.list_attachments(db_case_id))
        except Exception:
            db_case = None
            attachments_db = []

    summary = {
        "contact": contact,
        "found": True,
        "case": {
            "id": case_state.get('id'),
            "db_case_id": db_case_id,
            "severity": case_state.get('severity'),
            "categories": case_state.get('categories'),
            "status": case_state.get('status'),
            "opened_at": case_state.get('opened_at'),
            "last_updated": case_state.get('last_updated'),
            "required": requirements.get('required', []),
            "provided": requirements.get('provided', []),
            "missing": requirements.get('missing', []),
            "required_details": requirements.get('required_details', []),
            "provided_details": requirements.get('provided_details', []),
            "attachments_local": case_state.get('attachments') or [],
            "notes": case_state.get('notes'),
        },
        "db_case": db_case,
        "attachments_db": attachments_db,
    }
    return summary


@app.get("/admin/cases")
def admin_cases_overview(request: Request):
    if not _admin_allowed(request):
        raise HTTPException(status_code=403, detail="forbidden")
    try:
        from . import storage as _st
        cases = _st.list_cases()
    except Exception:
        cases = []
    return {"count": len(cases), "cases": cases}

# -------------------- Métricas estilo Prometheus --------------------
_metrics_enabled = (os.getenv('METRICS_ENABLE', '1').strip().lower() in {"1","true","yes"})
_mx_lock = threading.Lock()
_mx_req_total: dict[tuple, float] = {}
_mx_lat_sum: dict[tuple, float] = {}
_mx_lat_count: dict[tuple, float] = {}

def _labels_tuple(method: str, path: str, status: int) -> tuple:
    return (method or '', path or '', int(status))

@app.middleware("http")
async def _metrics_middleware(request: Request, call_next):
    if not _metrics_enabled:
        return await call_next(request)
    start = time.perf_counter()
    try:
        response = await call_next(request)
        status = getattr(response, 'status_code', 200)
    except Exception:
        status = 500
        raise
    finally:
        dur = max(0.0, time.perf_counter() - start)
        key = _labels_tuple(request.method, request.url.path, status)
        key2 = (request.method, request.url.path)
        with _mx_lock:
            _mx_req_total[key] = _mx_req_total.get(key, 0.0) + 1.0
            _mx_lat_sum[key2] = _mx_lat_sum.get(key2, 0.0) + dur
            _mx_lat_count[key2] = _mx_lat_count.get(key2, 0.0) + 1.0
    return response

@app.get("/metrics")
def metrics():
    if not _metrics_enabled:
        return Response(content="# metrics disabled\n", media_type="text/plain; version=0.0.4")
    lines = []
    lines.append("# HELP http_requests_total Total HTTP requests")
    lines.append("# TYPE http_requests_total counter")
    with _mx_lock:
        for (method, path, status), val in _mx_req_total.items():
            lines.append(f'http_requests_total{{method="{method}",path="{path}",status_code="{status}"}} {val:.0f}')
        lines.append("# HELP http_request_duration_seconds Request durations (sum & count)")
        lines.append("# TYPE http_request_duration_seconds summary")
        for (method, path), s in _mx_lat_sum.items():
            c = _mx_lat_count.get((method, path), 0.0)
            lines.append(f'http_request_duration_seconds_sum{{method="{method}",path="{path}"}} {s:.6f}')
            lines.append(f'http_request_duration_seconds_count{{method="{method}",path="{path}"}} {c:.0f}')
    body = "\n".join(lines) + "\n"
    return Response(content=body, media_type="text/plain; version=0.0.4")


@app.post("/twilio/whatsapp")
async def twilio_whatsapp(
    request: Request,
    Body: str = Form(...),
    From: str | None = Form(None),
    NumMedia: str = Form("0"),
    MessageSid: str | None = Form(None),
    ProfileName: str | None = Form(None),
    MediaUrl0: str | None = Form(None), MediaContentType0: str | None = Form(None),
    MediaUrl1: str | None = Form(None), MediaContentType1: str | None = Form(None),
    MediaUrl2: str | None = Form(None), MediaContentType2: str | None = Form(None),
    MediaUrl3: str | None = Form(None), MediaContentType3: str | None = Form(None),
    MediaUrl4: str | None = Form(None), MediaContentType4: str | None = Form(None),
    MediaUrl5: str | None = Form(None), MediaContentType5: str | None = Form(None),
    MediaUrl6: str | None = Form(None), MediaContentType6: str | None = Form(None),
    MediaUrl7: str | None = Form(None), MediaContentType7: str | None = Form(None),
    MediaUrl8: str | None = Form(None), MediaContentType8: str | None = Form(None),
    MediaUrl9: str | None = Form(None), MediaContentType9: str | None = Form(None),
):
    """
    Webhook simple para Twilio WhatsApp.
    - Espera form-urlencoded con campos `Body` y opcionalmente `From`.
    - Consulta el RAG y responde en TwiML.
    """
    _t0 = time.perf_counter()
    await _enforce_twilio_signature(request, "/twilio/whatsapp")
    # Log de entrada WhatsApp
    try:
        from . import storage as _st
        _st.log_event(
            kind="whatsapp_in",
            payload={
                "from": From,
                "text": Body,
                "sid": MessageSid,
                "classification": _st.classify(Body),
                "signals": _st.extract_signals(Body),
            }
        )
    except Exception:
        pass

    # Idempotencia: si ya respondimos este MessageSid, devolver la misma respuesta
    try:
        ttl = _int_env('TWILIO_DEDUP_TTL_SEC', 600)
        prev = _sid_get_answer(MessageSid or '', ttl)
        if prev:
            twiml_prev = f"""
            <Response>
              <Message>{prev}</Message>
            </Response>
            """.strip()
            return Response(content=twiml_prev, media_type="application/xml")
    except Exception:
        pass

    answer = "El asistente no está inicializado. Intenta más tarde."
    # Permitir pipeline si hay texto o si hay medios (audio/imagen)
    if Body or (NumMedia and NumMedia.isdigit() and int(NumMedia) > 0):
        try:
            # Usar pipeline híbrido con memoria de conversación
            question_text = (Body or '').strip()
            rewritten = rewrite_query(question_text)
            emb_model = _embeddings_model()
            embeddings = OpenAIEmbeddings(model=emb_model)
            cache_key_main = hashlib.sha256((rewritten or '').encode('utf-8', 'ignore')).hexdigest()
            sid_key = f"sid:{MessageSid}" if MessageSid else None
            q_vec = None
            for key in [k for k in [sid_key, cache_key_main] if k]:
                q_vec = _embedding_cache_get(key)
                if q_vec is not None:
                    break
            if q_vec is None:
                q_vec = embeddings.embed_query(rewritten)
                for key in [k for k in [sid_key, cache_key_main] if k]:
                    _embedding_cache_set(key, q_vec)
            vec_hits = retrieve_from_pinecone(q_vec, top_k=min(16, _int_env('HYBRID_TOP_K', 8) * 2))
            try:
                if (os.getenv('USE_DIAGRAMS','1').strip().lower() in {"1","true","yes"}) and is_diagram_intent(rewritten):
                    d_topk = int(_int_env('DIAGRAM_TOP_K', 6))
                    d_hits = retrieve_diagrams(q_vec, top_k=d_topk)
                    if d_hits:
                        vec_hits = (vec_hits or []) + d_hits
            except Exception:
                pass
            bm25_pack = load_bm25_pack()
            bm_hits = bm25_top(rewritten, bm25_pack, top_k=min(16, _int_env('HYBRID_TOP_K', 8) * 2))
            merged = hybrid_merge(vec_hits, bm_hits, alpha=float(_int_env('HYBRID_ALPHA', 0.6)), top_k=_int_env('HYBRID_TOP_K', 8), dedupe_threshold=float(_int_env('HYBRID_DEDUPE_THRESHOLD', 0.75)), query_text=question_text)
            reranked = lexical_rerank(rewritten, merged)
            reranked = _prepend_catalog_hits(question_text, reranked)
            citation_block = _format_citations(reranked)
            context = build_context(reranked)

            # Caso / Playbook
            case_header = ""
            transcripts = []  # asegúrate de existir aunque no haya From/media
            from . import storage as _st
            if From:
                case = _st.get_or_create_case(str(From))
                sig = _st.extract_signals(Body)
                # Playbook
                pb = _st.load_playbooks()
                cat = (sig.get('category') if isinstance(sig, dict) else None) or 'general'
                pb_cat = pb.get(cat) or {}
                # Requisitos
                req = pb_cat.get('ask_for') or []
                _st.add_required(str(From), req)

                # Crear/obtener caso en Neon
                neon_case_id = case.get('db_case_id')
                try:
                    import os as _os
                    from . import db_cases as _dbc
                    if not neon_case_id and (_os.getenv('POSTGRES_URL') or _os.getenv('DATABASE_URL')):
                        created = _dbc.create_case('whatsapp', client_id=str(From))
                        if created:
                            neon_case_id = created
                            case = _st.update_case(str(From), {'db_case_id': neon_case_id})
                except Exception:
                    pass

                # Media entrante
                try:
                    n = int(NumMedia or '0')
                except Exception:
                    n = 0

                # Modo de procesamiento de media: inline | skip (background)
                media_mode = (os.getenv('MEDIA_PROCESSING', 'inline') or 'inline').strip().lower()

                media = []
                media_fields = [
                    (MediaUrl0, MediaContentType0), (MediaUrl1, MediaContentType1), (MediaUrl2, MediaContentType2),
                    (MediaUrl3, MediaContentType3), (MediaUrl4, MediaContentType4), (MediaUrl5, MediaContentType5),
                    (MediaUrl6, MediaContentType6), (MediaUrl7, MediaContentType7), (MediaUrl8, MediaContentType8),
                    (MediaUrl9, MediaContentType9)
                ]
                max_items = _int_env('MEDIA_MAX_ITEMS', 3)
                for (u, ctype) in media_fields[: min(max(0, n), max_items) ]:
                    if u:
                        # Normalizar content_type
                        ct = (ctype or '').lower().split(';')[0].strip()
                        if not ct:
                            ct = _guess_content_type(u, ct) or ''
                        media.append({'url': u, 'content_type': ct})

                if media:
                    _st.attach_media(str(From), media)
                    if media_mode != 'inline':
                        try:
                            for item in media:
                                _st.enqueue_media(str(From), item)
                        except Exception:
                            pass
                        media = []

                provided_items = []
                transcripts = []
                ocr_list = []
                part_class_list = []
                rec_checks_agg = []
                oem_hits_agg = []
                if media:
                    res_media = _process_media_items(str(From), media, cat, neon_case_id, case)
                    provided_items.extend(res_media.get('provided_items') or [])
                    for tx in res_media.get('transcripts') or []:
                        if tx and tx not in transcripts:
                            transcripts.append(tx)
                    ocr_list.extend(res_media.get('ocr_list') or [])
                    part_class_list.extend(res_media.get('part_class_list') or [])
                    for rc in res_media.get('rec_checks') or []:
                        if rc not in rec_checks_agg:
                            rec_checks_agg.append(rc)
                    for h in res_media.get('oem_hits') or []:
                        oem_hits_agg.append(h)
                    case = res_media.get('case') or case
                # Marcar provided
                if provided_items:
                    _st.mark_provided(str(From), list(dict.fromkeys(provided_items)))

                # Actualizar caso con severidad/categorías (considera transcripts)
                agg = _aggregate_signals([Body] + transcripts)
                cats = case.get('categories', []) + (agg.get('categories') or [])
                worst = agg.get('severity') or case.get('severity')
                case = _st.update_case(str(From), {'severity': worst, 'categories': cats})
                # upsert tipo de falla en Neon
                try:
                    if neon_case_id:
                        from . import db_cases as _dbc
                        _dbc.upsert_case_meta(neon_case_id, falla_tipo=((agg.get('categories') or ['general'])[0]))
                except Exception:
                    pass
                case_header = f"Caso {case.get('id')} — Severidad: {case.get('severity','normal')}"

            # Conversación previa
            history_text = ""
            try:
                if From:
                    from . import storage as _st
                    hist = _st.get_conversation(str(From), limit=_int_env('HISTORY_LIMIT', 8))
                    if hist:
                        lines = ["### Conversación Reciente:"]
                        for m in hist:
                            role = 'Usuario' if m.get('role') == 'user' else 'Agente'
                            txt = (m.get('text') or '').replace('\n', ' ')
                            lines.append(f"- {role}: {txt}")
                        history_text = "\n" + "\n".join(lines) + "\n"
            except Exception:
                pass

            # Si hay cambio de tema, ignora historia en este turno
            try:
                if _detect_topic_switch(Body, ocr_list, transcripts, locals().get('part_class_list')):
                    history_text = ""
            except Exception:
                pass

            # Preparar texto de usuario: usa Body si existe, si no usa transcript(s) si los hay
            user_text = (Body or '').strip()
            if not user_text and transcripts:
                user_text = ' '.join(transcripts)
            # Resumen de evidencia detectada (VIN/placa/odómetro/tipo + insumo si aplica)
            try:
                ev_parts = []
                vin_s = None; plate_s = None; odom = None; kinds = []
                for oc in (locals().get('ocr_list') or []):
                    if not isinstance(oc, dict):
                        continue
                    if oc.get('vin') and not vin_s:
                        vin_s = _mask_vin(oc.get('vin'))
                    if oc.get('plate') and not plate_s:
                        plate_s = _mask_plate(oc.get('plate'))
                    if oc.get('odo_km') is not None and odom is None:
                        odom = oc.get('odo_km')
                    et = (oc.get('evidence_type') or '').strip()
                    if et:
                        kinds.append(et)
                # Datos desde clasificación de pieza/insumo
                try:
                    for cls in (locals().get('part_class_list') or []):
                        if not isinstance(cls, dict):
                            continue
                        if cls.get('fluid_guess'):
                            ev_parts.append(f"fluido={cls.get('fluid_guess')}")
                        if cls.get('brand'):
                            ev_parts.append(f"marca={cls.get('brand')}")
                        if cls.get('color'):
                            ev_parts.append(f"color={cls.get('color')}")
                except Exception:
                    pass
                if odom is not None: ev_parts.append(f"odómetro={odom} km")
                if vin_s: ev_parts.append(f"vin={vin_s}")
                if plate_s: ev_parts.append(f"placa={plate_s}")
                if kinds: ev_parts.append("evidencia=" + ", ".join(list(dict.fromkeys(kinds))[:3]))
                if ev_parts:
                    tag = "Evidencia detectada: " + ", ".join(ev_parts)
                    user_text = (user_text + "\n\n" + tag) if user_text else tag
            except Exception:
                pass
            # Generar respuesta (contexto + historia + resumen del caso)
            llm = ChatOpenAI(model=os.getenv("LLM_MODEL", "gpt-4o"), temperature=0.1)
            from langchain_core.messages import SystemMessage, HumanMessage
            scenario_txt = ""
            agg = {}
            agg_severity = 'normal'
            agg_categories: list[str] = []
            try:
                hx_user_texts = []
                if 'hist' in locals() and hist:
                    hx_user_texts = [m.get('text') for m in hist if m.get('role') == 'user']
                agg = _aggregate_signals(hx_user_texts + [user_text or '']) or {}
                agg_severity = (agg.get('severity') or 'normal') if isinstance(agg, dict) else 'normal'
                agg_categories = [c for c in (agg.get('categories') or []) if c] if isinstance(agg, dict) else []
                cats_txt = ", ".join(agg_categories) or 'general'
                scenario_txt = (
                    f"### Resumen del Caso (inferido):\n"
                    f"- Severidad: {agg_severity}\n"
                    f"- Categorías: {cats_txt}\n"
                    f"- Asume la misma vagoneta que mensajes previos salvo que se indique lo contrario.\n\n"
                )
            except Exception:
                agg_severity = 'normal'
                agg_categories = []
            pending_evidence: list[str] = []
            try:
                if From:
                    from . import storage as _st
                    case_ctx = _st.get_or_create_case(str(From))
                    sig_case = _st.extract_signals(user_text or '')
                    cat_hint = (sig_case.get('category') if isinstance(sig_case, dict) else None)
                    pb = _st.load_playbooks()
                    ask = (pb.get(cat_hint or (agg_categories[0] if agg_categories else 'general')) or {}).get('ask_for') or []
                    provided = set((case_ctx or {}).get('provided') or [])
                    pending_evidence = [item for item in ask if item not in provided]
            except Exception:
                pending_evidence = []
            human = (
                f"### Contexto de Manuales Técnicos:\n{context}\n\n" + history_text + scenario_txt +
                f"### Pregunta del Usuario:\n{user_text or '(sin texto, solo adjuntos)'}"
            )
            response = llm.invoke([
                SystemMessage(content=system_prompt_hybrid(
                    severity=agg_severity,
                    categories=agg_categories,
                    missing_evidence=pending_evidence,
                )),
                HumanMessage(content=human),
            ])
            raw_answer = getattr(response, 'content', None) or str(response)
            # Ajustar límite según severidad detectada
            try:
                from . import storage as _st
                sig = _st.extract_signals(user_text or '')
            except Exception:
                sig = {}
            limit = _effective_limit({'severity': sig.get('severity') if isinstance(sig, dict) else None}, channel="whatsapp")
            core = _summarize_to_limit(raw_answer, user_text or '', reranked, limit)
            # Añadir evidencia detectada (VIN/placa/odómetro) si existe
            try:
                if ocr_list:
                    vin_s = None; plate_s = None; odom = None; deliv = None
                    for oc in ocr_list:
                        if not isinstance(oc, dict):
                            continue
                        if oc.get('vin') and not vin_s:
                            vin_s = _mask_vin(oc.get('vin'))
                        if oc.get('plate') and not plate_s:
                            plate_s = _mask_plate(oc.get('plate'))
                        if oc.get('odo_km') is not None and odom is None:
                            odom = oc.get('odo_km')
                        if oc.get('delivered_at') and not deliv:
                            deliv = str(oc.get('delivered_at'))
                    parts = []
                    if vin_s: parts.append(f"VIN {vin_s}")
                    if plate_s: parts.append(f"placa {plate_s}")
                    if odom is not None: parts.append(f"odómetro={odom} km")
                    if deliv: parts.append(f"entregado={deliv}")
                    if parts:
                        core = "Leí " + ", ".join(parts) + ". ¿Es correcto?\n\n" + core
            except Exception:
                pass
            # Anexar encabezado de caso y pendientes si hay
            try:
                from . import storage as _st
                missing = []
                if From:
                    pb = _st.load_playbooks()
                    sig2 = _st.extract_signals(user_text or '')
                    cat2 = (sig2.get('category') if isinstance(sig2, dict) else None) or 'general'
                    ask = (pb.get(cat2) or {}).get('ask_for') or []
                    case = _st.get_or_create_case(str(From))
                    provided = set(case.get('provided') or [])
                    missing = [i for i in ask if i not in provided]
                # Si el usuario envió una fecha AAAA-MM-DD o DD/MM/AAAA en el texto, registrar delivered_at
                try:
                    import re as _rx
                    m = _rx.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", user_text or '')
                    if not m:
                        m2 = _rx.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", user_text or '')
                        if m2:
                            yyyy, mm, dd = m2.group(3), m2.group(2), m2.group(1)
                            iso = f"{int(yyyy):04d}-{int(mm):02d}-{int(dd):02d}"
                            m = None
                        else:
                            iso = None
                    else:
                        yyyy, mm, dd = m.group(1), m.group(2), m.group(3)
                        iso = f"{int(yyyy):04d}-{int(mm):02d}-{int(dd):02d}"
                    if iso and case.get('db_case_id'):
                        from . import db_cases as _dbc
                        _dbc.upsert_case_meta(case.get('db_case_id'), delivered_at=iso)
                except Exception:
                    pass

                if case_header:
                    core = f"{case_header}\n\n" + core
                if citation_block:
                    core = core.rstrip() + "\n\nFuentes:\n" + citation_block
                if missing:
                    core = core.rstrip() + "\n\nPendiente (evidencia mínima): " + ", ".join(missing[:5])
                else:
                    # Evidencia completa: evaluar garantía solo si hay confirmación o no se requiere
                    try:
                        from . import warranty as _w
                        neon_case_id = case.get('db_case_id') if isinstance(case, dict) else None
                        require_confirm = _bool_env('WARRANTY_REQUIRE_CONFIRM', True)
                        asked = _warranty_requested([Body] + (transcripts if 'transcripts' in locals() and transcripts else []))
                        if neon_case_id and (asked or not require_confirm):
                            w = _w.policy_evaluate(neon_case_id, fallback_category=cat, problem=(sig.get('problem') if isinstance(sig, dict) else None))
                            elig = w.get('eligibility')
                            reasons = w.get('reasons') or []
                            core += "\n\nGarantía: " + str(elig).upper()
                            if reasons:
                                core += " — " + "; ".join(reasons[:3])
                        elif require_confirm and not asked:
                            core += "\n\nSi deseas validar garantía: no se tramita automático; primero confirmo VIN/placa/odómetro/fecha de entrega. ¿Quieres que la validemos?"
                            if _bool_env('QUICK_REPLIES', True):
                                try:
                                    import re as _re_qr
                                    parts_hint = bool(_re_qr.search(r"\b[A-Z0-9]{2,}(?:[\-–][A-Z0-9]{2,})+\b", user_text or ''))
                                except Exception:
                                    parts_hint = False
                                actions = ['Validar garantía', 'Seguir diagnóstico']
                                if parts_hint:
                                    actions.append('Buscar refacción')
                                qr = _quick_replies_line(actions)
                                if qr:
                                    core += "\n" + qr
                    except Exception:
                        pass
            except Exception:
                pass
            # Anexar insights de piezas (probable, revisiones y OEM) si existen
            try:
                extra_blocks = []
                # 1) Pieza probable (de visión)
                try:
                    best = None
                    best_conf = -1.0
                    for cls in (locals().get('part_class_list') or []) or []:
                        if not isinstance(cls, dict):
                            continue
                        guess = (cls.get('part_guess') or '').strip()
                        try:
                            conf = float(cls.get('confidence') or 0.0)
                        except Exception:
                            conf = 0.0
                        if guess and conf >= best_conf:
                            best, best_conf = cls, conf
                    # 0) Objeto/Falla si está disponible
                    if best and isinstance(best, dict):
                        try:
                            obj = (best.get('object_primary') or '').strip()
                            subc = (best.get('subcomponent') or '').strip()
                            ftyp = (best.get('failure_type') or '').strip()
                            fzone = (best.get('failure_zone') or '').strip()
                            # Fallback desde OCR si faltan campos y hay evidencia de llanta/rin
                            if (not obj or not subc or not ftyp) and (locals().get('ocr_list') or []):
                                for oc in (ocr_list or []):
                                    et = (oc.get('evidence_type') or '').lower() if isinstance(oc, dict) else ''
                                    if et == 'fuga_llanta':
                                        obj = obj or 'llanta'
                                        subc = subc or 'rin'
                                        ftyp = ftyp or 'fuga de aire'
                                    if et == 'grieta_rin':
                                        obj = obj or 'llanta'
                                        subc = subc or 'rin'
                                        ftyp = ftyp or 'grieta'
                            parts = []
                            if obj: parts.append(f"Objeto: {obj}")
                            if subc: parts.append(f"Subcomponente: {subc}")
                            if ftyp:
                                if fzone:
                                    parts.append(f"Falla: {ftyp} (zona: {fzone})")
                                else:
                                    parts.append(f"Falla: {ftyp}")
                            if parts:
                                extra_blocks.append(" · ".join(parts))
                        except Exception:
                            pass
                    if best and best.get('part_guess'):
                        sys_txt = (best.get('system') or '').strip()
                        conf_txt = f"{best_conf:.2f}" if best_conf >= 0 else ""
                        # Umbrales de confianza: >=0.86 => ES, 0.55..0.85 => puede ser, <0.55 => omitir
                        if best_conf >= 0.86:
                            line = f"ES: {best.get('part_guess')}"
                        elif best_conf >= 0.55:
                            line = f"puede ser: {best.get('part_guess')}"
                        else:
                            line = None
                        if line:
                            if sys_txt:
                                line += f" · sistema {sys_txt}"
                            if conf_txt:
                                line += f" (conf {conf_txt})"
                            extra_blocks.append(line)
                    # Fallback heurístico si no hubo guess y el texto menciona diferencial o refrigerante
                    if not best:
                        ut = (user_text or Body or '').lower()
                        if 'diferencial' in ut:
                            extra_blocks.append('puede ser: tapa de diferencial / sello de piñón (a confirmar)')
                        elif any(k in ut for k in ['refrigerante','anticongelante','garrafa','bidón','bidon']):
                            extra_blocks.append('puede ser: garrafa/etiqueta de refrigerante')
                except Exception:
                    pass
                try:
                    if rec_checks_agg:
                        checks_txt = "\n- ".join(rec_checks_agg[:6])
                        extra_blocks.append("Recomendado revisar:\n- " + checks_txt)
                except Exception:
                    pass
                try:
                    # Filtrar OEM por confianza de clasificación y score mínimo del catálogo
                    oem_conf_min = _float_env('OEM_CONF_MIN', 0.6)
                    oem_score_min = _float_env('OEM_SCORE_MIN', 0.5)
                    best_conf_current = 0.0
                    try:
                        for cls in (locals().get('part_class_list') or []) or []:
                            try:
                                c = float(cls.get('confidence') or 0.0)
                                if c > best_conf_current:
                                    best_conf_current = c
                            except Exception:
                                continue
                    except Exception:
                        best_conf_current = 0.0
                    if oem_hits_agg and best_conf_current >= oem_conf_min:
                        items = []
                        for h in oem_hits_agg[:5]:
                            try:
                                sc = float(h.get('score') or 0.0)
                            except Exception:
                                sc = 0.0
                            if sc < oem_score_min:
                                continue
                            o = str(h.get('oem') or '?')
                            pn = str(h.get('part_name') or '?')
                            pg = str(h.get('page_label') or '?')
                            items.append(f"{o} ({pn} · pág {pg})")
                        if items:
                            extra_blocks.append("Posibles OEM: " + ", ".join(items))
                except Exception:
                    pass
                if extra_blocks:
                    core = core.rstrip() + "\n\n" + "\n".join(extra_blocks)
                    # Quick replies específicos para refacciones/garantía
                    try:
                        qr = _quick_replies_line(['Enviar a refacciones', 'Validar garantía'])
                        if qr:
                            core += "\n" + qr
                    except Exception:
                        pass
            except Exception:
                pass
            # Cierre natural (no crítico)
            try:
                closers = ["¿Seguimos?", "¿Te late?", "¿Te va?", "¿Te ayudo con algo más?"]
                sev_now = (sig.get('severity') if isinstance(sig, dict) else None) or 'normal'
                if sev_now != 'critical':
                    core = core.rstrip() + "\n\n" + random.choice(closers)
            except Exception:
                pass

            # Saludo con nombre (una vez por caso)
            try:
                from . import storage as _st
                if From and ProfileName:
                    case = _st.get_or_create_case(str(From))
                    if not case.get('greeted'):
                        greet = f"Hola {ProfileName},\n\n"
                        core = greet + core
                        _st.update_case(str(From), {'greeted': True})
            except Exception:
                pass

            answer = core
        except Exception:
            logger.exception("Error en pipeline WhatsApp (/twilio/whatsapp)")
            answer = "Hubo un problema procesando tu mensaje. Intenta de nuevo."

    # Asegurar límite duro para WhatsApp (final)
    max_chars_wa = _int_env("WHATSAPP_MAX_CHARS", 1600)
    if answer and len(answer) > max_chars_wa:
        answer = answer[: max(0, max_chars_wa - 1)] + "…"

    twiml = f"""
    <Response>
      <Message>{answer}</Message>
    </Response>
    """.strip()
    # Log de salida WhatsApp
    try:
        from . import storage as _st
        latency_ms = (time.perf_counter() - _t0) * 1000.0
        response_bytes = len((answer or '').encode('utf-8'))
        _st.log_event(
            kind="whatsapp_out",
            payload={
                "to": From,
                "answer": answer,
                "channel": "whatsapp",
                "sid": MessageSid,
                "latency_ms": latency_ms,
                "response_bytes": response_bytes,
            }
        )
    except Exception:
        pass

    # Recordar respuesta por MessageSid (para dedupe)
    try:
        if MessageSid:
            _sid_remember(MessageSid, answer)
    except Exception:
        pass

    return Response(content=twiml, media_type="application/xml")


# =============== JSON mirror for Make (no TwiML) ===============
class WhatsAppProcessMedia(BaseModel):
    url: str
    content_type: str | None = None


class WhatsAppProcessRequest(BaseModel):
    # Forma principal (Make)
    From: str | None = None
    Body: str | None = None
    media: list[WhatsAppProcessMedia] | None = None
    # Compatibilidad opcional (payload alterno)
    contact: str | None = None
    meta: dict | None = None


@app.post("/twilio/whatsapp_json")
def twilio_whatsapp_json(payload: WhatsAppProcessRequest):
    """JSON endpoint (espejo) para orquestar con Make.
    Procesa texto + medios y devuelve {answer, case_id, pending, warranty?}.
    """
    _t0 = time.perf_counter()
    # Resolver campos con tolerancia: From/contact/meta.contact
    From = payload.From or payload.contact or ((payload.meta or {}).get('contact') if isinstance(payload.meta, dict) else None)
    Body = payload.Body or ""
    media_list = payload.media or []
    ocr_list = []
    # Sanitizar media: filtrar entradas sin URL válida y aplicar límite
    try:
        mm = []
        for m in media_list:
            u = getattr(m, 'url', None)
            if isinstance(u, str) and u.strip().lower().startswith(('http://','https://')):
                mm.append(m)
        media_list = mm[: _int_env('MEDIA_MAX_ITEMS', 3)]
    except Exception:
        media_list = []

    # Log inbound
    try:
        from . import storage as _st
        _st.log_event(kind="whatsapp_in", payload={"from": From, "text": Body, "classification": _st.classify(Body), "signals": _st.extract_signals(Body)})
    except Exception:
        pass

    # Build case + playbook like Twilio webhook
    case_header = ""
    neon_case_id = None
    sig = {}
    try:
        from . import storage as _st
        if From:
            case = _st.get_or_create_case(str(From))
            sig = _st.extract_signals(Body)
            pb = _st.load_playbooks()
            cat = (sig.get('category') if isinstance(sig, dict) else None) or 'general'
            req = (pb.get(cat) or {}).get('ask_for') or []
            _st.add_required(str(From), req)
            try:
                import os as _os
                from . import db_cases as _dbc
                neon_case_id = case.get('db_case_id')
                if not neon_case_id and (_os.getenv('POSTGRES_URL') or _os.getenv('DATABASE_URL')):
                    created = _dbc.create_case('make', client_id=str(From))
                    if created:
                        neon_case_id = created
                        case = _st.update_case(str(From), {'db_case_id': neon_case_id})
            except Exception:
                pass
            # Process media (OCR / audio)
            provided_items = []
            transcripts = []
            ocr_list = []
            part_class_list = []
            rec_checks_agg = []
            oem_hits_agg = []
            media_mode = (os.getenv('MEDIA_PROCESSING', 'inline') or 'inline').strip().lower()
            media_payload = []
            for mm in media_list[:10]:
                url = getattr(mm, 'url', None)
                if not isinstance(url, str):
                    continue
                ct = (getattr(mm, 'content_type', None) or '').lower().split(';')[0].strip()
                media_payload.append({'url': url, 'content_type': ct})
            if media_payload:
                _st.attach_media(str(From), media_payload)
                if media_mode != 'inline':
                    try:
                        for item in media_payload:
                            _st.enqueue_media(str(From), item)
                    except Exception:
                        pass
                    media_payload = []
            if media_payload:
                res_media = _process_media_items(str(From), media_payload, cat, neon_case_id, case)
                provided_items.extend(res_media.get('provided_items') or [])
                for tx in res_media.get('transcripts') or []:
                    if tx and tx not in transcripts:
                        transcripts.append(tx)
                ocr_list.extend(res_media.get('ocr_list') or [])
                part_class_list.extend(res_media.get('part_class_list') or [])
                for rc in res_media.get('rec_checks') or []:
                    if rc not in rec_checks_agg:
                        rec_checks_agg.append(rc)
                for h in res_media.get('oem_hits') or []:
                    oem_hits_agg.append(h)
                case = res_media.get('case') or case

            if provided_items:
                _st.mark_provided(str(From), list(dict.fromkeys(provided_items)))
            # Update severity/categories in case
            agg = _aggregate_signals([Body] + transcripts)
            cats = case.get('categories', []) + (agg.get('categories') or [])
            worst = agg.get('severity') or case.get('severity')
            case = _st.update_case(str(From), {'severity': worst, 'categories': cats})
            # falla_tipo en Neon
            try:
                if neon_case_id:
                    from . import db_cases as _dbc
                    _dbc.upsert_case_meta(neon_case_id, falla_tipo=((agg.get('categories') or ['general'])[0]))
            except Exception:
                pass
            case_header = f"Caso {case.get('id')} — Severidad: {case.get('severity','normal')}"
    except Exception:
        pass

    # Comando rápido: Buscar refacción
    try:
        if _is_quick_parts(Body):
            import re as _re
            # Construir texto para búsqueda (preferir OEM desde OCR/transcripción)
            scan_parts = []
            for oc in (locals().get('ocr_list') or []) or []:
                if isinstance(oc, dict):
                    if oc.get('notes'): scan_parts.append(str(oc.get('notes')))
                    if oc.get('raw'): scan_parts.append(str(oc.get('raw')))
            for tx in (locals().get('transcripts') or []) or []:
                scan_parts.append(tx)
            scan_parts.append(Body or '')
            scan_text = " \n".join([s for s in scan_parts if s])
            m = _re.search(r"\b[A-Z0-9]{2,}(?:[\-–][A-Z0-9]{2,})+\b", scan_text)
            q = m.group(0) if m else (scan_text if scan_text.strip() and scan_text.strip().lower() != 'buscar refacción' else '')
            items = []
            if q:
                try:
                    res = parts_search(q, top_k=3, include_equivalences=True)  # usa la función del endpoint local
                    items = (res or {}).get('items', [])
                    equivs = (res or {}).get('equivalences', [])
                except Exception:
                    items = []
                    equivs = []
            else:
                items, equivs = [], []
            response_lines = []
            if items:
                response_lines.append("Catálogo (top):")
                for it in items[:3]:
                    response_lines.append(f"- {it.get('part_name','?')} · OEM {it.get('oem','?')} · pág {it.get('page_label','?')}")
            if equivs:
                response_lines.append("Equivalencias sugeridas:")
                for eq in equivs[:3]:
                    variants = []
                    for variant in (eq.get('equivalents') or [])[:4]:
                        if not isinstance(variant, dict):
                            continue
                        label = variant.get('provider') or ''
                        part_num = variant.get('part_number') or ''
                        vtype = variant.get('type') or ''
                        components = [label]
                        if part_num:
                            components.append(part_num)
                        if vtype == 'oem':
                            components.append('OEM')
                        variants.append(" ".join([c for c in components if c]))
                    ref_name = eq.get('name') or 'sin nombre'
                    response_lines.append(f"- {eq.get('internal_ref','?')} ({ref_name}) → {', '.join(variants) if variants else 'sin equivalencias registradas'}")
            if response_lines:
                if _bool_env('QUICK_REPLIES', True):
                    qr = _quick_replies_line(['Seguir diagnóstico', 'Validar garantía'])
                    if qr:
                        response_lines.append(qr)
                return {"answer": "\n".join(response_lines), "case_id": case.get('id') if isinstance(case, dict) else None}
            msg = "No encontré coincidencias claras. Dime el nombre de la pieza o envía una foto de la etiqueta (OEM)."
            if _bool_env('QUICK_REPLIES', True):
                qr = _quick_replies_line(['Seguir diagnóstico', 'Validar garantía'])
                if qr:
                    msg += "\n" + qr
            return {"answer": msg, "case_id": case.get('id') if isinstance(case, dict) else None}
    except Exception:
        pass

    # Build RAG answer
    # Query text: Body + evidencia/transcripciones (si aplica)
    user_text = (Body or '').strip()
    # Resumen de evidencia desde OCR/transcripción
    ev_parts = []
    try:
        vin = None; plate = None; odo = None; delivered = None; kinds = []
        for oc in (locals().get('ocr_list') or []):
            if isinstance(oc, dict):
                vin = vin or _mask_vin(oc.get('vin'))
                plate = plate or _mask_plate(oc.get('plate'))
                if oc.get('odo_km') is not None:
                    try:
                        odo = int(oc.get('odo_km'))
                    except Exception:
                        odo = oc.get('odo_km')
                delivered = delivered or oc.get('delivered_at')
                et = (oc.get('evidence_type') or '').strip()
                if et:
                    kinds.append(et)
        if odo is not None:
            ev_parts.append(f"odómetro={odo} km")
        if vin:
            ev_parts.append(f"vin={vin}")
        if plate:
            ev_parts.append(f"placa={plate}")
        if delivered:
            ev_parts.append(f"delivered_at={delivered}")
        if kinds:
            ev_parts.append("evidencia=" + ", ".join(list(dict.fromkeys(kinds))[:3]))
    except Exception:
        pass
    # Usar la etiqueta exacta que el prompt instruye a priorizar
    ev_text = ("Evidencia detectada: " + ", ".join(ev_parts)) if ev_parts else ""
    tx_text = ("Transcripción: " + (transcripts[0] if ('transcripts' in locals() and transcripts) else "").strip()) if (not user_text and ('transcripts' in locals()) and transcripts) else ""
    if not user_text:
        user_text = "\n\n".join([t for t in [ev_text, tx_text] if t]) or 'Evidencia adjunta'
    else:
        if ev_text:
            user_text = user_text + "\n\n" + ev_text
    try:
        emb_model = _embeddings_model()
        embeddings = OpenAIEmbeddings(model=emb_model)
        rewritten_q = rewrite_query(user_text)
        cache_key_main = hashlib.sha256((rewritten_q or '').encode('utf-8', 'ignore')).hexdigest()
        q_vec = _embedding_cache_get(cache_key_main)
        if q_vec is None:
            q_vec = embeddings.embed_query(rewritten_q)
            _embedding_cache_set(cache_key_main, q_vec)
        vec_hits = retrieve_from_pinecone(q_vec, top_k=min(16, int(_env('HYBRID_TOP_K','8'))*2))
        try:
            if (os.getenv('USE_DIAGRAMS','1').strip().lower() in {"1","true","yes"}) and is_diagram_intent(user_text):
                d_topk = int(_env('DIAGRAM_TOP_K','6') or '6')
                d_hits = retrieve_diagrams(q_vec, top_k=d_topk)
                if d_hits:
                    vec_hits = (vec_hits or []) + d_hits
        except Exception:
            pass
        bm25_pack = load_bm25_pack()
        bm_hits = bm25_top(user_text, bm25_pack, top_k=min(16, int(_env('HYBRID_TOP_K','8'))*2))
        alpha = float(_env('HYBRID_ALPHA','0.6'))
        dedupe = float(_env('HYBRID_DEDUPE_THRESHOLD','0.75'))
        top_k = int(_env('HYBRID_TOP_K','8'))
        merged = hybrid_merge(vec_hits, bm_hits, alpha=alpha, top_k=top_k, dedupe_threshold=dedupe, query_text=user_text)
        reranked = lexical_rerank(user_text, merged)
        reranked = _prepend_catalog_hits(user_text, reranked)
        citation_block = _format_citations(reranked)
        context = build_context(reranked)
        # History
        history_text = ""
        try:
            if From:
                from . import storage as _st
                hist = _st.get_conversation(str(From), limit=_int_env('HISTORY_LIMIT',8))
                if hist:
                    lines = ["### Conversación Reciente:"]
                    for m in hist:
                        role = 'Usuario' if m.get('role') == 'user' else 'Agente'
                        txt = (m.get('text') or '').replace('\n', ' ')
                        lines.append(f"- {role}: {txt}")
                    history_text = "\n" + "\n".join(lines) + "\n"
        except Exception:
            pass
        # Ignorar historia si hay cambio de tema explícito
        try:
            if _detect_topic_switch(Body, ocr_list, transcripts, locals().get('part_class_list')):
                history_text = ""
        except Exception:
            pass
        # Si viene solo media sin texto, reduce historia a los últimos 2 turnos (mantener contexto breve)
        try:
            has_media = bool((locals().get('media_list') or []))
            has_body = bool((Body or '').strip())
            if has_media and not has_body:
                try:
                    last_hist = hist[-2:] if ('hist' in locals() and hist) else []
                    if last_hist:
                        lines = ["### Conversación Reciente (breve):"]
                        for m in last_hist:
                            role = 'Usuario' if m.get('role') == 'user' else 'Agente'
                            txt = (m.get('text') or '').replace('\n', ' ')
                            lines.append(f"- {role}: {txt}")
                        history_text = "\n" + "\n".join(lines) + "\n"
                except Exception:
                    pass
        except Exception:
            pass
        # Scenario
        scenario_txt = ""
        agg_case = {}
        agg_severity = 'normal'
        agg_categories: list[str] = []
        try:
            from . import storage as _st
            hx_user_texts = []
            if 'hist' in locals() and hist:
                hx_user_texts = [m.get('text') for m in hist if m.get('role') == 'user']
            agg_case = _aggregate_signals(hx_user_texts + [user_text]) or {}
            agg_severity = (agg_case.get('severity') or 'normal') if isinstance(agg_case, dict) else 'normal'
            agg_categories = [c for c in (agg_case.get('categories') or []) if c] if isinstance(agg_case, dict) else []
            cats_txt = ", ".join(agg_categories) or 'general'
            scenario_txt = f"### Resumen del Caso (inferido):\n- Severidad: {agg_severity}\n- Categorías: {cats_txt}\n- Asume la misma vagoneta que mensajes previos salvo que se indique lo contrario.\n\n"
        except Exception:
            agg_severity = 'normal'
            agg_categories = []
        # Relación con el caso (categoría + similitud + OEM)
        relation_block = ""
        try:
            import re as _re
            from . import storage as _st
            # Texto de evidencia libre (notas/raw de OCR + transcripts)
            ev_free = []
            for oc in (locals().get('ocr_list') or []) or []:
                if isinstance(oc, dict):
                    if oc.get('notes'):
                        ev_free.append(str(oc.get('notes')))
                    elif oc.get('raw'):
                        ev_free.append(str(oc.get('raw')))
            for tx in (locals().get('transcripts') or []) or []:
                ev_free.append(tx)
            ev_free_text = " \n".join([t for t in ev_free if t])
            # OEM detectado
            oem_re = _re.compile(r"\b[A-Z0-9]{2,}(?:[\-–][A-Z0-9]{2,})+\b")
            has_oem = bool(oem_re.search(ev_free_text))
            # Categorías
            sig_ev = _st.extract_signals(ev_free_text) if ev_free_text else {}
            cat_ev = sig_ev.get('category') if isinstance(sig_ev, dict) else None
            cats_case = []
            try:
                if 'case' in locals() and isinstance(case, dict):
                    cats_case = (case.get('categories') or [])
            except Exception:
                cats_case = []
            try:
                agg_case = _aggregate_signals((locals().get('hx_user_texts') or []) + [user_text])
                for c in (agg_case.get('categories') or []):
                    if c not in cats_case:
                        cats_case.append(c)
            except Exception:
                pass
            # Similitud léxica con últimos turnos de usuario
            def _tokset(s: str):
                import re as __re
                return set(__re.findall(r"\w+", (s or '').lower()))
            def _jacc(a: str, b: str) -> float:
                sa, sb = _tokset(a), _tokset(b)
                if not sa or not sb:
                    return 0.0
                inter = len(sa & sb); union = len(sa | sb)
                return (inter/union) if union else 0.0
            last_users = (locals().get('hx_user_texts') or [])
            last_users = last_users[-max(1, _int_env('HISTORY_WINDOW', 2)):] if last_users else []
            max_sim = 0.0
            for t in last_users:
                max_sim = max(max_sim, _jacc(ev_free_text, t))
            try:
                rel_th = float(os.getenv('RELATION_THRESHOLD', '0.25'))
            except Exception:
                rel_th = 0.25
            # Decisión
            relation = 'indefinida'
            reason = []
            if has_oem:
                relation = 'parts'; reason.append('OEM_detectado')
            elif cat_ev and cat_ev in (cats_case or []):
                relation = 'relacionada'; reason.append('categoria_match')
            elif max_sim >= rel_th:
                relation = 'relacionada'; reason.append(f'similitud_lexica={max_sim:.2f}')
            else:
                reason.append(f'similitud_lexica={max_sim:.2f}')
            relation_block = (
                "### Relación con el caso:\n"
                f"- relacion: {relation}\n"
                f"- razon: {', '.join(reason)}\n\n"
            )
        except Exception:
            relation_block = ""

        # LLM
        llm = ChatOpenAI(model=os.getenv("LLM_MODEL", "gpt-4o"), temperature=0.1)
        from langchain_core.messages import SystemMessage, HumanMessage
        evidence_block = ("### Evidencia detectada:\n" + ev_text + "\n\n") if ev_text else ""
        mode_block = ""
        try:
            # Determinar tipo de media: audio vs imagen
            mode_has_audio = False
            mode_has_image = False
            try:
                for m in (locals().get('media_list') or []) or []:
                    ct = (getattr(m, 'content_type', None) or '').lower()
                    if ct.startswith('audio'):
                        mode_has_audio = True
                    if ct.startswith('image'):
                        mode_has_image = True
            except Exception:
                pass
            if has_media and not has_body:
                if mode_has_audio and not mode_has_image:
                    mode = 'solo_audio'
                elif mode_has_image and not mode_has_audio:
                    mode = 'solo_imagenes'
                else:
                    mode = 'solo_media_mixto'
            elif has_media and has_body:
                if mode_has_audio and not mode_has_image:
                    mode = 'audio_y_texto'
                elif mode_has_image and not mode_has_audio:
                    mode = 'imagenes_y_texto'
                else:
                    mode = 'mixto_y_texto'
            else:
                mode = 'solo_texto'
            mode_block = f"### Modo de entrada: {mode}\n\n"
        except Exception:
            pass
        pending_evidence: list[str] = []
        try:
            from . import storage as _st
            contact_ref = None
            if payload.meta and isinstance(payload.meta, dict):
                contact_ref = payload.meta.get('contact') or payload.meta.get('from') or payload.meta.get('session_id')
            contact_ref = contact_ref or From
            if contact_ref:
                case_ctx = _st.get_or_create_case(str(contact_ref))
                sig_case = _st.extract_signals(user_text)
                cat_hint = (sig_case.get('category') if isinstance(sig_case, dict) else None)
                pb = _st.load_playbooks()
                ask = (pb.get(cat_hint or (agg_categories[0] if agg_categories else 'general')) or {}).get('ask_for') or []
                provided = set((case_ctx or {}).get('provided') or [])
                pending_evidence = [item for item in ask if item not in provided]
        except Exception:
            pending_evidence = []
        human = (
            f"### Contexto de Manuales Técnicos:\n{context}\n\n" + mode_block + history_text + scenario_txt + relation_block + evidence_block +
            f"### Pregunta del Usuario:\n{user_text}"
        )
        response = llm.invoke([
            SystemMessage(content=system_prompt_hybrid(
                severity=agg_severity,
                categories=agg_categories,
                missing_evidence=pending_evidence,
            )),
            HumanMessage(content=human),
        ])
        raw_answer = getattr(response,'content',None) or str(response)
        # Summarize
        try:
            from . import storage as _st
            sig3 = _st.extract_signals(user_text)
        except Exception:
            sig3 = {}
        # Permitir límite desde meta.max_chars si viene en payload
        meta_for_limit = {}
        try:
            if isinstance(payload.meta, dict):
                meta_for_limit.update(payload.meta)
        except Exception:
            pass
        if isinstance(sig3, dict):
            meta_for_limit.setdefault('severity', sig3.get('severity'))
        limit = _effective_limit(meta_for_limit, channel="whatsapp")
        core = _summarize_to_limit(raw_answer, user_text, reranked, limit)

        # Prefijo determinista con evidencia y relación (si hay) para evitar respuestas genéricas
        try:
            prefix_lines = []
            # Extraer evidencia directamente desde ocr_list (más robusto que parsear ev_text)
            odom = None; vin_s = None; plate_s = None; deliv = None
            for oc in (locals().get('ocr_list') or []) or []:
                try:
                    if oc.get('odo_km') is not None and odom is None:
                        odom = int(oc.get('odo_km')) if str(oc.get('odo_km')).isdigit() else oc.get('odo_km')
                    if oc.get('vin') and vin_s is None:
                        vin_s = _mask_vin(oc.get('vin'))
                    if oc.get('plate') and plate_s is None:
                        plate_s = _mask_plate(oc.get('plate'))
                    if oc.get('delivered_at') and deliv is None:
                        deliv = str(oc.get('delivered_at'))
                except Exception:
                    continue
            parts = []
            if odom is not None: parts.append(f"odómetro={odom} km")
            if vin_s: parts.append(f"VIN {vin_s}")
            if plate_s: parts.append(f"placa {plate_s}")
            if deliv: parts.append(f"entregado={deliv}")
            if parts:
                prefix_lines.append("Leí " + ", ".join(parts) + ". ¿Es correcto?")
            # Audio: si hay transcripción y Body vacío/corto, anteponer resumen y preguntas dirigidas
            try:
                has_body_short = not (Body or '').strip() or len((Body or '').strip()) < 8
                if has_body_short and ('transcripts' in locals()) and transcripts:
                    tx = (transcripts[0] or '').strip().replace('\n', ' ')
                    if tx:
                        # Longitud configurable del snippet de transcripción
                        snip_len = _int_env('TRANSCRIPT_SNIPPET_CHARS', 140)
                        if len(tx) > max(40, snip_len):
                            short = tx[: max(40, snip_len)] + '…'
                        else:
                            short = tx
                        prefix_lines.append("Escuché: " + short)
                        # Preguntas dirigidas por categoría/severidad
                        try:
                            cat_hint = None
                            sev_hint = None
                            if isinstance(sig3, dict):
                                cat_hint = sig3.get('category') or None
                                sev_hint = sig3.get('severity') or None
                            qs = []
                            if sev_hint == 'critical':
                                qs.append('Alerta: si hay riesgo, detén la vagoneta.')
                            if cat_hint == 'brakes':
                                qs.append('¿Pedal al fondo o fugas? ¿Luz ABS?')
                            elif cat_hint == 'oil':
                                qs.append('¿Luz de aceite encendida? ¿Nivel/fugas?')
                            elif cat_hint == 'cooling':
                                qs.append('¿Temp alta/ventiladores? ¿Olor a refrigerante?')
                            elif cat_hint == 'electrical':
                                qs.append('¿Check engine/fusibles? ¿Gira motor (crank)?')
                            elif cat_hint == 'fuel':
                                qs.append('¿Pierde potencia/jaloneos? ¿Filtros recientes?')
                            elif cat_hint == 'transmission':
                                qs.append('¿Entran cambios o patina? ¿Ruidos?')
                            else:
                                qs.append('¿Intermitente o constante? ¿Desde cuándo?')
                            qs.append('¿Se puede mover sin riesgo? ¿Testigos en tablero?')
                            prefix_lines.append(' '.join(qs))
                        except Exception:
                            prefix_lines.append('¿Hay testigos (aceite/temperatura/check)? ¿Se puede mover sin riesgo? Si puedes, confirma si es intermitente o constante.')
            except Exception:
                pass
            # Relación simple: categoría actual vs caso
            related_hint = ""
            try:
                case_cats = (case.get('categories') or []) if isinstance(case, dict) else []
            except Exception:
                case_cats = []
            rel_cat = None
            try:
                if isinstance(sig3, dict):
                    rel_cat = sig3.get('category')
            except Exception:
                rel_cat = None
            if rel_cat and rel_cat in case_cats:
                related_hint = "Relacionado con la falla en curso."
            # Ensamblar prefijo si hay algo que decir
            if prefix_lines or related_hint:
                prefix = "\n".join([l for l in [related_hint] if l])
                if prefix_lines:
                    prefix = (prefix + ("\n" if prefix else "")) + prefix_lines[0]
                core = prefix + "\n\n" + core
        except Exception:
            pass
        # Compose header + pending + warranty
        pending = []
        try:
            from . import storage as _st
            if From:
                pb = _st.load_playbooks()
                sig4 = _st.extract_signals(user_text)
                cat4 = (sig4.get('category') if isinstance(sig4, dict) else None) or 'general'
                ask = (pb.get(cat4) or {}).get('ask_for') or []
                case = _st.get_or_create_case(str(From))
                provided = set(case.get('provided') or [])
                pending = [i for i in ask if i not in provided]
            if case_header:
                core = f"{case_header}\n\n" + core
        except Exception:
            pass
        # Anexar insights de piezas (probable, revisiones y OEM) si existen
        try:
            extra_blocks = []
            # 1) Pieza probable (de visión)
            try:
                best = None
                best_conf = -1.0
                for cls in (locals().get('part_class_list') or []) or []:
                    if not isinstance(cls, dict):
                        continue
                    guess = (cls.get('part_guess') or '').strip()
                    try:
                        conf = float(cls.get('confidence') or 0.0)
                    except Exception:
                        conf = 0.0
                    if guess and conf >= best_conf:
                        best, best_conf = cls, conf
                # 0) Objeto/Falla si está disponible
                if best and isinstance(best, dict):
                    try:
                        obj = (best.get('object_primary') or '').strip()
                        subc = (best.get('subcomponent') or '').strip()
                        ftyp = (best.get('failure_type') or '').strip()
                        fzone = (best.get('failure_zone') or '').strip()
                        # Fallback desde OCR si faltan campos
                        if (not obj or not subc or not ftyp) and (locals().get('ocr_list') or []):
                            for oc in (ocr_list or []):
                                et = (oc.get('evidence_type') or '').lower() if isinstance(oc, dict) else ''
                                if et == 'fuga_llanta':
                                    obj = obj or 'llanta'
                                    subc = subc or 'rin'
                                    ftyp = ftyp or 'fuga de aire'
                                if et == 'grieta_rin':
                                    obj = obj or 'llanta'
                                    subc = subc or 'rin'
                                    ftyp = ftyp or 'grieta'
                        parts = []
                        if obj: parts.append(f"Objeto: {obj}")
                        if subc: parts.append(f"Subcomponente: {subc}")
                        if ftyp:
                            if fzone:
                                parts.append(f"Falla: {ftyp} (zona: {fzone})")
                            else:
                                parts.append(f"Falla: {ftyp}")
                        if parts:
                            extra_blocks.append(" · ".join(parts))
                    except Exception:
                        pass
                if best and best.get('part_guess'):
                    sys_txt = (best.get('system') or '').strip()
                    conf_txt = f"{best_conf:.2f}" if best_conf >= 0 else ""
                    if best_conf >= 0.86:
                        line = f"ES: {best.get('part_guess')}"
                    elif best_conf >= 0.55:
                        line = f"puede ser: {best.get('part_guess')}"
                    else:
                        line = None
                    if line:
                        if sys_txt:
                            line += f" · sistema {sys_txt}"
                        if conf_txt:
                            line += f" (conf {conf_txt})"
                        extra_blocks.append(line)
                # Fallback heurístico si no hubo guess
                if not best:
                    ut = (user_text or Body or '').lower()
                    if 'diferencial' in ut:
                        extra_blocks.append('puede ser: tapa de diferencial / sello de piñón (a confirmar)')
                    elif any(k in ut for k in ['refrigerante','anticongelante','garrafa','bidón','bidon']):
                        extra_blocks.append('puede ser: garrafa/etiqueta de refrigerante')
            except Exception:
                pass
            # Fallback: si no hay checks y el usuario menciona 'diferencial', sugiere básicos
            try:
                ut = (user_text or Body or '').lower()
                if (not ('rec_checks_agg' in locals() and rec_checks_agg)) and ('diferencial' in ut):
                    rec_checks_agg = [
                        'Limpia el housing y ubica el punto exacto de fuga',
                        'Revisa sello de piñón (yugo) y sellos de flecha',
                        'Aprieta/reemplaza junta de tapa del diferencial',
                        'Verifica tapones de dren/fill y respiradero',
                        'Rellena y verifica nivel según manual'
                    ]
            except Exception:
                pass
            if 'rec_checks_agg' in locals() and rec_checks_agg:
                checks_txt = "\n- ".join(rec_checks_agg[:6])
                extra_blocks.append("Recomendado revisar:\n- " + checks_txt)
            if 'oem_hits_agg' in locals() and oem_hits_agg:
                try:
                    oem_conf_min = _float_env('OEM_CONF_MIN', 0.6)
                    oem_score_min = _float_env('OEM_SCORE_MIN', 0.5)
                    best_conf_current = 0.0
                    try:
                        for cls in (locals().get('part_class_list') or []) or []:
                            try:
                                c = float(cls.get('confidence') or 0.0)
                                if c > best_conf_current:
                                    best_conf_current = c
                            except Exception:
                                continue
                    except Exception:
                        best_conf_current = 0.0
                    items = []
                    if best_conf_current >= oem_conf_min:
                        for h in oem_hits_agg[:5]:
                            try:
                                sc = float(h.get('score') or 0.0)
                            except Exception:
                                sc = 0.0
                            if sc < oem_score_min:
                                continue
                            o = str(h.get('oem') or '?')
                            pn = str(h.get('part_name') or '?')
                            pg = str(h.get('page_label') or '?')
                            items.append(f"{o} ({pn} · pág {pg})")
                    if items:
                        extra_blocks.append("Posibles OEM: " + ", ".join(items))
                except Exception:
                    pass
            if extra_blocks:
                core = core.rstrip() + "\n\n" + "\n".join(extra_blocks)
                # Quick replies específicos para flujo de refacciones/garantía
                try:
                    qr = _quick_replies_line(['Enviar a refacciones', 'Validar garantía'])
                    if qr:
                        core += "\n" + qr
                except Exception:
                    pass
        except Exception:
            pass

        # Cierre natural si no es crítico
        try:
            closers = ["¿Seguimos?", "¿Te late?", "¿Te va?", "¿Te ayudo con algo más?"]
            sev_now = (sig3.get('severity') if isinstance(sig3, dict) else None) or 'normal'
            if sev_now != 'critical':
                core = core.rstrip() + "\n\n" + random.choice(closers)
        except Exception:
            pass

        # Saludo por nombre (si viene en meta)
        try:
            prof = None
            if isinstance(payload.meta, dict):
                prof = payload.meta.get('profile_name') or payload.meta.get('ProfileName')
            if From and prof:
                from . import storage as _st
                case = _st.get_or_create_case(str(From))
                if not case.get('greeted'):
                    core = f"Hola {prof},\n\n" + core
                    _st.update_case(str(From), {'greeted': True})
        except Exception:
            pass

        result = {"answer": core}
        if From:
            result["case_id"] = case.get('id') if isinstance(case, dict) else None
        if pending:
            result["pending"] = pending
        else:
            # Evaluate warranty only if confirmed or not required
            try:
                require_confirm = _bool_env('WARRANTY_REQUIRE_CONFIRM', True)
                asked = _warranty_requested([user_text] + (transcripts if 'transcripts' in locals() and transcripts else []))
                if neon_case_id and (asked or not require_confirm):
                    from . import warranty as _w
                    w = _w.policy_evaluate(
                        neon_case_id,
                        fallback_category=((agg2.get('categories') or ['general'])[0]),
                        problem=(sig3.get('problem') if isinstance(sig3, dict) else None),
                    )
                    result["warranty"] = w
                    pending_block = (w or {}).get('pending_block') if isinstance(w, dict) else None
                    if pending_block:
                        closers = ["¿Seguimos?", "¿Te late?", "¿Te va?", "¿Te ayudo con algo más?"]
                        closer_phrase = None
                        core_base = core.rstrip()
                        for phrase in closers:
                            if core_base.endswith(phrase):
                                closer_phrase = phrase
                                core_base = core_base[: -len(phrase)].rstrip()
                                break
                        if core_base:
                            core = core_base + "\n\n" + pending_block
                        else:
                            core = pending_block
                        if closer_phrase:
                            core = core.rstrip() + "\n\n" + closer_phrase
                        result["answer"] = core
                elif require_confirm and not asked:
                    core += "\n\nSi deseas validar garantía: no se tramita automático; primero confirmo VIN/placa/odómetro/fecha de entrega. ¿Quieres que la validemos?"
                    if _bool_env('QUICK_REPLIES', True):
                        try:
                            import re as _re_qr
                            parts_hint = bool(_re_qr.search(r"\b[A-Z0-9]{2,}(?:[\-–][A-Z0-9]{2,})+\b", user_text or ''))
                        except Exception:
                            parts_hint = False
                        actions = ['Validar garantía', 'Seguir diagnóstico']
                        if parts_hint:
                            actions.append('Buscar refacción')
                        qr = _quick_replies_line(actions)
                        if qr:
                            core += "\n" + qr
                    result["answer"] = core
            except Exception:
                pass
        # Log outbound
        try:
            from . import storage as _st
            latency_ms = (time.perf_counter() - _t0) * 1000.0
            response_bytes = len((core or '').encode('utf-8'))
            _st.log_event(
                kind="whatsapp_out",
                payload={
                    "to": From,
                    "answer": core,
                    "channel": "whatsapp",
                    "latency_ms": latency_ms,
                    "response_bytes": response_bytes,
                }
            )
        except Exception:
            pass
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail="Processing error")


@app.get("/parts/equivalences")
def parts_equivalences(q: str, top_k: int = 5):
    """Devuelve equivalencias aftermarket/OEM a partir del catálogo consolidado."""
    return {"equivalences": _collect_equivalence_suggestions(q, limit=top_k, force=True)}


@app.get("/parts/search")
def parts_search(name: str, top_k: int = 3, include_equivalences: bool = False):
    """Busca refacciones en el catálogo local extraído (parts_index.json)."""
    parts_index_file = os.getenv("PARTS_INDEX_FILE", "parts_index.json")
    candidates = [parts_index_file, os.path.join(os.path.dirname(__file__), parts_index_file)]
    data = None
    for p in candidates:
        try:
            if p and os.path.exists(p):
                with open(p, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    break
        except Exception:
            continue
    if not data:
        equivs = _collect_equivalence_suggestions(name, force=include_equivalences)
        return {"items": [], "equivalences": equivs}
    import re as _re
    def token_set(s: str):
        normalized = _normalize_ascii(s).lower()
        base_tokens = set(_re.findall(r"\w+", normalized))
        extended = set(base_tokens)
        for tok in list(base_tokens):
            if tok.endswith('es') and len(tok) > 3:
                extended.add(tok[:-2])
            if tok.endswith('s') and len(tok) > 3:
                extended.add(tok[:-1])
        return extended
    def jaccard(a: str, b: str) -> float:
        sa, sb = token_set(a), token_set(b)
        if not sa or not sb:
            return 0.0
        inter = len(sa & sb)
        union = len(sa | sb)
        return inter/union
    items = []
    # 1) OEM exacto si viene en la consulta
    m = _re.search(r"\b[A-Z0-9]{2,}(?:[\-–][A-Z0-9]{2,})+\b", name)
    if m:
        oem_q = m.group(0)
        exact = [it for it in data.get('items', []) if (it.get('oem') or '').upper() == oem_q.upper()]
        if exact:
            items = [{**it, 'score': 1.0} for it in exact][:max(1, min(top_k, 10))]
            include = include_equivalences or _wants_equivalences([name])
            equivs = _collect_equivalence_suggestions(name, limit=max(top_k, 5), force=include)
            return {"items": items, "equivalences": equivs}
    # 2) Sinónimos básicos ES↔EN para mayor recall
    synonyms = {
        'filtro de aceite': ['filtro de aceite', 'filtro aceite', 'oil filter'],
        'bomba de aceite': ['bomba de aceite', 'bomba aceite', 'oil pump'],
        'sello de aceite': ['sello de aceite', 'sello aceite', 'oil seal'],
        'correa de distribución': ['correa de distribución', 'banda de distribución', 'timing belt', 'timing chain'],
        'bujía': ['bujia', 'bujía', 'bujias', 'bujías', 'spark plug', 'sparkplugs'],
        'bomba de agua': ['bomba de agua', 'water pump', 'bombaagua'],
        'cadena de distribución': ['cadena de distribución', 'cadena de tiempo', 'timing chain'],
    }
    name_l = _normalize_ascii(name).lower()
    variants = {name, _normalize_ascii(name)} if name else set()
    variants = {v for v in variants if v}
    for group, words in synonyms.items():
        normalized_words = [ _normalize_ascii(w) for w in words ]
        if any(w in name_l for w in normalized_words):
            variants.update(words)
            variants.update(normalized_words)
    # 3) Puntuar por el mejor match entre variantes
    scored = []
    for it in data.get('items', []):
        target = it.get('part_name', '') or ''
        best = 0.0
        for v in variants:
            s = jaccard(v, target)
            if s > best:
                best = s
        if best > 0:
            scored.append({**it, 'score': best})
    scored.sort(key=lambda x: x['score'], reverse=True)
    items = scored[:max(1, min(top_k, 10))]
    request_texts = [name]
    include = include_equivalences or _wants_equivalences(request_texts)
    equivs = _collect_equivalence_suggestions(name, limit=max(top_k, 5), force=include)
    return {"items": items, "equivalences": equivs}


@app.get("/search")
def hybrid_search(q: str, top_k: int = 10):
    """Búsqueda híbrida: catálogo de partes + Pinecone vectorial (filtrado por brand/model si aplica)."""
    results = []
    # 1) Catálogo local
    try:
        cat = parts_search(q, top_k=top_k).get('items', [])
        for it in cat:
            results.append({
                "type": "catalog",
                "part_name": it.get('part_name'),
                "oem": it.get('oem'),
                "page_label": it.get('page_label'),
                "score": it.get('score', 0),
            })
    except Exception:
        pass

    # 2) Pinecone vectorial
    try:
        from langchain_openai import OpenAIEmbeddings
        from pinecone import Pinecone as PineconeClient
        emb_model = _embeddings_model()
        embeddings = OpenAIEmbeddings(model=emb_model)
        vec = embeddings.embed_query(q)
        pc = PineconeClient(api_key=os.getenv("PINECONE_API_KEY"))
        idx = pc.Index(os.getenv("PINECONE_INDEX", "ssot-higer"))
        flt = {}
        if os.getenv("BRAND_NAME"):
            flt["brand"] = os.getenv("BRAND_NAME")
        if os.getenv("MODEL_NAME"):
            flt["model"] = os.getenv("MODEL_NAME")
        pine = idx.query(vector=vec, top_k=min(16, top_k), include_metadata=True, filter=flt or None)
        matches = pine.get('matches', [])
        # normaliza score y adapta campos
        for m in matches:
            md = m.get('metadata', {})
            results.append({
                "type": md.get('source') or 'vector',
                "page_label": md.get('page_label', md.get('page')),
                "snippet": (md.get('text') or '')[:180],
                "score": float(m.get('score', 0)),
                "oem": md.get('oem'),
                "part_name": md.get('part_name'),
            })
    except Exception:
        pass

    # Ordenar por score descendente y truncar
    results.sort(key=lambda x: x.get('score', 0), reverse=True)
    return {"items": results[:max(1, min(top_k, 25))]}
# Registrar rutas de casos (Fase 1)
try:
    from .cases_api import router as cases_router
    app.include_router(cases_router)
except Exception:
    pass
def _embedding_cache_get(key: str) -> Optional[list[float]]:
    if not key:
        return None
    with _embed_cache_lock:
        vec = _EMBEDDING_CACHE.get(key)
        if vec is not None:
            _EMBEDDING_CACHE.move_to_end(key)
            return list(vec)
    return None


def _embedding_cache_set(key: str, vector: list[float]):
    if not key:
        return
    with _embed_cache_lock:
        _EMBEDDING_CACHE[key] = list(vector)
        _EMBEDDING_CACHE.move_to_end(key)
        while len(_EMBEDDING_CACHE) > _EMBED_CACHE_LIMIT:
            _EMBEDDING_CACHE.popitem(last=False)
