import json
import os
from pathlib import Path
import re as _re

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent


def load_parts_catalog(path_env: str = 'PARTS_INDEX_FILE'):
    path = os.getenv(path_env, 'parts_index.json')
    cands = [path]
    try:
        cands.append(str((ROOT_DIR / path).resolve()))
    except Exception:
        pass
    for p in cands:
        try:
            if p and os.path.exists(p):
                with open(p, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception:
            continue
    return None


def _token_set(s: str):
    import re as _re2
    return set(_re2.findall(r"\w+", (s or '').lower()))


def _fuzzy_score(a: str, b: str) -> float:
    sa, sb = _token_set(a), _token_set(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union


def search_parts_catalog(query: str, top_k: int = 3):
    cat = load_parts_catalog()
    if not cat:
        return []
    items = cat.get('items', [])
    # OEM exacto en query
    m = _re.search(r"\b[A-Z0-9]{2,}(?:[\-–][A-Z0-9]{2,})+\b", query or '')
    if m:
        oem_q = m.group(0)
        exact = [it for it in items if (it.get('oem') or '').upper() == oem_q.upper()]
        if exact:
            return [{**it, 'score': 1.0} for it in exact[:top_k]]
    # fuzzy por nombre
    scored = []
    for it in items:
        name = it.get('part_name', '')
        score = _fuzzy_score(query or '', name)
        if score > 0:
            scored.append({**it, 'score': score})
    scored.sort(key=lambda x: x.get('score', 0), reverse=True)
    return scored[:top_k]
