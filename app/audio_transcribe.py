import logging
import os
import time
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent

def _twilio_creds_from_legacy_file() -> Tuple[Optional[str], Optional[str]]:
    paths = [
        APP_DIR / 'secrets.local.txt',
        ROOT_DIR / 'secrets.local.txt',
        Path('secrets.local.txt'),
        APP_DIR / 'secrets.loca.txt',
        ROOT_DIR / 'secrets.loca.txt',
        Path('secrets.loca.txt'),
    ]
    sid = None
    token = None
    try:
        for p in paths:
            if not os.path.exists(p):
                continue
            with open(p, 'r', encoding='utf-8', errors='ignore') as f:
                lines = [ln.strip() for ln in f.readlines()]
            for i, ln in enumerate(lines):
                low = ln.lower()
                if ('account sid' in low or low == 'twilio sid' or low == 'twilio') and i + 1 < len(lines):
                    j = i + 1
                    while j < len(lines) and not lines[j]:
                        j += 1
                    if j < len(lines) and lines[j].startswith('AC'):
                        sid = lines[j]
                if ('auth token' in low or low == 'twilio auth token') and i + 1 < len(lines):
                    j = i + 1
                    while j < len(lines) and not lines[j]:
                        j += 1
                    if j < len(lines) and len(lines[j]) >= 20:
                        token = lines[j]
        return sid, token
    except Exception:
        return None, None

def _fetch_bytes(url: str) -> Optional[bytes]:
    try:
        import httpx
    except Exception:
        return None
    auth = None
    sid = os.getenv('TWILIO_SID') or os.getenv('TWILIO_ACCOUNT_SID')
    token = os.getenv('TWILIO_AUTH_TOKEN')
    if not (sid and token):
        fsid, ftoken = _twilio_creds_from_legacy_file()
        sid = sid or fsid
        token = token or ftoken
    if sid and token:
        auth = (sid, token)
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True) as cli:
            r = cli.get(url, auth=auth)
            r.raise_for_status()
            return r.content
    except httpx.TimeoutException:
        try:
            from . import storage as _st  # type: ignore
            _st.log_event('media_fetch_timeout', {'url': url, 'media_type': 'audio'})
        except Exception:
            pass
        logger.warning("audio_transcribe: timeout fetching media %s", url)
        return None
    except Exception:
        try:
            from . import storage as _st  # type: ignore
            _st.log_event('media_fetch_failed', {'url': url, 'media_type': 'audio'})
        except Exception:
            pass
        return None


def transcribe_audio_from_url(url: str) -> Optional[str]:
    """Descarga audio desde URL (Twilio media) y transcribe con OpenAI Whisper.
    Devuelve texto o None si falla.
    """
    api_key = os.getenv('OPENAI_API_KEY')
    if not api_key:
        try:
            from . import storage as _st  # type: ignore
            _st.log_event('asr_error', {'url': url, 'reason': 'missing_openai_api_key'})
        except Exception:
            pass
        return None
    data = _fetch_bytes(url)
    if not data:
        return None
    audio_size = len(data)
    asr_model = (os.getenv('ASR_MODEL', 'whisper-1') or 'whisper-1').strip() or 'whisper-1'
    start = time.perf_counter()
    _openai = None
    try:
        from openai import OpenAI  # type: ignore
        import openai as _openai  # type: ignore
        import io
        client = OpenAI(api_key=api_key)
        timeout_val = None
        timeout_env = os.getenv('ASR_TIMEOUT_SECONDS')
        if timeout_env is not None and timeout_env.strip():
            try:
                timeout_val = float(timeout_env.strip())
            except Exception:
                timeout_val = None
        if timeout_val is None:
            timeout_val = 45.0
        if timeout_val and timeout_val > 0:
            client = client.with_options(timeout=timeout_val)
        bio = io.BytesIO(data)
        bio.name = 'audio.ogg'
        # Sugerir lenguaje y contexto para mejorar precisión en español técnico
        out = client.audio.transcriptions.create(
            model=asr_model,
            file=bio,
            language='es',
            temperature=0,
            prompt='Contexto: diagnóstico mecánico postventa Higer (español). Menciona síntomas, testigos, frenos, aceite, temperatura, humo.'
        )
        # SDK devuelve objeto; tomar texto
        text = getattr(out, 'text', None)
        if isinstance(text, str) and text.strip():
            cleaned = text.strip()
            duration_ms = (time.perf_counter() - start) * 1000.0
            char_count = len(cleaned)
            word_count = len(cleaned.split()) if cleaned else 0
            try:
                from . import storage as _st  # type: ignore
                _st.log_event('asr_success', {
                    'url': url,
                    'model': asr_model,
                    'duration_ms': duration_ms,
                    'audio_bytes': audio_size,
                    'char_count': char_count,
                    'word_count': word_count,
                })
            except Exception:
                pass
            # Alertar transcripciones sospechosamente cortas
            min_words = int(os.getenv('ASR_ALERT_MIN_WORDS', '4') or '4')
            min_chars = int(os.getenv('ASR_ALERT_MIN_CHARS', '12') or '12')
            if word_count < min_words and char_count < min_chars:
                try:
                    from . import storage as _st  # type: ignore
                    _st.log_event('asr_short_transcript', {
                        'url': url,
                        'model': asr_model,
                        'duration_ms': duration_ms,
                        'char_count': char_count,
                        'word_count': word_count,
                    })
                except Exception:
                    pass
                logger.warning(
                    "audio_transcribe: short transcript detected (chars=%s, words=%s) for %s",
                    char_count,
                    word_count,
                    url,
                )
            return cleaned
        # fallback
        try:
            return str(out)
        except Exception:
            return None
    except Exception as e:
        duration_ms = (time.perf_counter() - start) * 1000.0
        timeout_error = getattr(_openai, 'APITimeoutError', None) if _openai else None
        if timeout_error and isinstance(e, timeout_error):
            try:
                from . import storage as _st  # type: ignore
                _st.log_event('asr_timeout', {'url': url, 'model': asr_model, 'duration_ms': duration_ms})
            except Exception:
                pass
            logger.warning("audio_transcribe: OpenAI timeout for %s", url)
            return None
        try:
            from . import storage as _st  # type: ignore
            _st.log_event('asr_error', {'url': url, 'reason': 'openai_whisper_exception', 'error': str(e), 'duration_ms': duration_ms})
        except Exception:
            pass
        return None
