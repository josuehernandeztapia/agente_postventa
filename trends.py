import os
import json
from pathlib import Path
from datetime import datetime, timezone
from math import pow
from typing import Dict, Any

_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except Exception:
        return None


def _keywords_env() -> Dict[str, list[str]]:
    base = {
        'catalizador': ['catalizador', 'catalítico', 'catalitico', 'convertidor catalitico', 'convertidor catalítico', 'p0420', 'cat'],
        'sensor': ['sensor', 'captador', 'sonda', 'switch', 'transductor', 'sensor de oxígeno', 'sensor de oxigeno', 'o2', 'lambda'],
    }
    extra = os.getenv('TREND_KEYWORDS', '')
    if extra:
        # Formato: clave1=pal1|pal2;clave2=palA|palB
        try:
            for chunk in extra.split(';'):
                if not chunk.strip():
                    continue
                k, v = chunk.split('=', 1)
                vals = [w.strip() for w in v.split('|') if w.strip()]
                if vals:
                    base[k.strip().lower()] = vals
        except Exception:
            pass
    return base


def _decay_weight(age_days: float, half_life: float) -> float:
    if age_days <= 0:
        return 1.0
    if half_life <= 0:
        return 1.0
    # 0.5 ** (age/half_life)
    return pow(0.5, age_days / half_life)


def _scan_logs(window_days: int, half_life: float) -> dict:
    base = Path(__file__).parent
    log = base / 'logs' / 'events.jsonl'
    cats: Dict[str, float] = {}
    evs: Dict[str, float] = {}
    kws: Dict[str, float] = {}
    kw_map = _keywords_env()
    now = _now_ts()
    if not log.exists():
        return {'categories': {}, 'evidence': {}, 'keywords': {}}
    try:
        with log.open('r', encoding='utf-8') as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts = rec.get('ts')
                dt = _parse_iso(str(ts)) if ts else None
                if not dt:
                    continue
                age_days = max(0.0, (now - dt.timestamp()) / 86400.0)
                if age_days > float(max(1, window_days)):
                    continue
                w = _decay_weight(age_days, half_life)
                sig = rec.get('signals') or {}
                cat = (sig.get('category') or '').strip().lower()
                if cat:
                    cats[cat] = cats.get(cat, 0.0) + w
                # evidence inferida desde texto del evento
                text = (rec.get('question') or rec.get('text') or '')
                ev = _infer_evidence_from_text(text)
                if ev:
                    evs[ev] = evs.get(ev, 0.0) + w
                # keywords por texto
                tl = str(text).lower()
                for k, terms in kw_map.items():
                    if any(t in tl for t in terms):
                        kws[k] = kws.get(k, 0.0) + w
    except Exception:
        pass
    # normalizar 0..1
    def norm(d: Dict[str, float]) -> Dict[str, float]:
        if not d:
            return {}
        mx = max(d.values())
        if mx <= 0:
            return {k: 0.0 for k in d}
        return {k: min(1.0, v / mx) for k, v in d.items()}
    return {'categories': norm(cats), 'evidence': norm(evs), 'keywords': norm(kws)}


def trends() -> dict:
    enable = (os.getenv('TREND_ENABLE', '1').strip().lower() in {'1', 'true', 'yes', 'on'})
    if not enable:
        return {'categories': {}, 'evidence': {}, 'keywords': {}}
    # Cache 60s
    if _CACHE['data'] and (_now_ts() - _CACHE['ts'] < 60.0):
        return _CACHE['data']
    window = int(os.getenv('TREND_WINDOW_DAYS', '14') or '14')
    half = float(os.getenv('TREND_DECAY_HALF_LIFE_DAYS', '7') or '7')
    data = _scan_logs(window, half)
    _CACHE['data'] = data
    _CACHE['ts'] = _now_ts()
    return data


def _infer_evidence_from_text(text: str | None) -> str | None:
    t = (text or '').lower()
    if not t:
        return None
    if any(w in t for w in ['rin', 'rín', 'llanta', 'neumát', 'neumat']) and any(w in t for w in ['burbuja', 'burbuj', 'espuma', 'jabón', 'jabon', 'agua jabonosa', 'burbujeo']):
        return 'fuga_llanta'
    if any(w in t for w in ['grieta', 'fisura', 'rajadura']) and any(w in t for w in ['rin', 'rín', 'aro']):
        return 'grieta_rin'
    if any(w in t for w in ['odómetro', 'odometro']):
        return 'odometro'
    if 'placa' in t:
        return 'placa_unidad'
    if 'vin' in t or 'número de serie' in t or 'numero de serie' in t:
        return 'vin_plate'
    if 'tablero' in t or 'testigo' in t or 'warning' in t:
        return 'tablero'
    if 'fuga' in t and any(w in t for w in ['aceite', 'refrigerante', 'frenos', 'líquido', 'liquido']):
        return 'fuga_liquido'
    return None


def trend_boost(meta: dict | None, query_text: str | None) -> float:
    try:
        d = trends()
        if not d:
            return 0.0
        score = 0.0
        md = meta or {}
        # category match
        cat = (md.get('category') or '').strip().lower()
        if cat and d['categories'].get(cat):
            score = max(score, float(d['categories'][cat]))
        # evidence match
        ev = (md.get('evidence_type') or '').strip().lower()
        if ev and d['evidence'].get(ev):
            score = max(score, float(d['evidence'][ev]))
        # keyword match (query ∧ item)
        tl_q = (query_text or '').lower()
        tl_item = (md.get('text') or '').lower()
        for k, _val in (d.get('keywords') or {}).items():
            terms = _keywords_env().get(k, [])
            if any(t in tl_q for t in terms) and any(t in tl_item for t in terms):
                score = max(score, float(_val))
        return max(0.0, min(1.0, score))
    except Exception:
        return 0.0

