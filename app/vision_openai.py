import os
import base64
import logging
import time
from pathlib import Path
from typing import Optional, Tuple
import re

logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent

def _twilio_creds_from_legacy_file() -> Tuple[Optional[str], Optional[str]]:
    """Intenta leer TWILIO_SID y TWILIO_AUTH_TOKEN desde secrets.local.txt/loca.txt en formato libre.
    No imprime secretos. Solo para fallback local.
    """
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
                    # siguiente no vacío
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

def _fetch_bytes_with_twilio_auth(url: str) -> Tuple[Optional[bytes], Optional[str]]:
    """Descarga bytes desde URL (incluye soporte de auth Twilio). Devuelve (bytes, content_type)."""
    try:
        import httpx  # type: ignore
    except Exception:
        return None, None
    auth = None
    sid = os.getenv('TWILIO_SID') or os.getenv('TWILIO_ACCOUNT_SID')
    token = os.getenv('TWILIO_AUTH_TOKEN')
    if not (sid and token):
        fsid, ftoken = _twilio_creds_from_legacy_file()
        sid = sid or fsid
        token = token or ftoken
    # Twilio media requiere Basic Auth; si hay credenciales, úsalas siempre
    if sid and token:
        auth = (sid, token)
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True) as cli:
            r = cli.get(url, auth=auth)
            r.raise_for_status()
            ctype = r.headers.get('Content-Type')
            return r.content, ctype
    except httpx.TimeoutException:
        try:
            from . import storage as _st  # type: ignore
            _st.log_event('media_fetch_timeout', {'url': url, 'media_type': 'image'})
        except Exception:
            pass
        logger.warning("vision_openai: timeout fetching media %s", url)
        return None, None
    except Exception:
        try:
            from . import storage as _st  # type: ignore
            _st.log_event('media_fetch_failed', {'url': url, 'media_type': 'image'})
        except Exception:
            pass
        return None, None

def _to_data_url(b: bytes, content_type: Optional[str]) -> str:
    ctype = content_type or 'image/jpeg'
    b64 = base64.b64encode(b).decode('utf-8')
    return f"data:{ctype};base64,{b64}"

def ocr_image_openai(image_url: str, kind: str = "evidencia") -> Optional[dict]:
    """OCR con OpenAI Vision.
    - Si la URL es de Twilio u otra protegida, descarga con auth y manda como data URL.
    - Extrae: plate, vin, odo_km, delivered_at, evidence_type, notes.
    Devuelve dict o None si falla.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        try:
            from . import storage as _st  # type: ignore
            _st.log_event('ocr_error', {'url': image_url, 'reason': 'missing_openai_api_key'})
        except Exception:
            pass
        return None

    # Preparar image_url (data URL si posible)
    input_image_url = image_url
    image_size = None
    try:
        b, ctype = _fetch_bytes_with_twilio_auth(image_url)
        if b:
            image_size = len(b)
            input_image_url = _to_data_url(b, ctype)
    except Exception:
        pass

    ocr_model = os.getenv("OCR_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
    start = time.perf_counter()
    _openai = None

    try:
        from openai import OpenAI  # type: ignore
        import openai as _openai  # type: ignore
        client = OpenAI(api_key=api_key)
        timeout_val = None
        timeout_env = os.getenv('OCR_TIMEOUT_SECONDS')
        if timeout_env is not None and timeout_env.strip():
            try:
                timeout_val = float(timeout_env.strip())
            except Exception:
                timeout_val = None
        if timeout_val is None:
            timeout_val = 45.0
        if timeout_val and timeout_val > 0:
            client = client.with_options(timeout=timeout_val)
        prompt = (
            f"Eres extractor. Imagen de {kind}. Devuelve SOLO JSON con campos cuando apliquen.\n"
            "Campos: plate, vin (17), odo_km (int), delivered_at (YYYY-MM-DD), evidence_type, notes.\n"
            "Clasificación evidence_type (elige la más precisa):\n"
            "- 'odometro' (si es odómetro/cluster con km)\n"
            "- 'vin_plate' (placa VIN/serie)\n"
            "- 'placa_unidad' (placa de circulación)\n"
            "- 'tablero' (testigos o mensajes en tablero)\n"
            "- 'conector' (conector/cableado visible)\n"
            "- 'fuga_llanta' (burbujas/espuma/agua jabonosa en llanta/rin → fuga de aire)\n"
            "- 'grieta_rin' (fisura/porosidad en rin)\n"
            "- 'fuga_liquido' (líquido visible: aceite, refrigerante, frenos)\n"
            "- 'otro'\n"
            "Si ves burbujas/espuma con líquido jabonoso en rin/llanta, usa 'fuga_llanta' y describe zona (talón/valvula/soldadura).\n"
            "Si no estás seguro, usa 'otro' y explica en 'notes' qué se ve."
        )
        raw = None
        # Intento 1: Responses API (mejor soporte para imágenes en modelos 4o)
        try:
            rsp = client.responses.create(
                model=ocr_model,
                input=[{
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": input_image_url},
                    ],
                }],
                temperature=0.0,
                response_format={"type": "json_object"},
            )
            try:
                raw = rsp.output_text
            except Exception:
                # Algunas versiones exponen 'output' como lista de bloques
                raw = getattr(rsp, 'output', None) or None
                if raw is not None:
                    raw = str(raw)
        except Exception:
            raw = None
        # Intento 2 (fallback): Chat Completions
        if not raw:
            msg = client.chat.completions.create(
                model=ocr_model,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": input_image_url}},
                    ],
                }],
                temperature=0.0,
                response_format={"type": "json_object"},
            )
            raw = msg.choices[0].message.content
        import json
        try:
            data = json.loads(raw)
        except Exception:
            data = {"raw": raw}
        # Fallback: intentar extraer odómetro desde notas/raw si falta
        try:
            if isinstance(data, dict) and (data.get('odo_km') is None):
                txt = ''
                for k in ('notes','raw','text','description'):
                    v = data.get(k)
                    if isinstance(v, str):
                        txt += ' ' + v
                if txt.strip():
                    t = txt.lower()
                    NUM = r"(\d{1,3}(?:[\.,\s]\d{3})+|\d{4,7})"
                    def _to_int(num_str: str) -> Optional[int]:
                        try:
                            return int(re.sub(r"[\s\.,]", "", num_str))
                        except Exception:
                            return None
                    # 1) n + (km/kms/kilometros/kilómetros) evitando km/h
                    m = re.search(rf"\b{NUM}\b\s*(?:km|kms|kilometros|kilómetros)\b(?!\s*/\s*h)", t, flags=re.IGNORECASE)
                    if m:
                        val = _to_int(m.group(1))
                        if val is not None:
                            data['odo_km'] = val
                            data.setdefault('evidence_type','odometro')
                            return data
                    # 2) (km/...) + n  evitando km/h
                    m = re.search(rf"\b(?:km|kms|kilometros|kilómetros)\b(?!\s*/\s*h)\s*[:=\- ]*\b{NUM}\b", t, flags=re.IGNORECASE)
                    if m:
                        val = _to_int(m.group(1))
                        if val is not None:
                            data['odo_km'] = val
                            data.setdefault('evidence_type','odometro')
                            return data
                    # 3) Cerca de odómetro/odo sin 'km'
                    if re.search(r"od[oó]metro|\bodo\b", t, flags=re.IGNORECASE):
                        cands = re.findall(rf"\b{NUM}\b", txt)
                        if cands:
                            best = None
                            best_val = -1
                            for c in cands:
                                val = _to_int(c)
                                if val is not None and val > best_val:
                                    best_val = val
                                    best = val
                            if best is not None:
                                data['odo_km'] = best
                                data.setdefault('evidence_type','odometro')
                                return data
                    # 4) Fallback: mayor 4–7 dígitos (no tiempo), asumiendo confirmación posterior
                    digits = re.findall(r"\b\d{4,7}\b", t)
                    if digits:
                        try:
                            val = max((int(x) for x in digits), default=None)
                            if val is not None:
                                data['odo_km'] = val
                                data.setdefault('evidence_type','odometro')
                                return data
                        except Exception:
                            pass
                    # 5) Segundo intento con prompt específico de odómetro
                    try:
                        from openai import OpenAI  # type: ignore
                        client2 = OpenAI(api_key=api_key)
                        ocr_model2 = os.getenv("OCR_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
                        odo_prompt = (
                            "Extrae EXCLUSIVAMENTE la lectura de odómetro (kilometraje total) si está visible en la imagen. "
                            "Ignora horas (patrones como 1:23), ignora velocidad (km/h) y parciales. "
                            "Normaliza separadores (1,480 → 1480). Si no hay odómetro claro, responde {\"odo_km\": null}."
                        )
                        msg2 = client2.chat.completions.create(
                            model=ocr_model2,
                            messages=[{
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": odo_prompt},
                                    {"type": "image_url", "image_url": {"url": input_image_url}},
                                ],
                            }],
                            temperature=0.0,
                            response_format={"type": "json_object"},
                        )
                        raw2 = msg2.choices[0].message.content
                        import json as _json2
                        try:
                            d2 = _json2.loads(raw2)
                            val = d2.get('odo_km')
                            if isinstance(val, (int, float)):
                                data['odo_km'] = int(val)
                                data.setdefault('evidence_type','odometro')
                                return data
                            if isinstance(val, str):
                                v = re.sub(r"[\s\.,]", "", val)
                                if v.isdigit():
                                    data['odo_km'] = int(v)
                                    data.setdefault('evidence_type','odometro')
                                    return data
                        except Exception:
                            pass
                    except Exception:
                        pass
        except Exception:
            pass
        # Pase adicional: placa/VIN/fecha si faltan
        try:
            need_plate = (isinstance(data, dict) and not data.get('plate'))
            need_vin = (isinstance(data, dict) and not data.get('vin'))
            need_date = (isinstance(data, dict) and not data.get('delivered_at'))
            if need_plate or need_vin or need_date:
                from openai import OpenAI  # type: ignore
                client3 = OpenAI(api_key=api_key)
                ocr_model3 = os.getenv("OCR_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
                pv_prompt = (
                    "Si está visible, responde JSON con: "
                    "plate (placa), vin (17 caracteres), delivered_at (YYYY-MM-DD). "
                    "Incluye solo llaves con valor."
                )
                msg3 = client3.chat.completions.create(
                    model=ocr_model3,
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": pv_prompt},
                            {"type": "image_url", "image_url": {"url": input_image_url}},
                        ],
                    }],
                    temperature=0.0,
                    response_format={"type": "json_object"},
                )
                raw3 = msg3.choices[0].message.content
                import json as _json3
                try:
                    d3 = _json3.loads(raw3)
                except Exception:
                    d3 = {"raw": raw3}
                if isinstance(d3, dict):
                    if need_plate and d3.get('plate'):
                        data['plate'] = d3.get('plate')
                    if need_vin and d3.get('vin'):
                        data['vin'] = d3.get('vin')
                    if need_date and d3.get('delivered_at'):
                        data['delivered_at'] = d3.get('delivered_at')
        except Exception:
            pass
        # Refinamiento heurístico de clasificación (mejora casos de rin/llanta)
        try:
            t = " ".join([str(x) for x in [data.get('notes'), data.get('raw')] if x])
            tl = (t or '').lower()
            # Indicadores de rin/llanta + burbujas/espuma → fuga de aire en llanta
            if any(k in tl for k in ["rin", "rín", "llanta", "neumát", "neumat"]) and any(k in tl for k in ["burbuja", "burbujas", "espuma", "jabón", "jabon", "burbujeo"]):
                data['evidence_type'] = data.get('evidence_type') or 'fuga_llanta'
            # Si menciona fisura/grieta por rin
            if any(k in tl for k in ["grieta", "fisura", "rajadura"]) and any(k in tl for k in ["rin", "rín", "aro"]):
                data['evidence_type'] = 'grieta_rin'
        except Exception:
            pass
        duration_ms = (time.perf_counter() - start) * 1000.0
        if isinstance(data, dict):
            try:
                from . import storage as _st  # type: ignore
                _st.log_event('ocr_success', {
                    'url': image_url,
                    'model': ocr_model,
                    'duration_ms': duration_ms,
                    'image_bytes': image_size,
                    'has_vin': bool(data.get('vin')),
                    'has_plate': bool(data.get('plate')),
                    'has_odo': data.get('odo_km') is not None,
                    'evidence_type': data.get('evidence_type'),
                })
            except Exception:
                pass
        return data
    except Exception as e:
        duration_ms = (time.perf_counter() - start) * 1000.0
        timeout_error = getattr(_openai, 'APITimeoutError', None) if _openai else None
        if timeout_error and isinstance(e, timeout_error):
            try:
                from . import storage as _st  # type: ignore
                _st.log_event('ocr_timeout', {'url': image_url, 'model': ocr_model, 'duration_ms': duration_ms})
            except Exception:
                pass
            logger.warning("vision_openai: OpenAI timeout for %s", image_url)
            return None
        try:
            from . import storage as _st  # type: ignore
            _st.log_event('ocr_error', {'url': image_url, 'reason': 'openai_vision_exception', 'error': str(e), 'duration_ms': duration_ms})
        except Exception:
            pass
        return None


def parse_postventa_form(image_url: str) -> Optional[dict]:
    """Detecta si la imagen es un formulario de postventa/garantía y extrae campos estructurados.
    Devuelve un dict con 'form_type' (postventa|garantia|otro), 'completo' (bool), 'faltantes' (list),
    y campos como cliente, contacto, vin, placa, kilometraje_km, fecha, sintomas, acciones, piezas, etc.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None


def classify_part_image(image_url: str) -> Optional[dict]:
    """Clasifica una imagen de pieza/componente o insumo (garrafa/etiqueta) con reintento guiado y lookup OEM.
    Devuelve: part_guess, synonyms, system, condition, damage_signs, evidence_type,
    recommended_checks, risk_level, confidence, ask_user, notes, oem_hits (lista),
    y cuando aplique para insumos: brand, fluid_guess (oil/diff_oil/atf/coolant/brake_fluid/washer), color, standard.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    input_image_url = image_url
    try:
        b, ctype = _fetch_bytes_with_twilio_auth(image_url)
        if b:
            input_image_url = _to_data_url(b, ctype)
    except Exception:
        pass
    try:
        from openai import OpenAI  # type: ignore
        client = OpenAI(api_key=api_key)
        ocr_model = os.getenv("OCR_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
        COMP_KB = [
            {"name":"catalizador (convertidor catalítico)","system":"escape","syn":"catalizador|convertidor|cat|p0420|silenciador cercano a sensor O2","cues":"lata metálica oval/cilíndrica en línea de escape, a veces con sensor roscado"},
            {"name":"sensor O2 / lambda","system":"escape","syn":"sensor|o2|lambda|sonda","cues":"sensor roscado con cable en el tubo de escape"},
            {"name":"depósito de anticongelante","system":"refrigeración","syn":"tanque|depósito|expansión|expansion","cues":"recipiente plástico translúcido con tapa, mangueras al radiador"},
            {"name":"radiador","system":"refrigeración","syn":"radiador","cues":"panel aletado, ventiladores cercanos"},
            {"name":"manguera de refrigerante","system":"refrigeración","syn":"manguera|hose","cues":"tubo de goma con abrazaderas, posible fuga/escurrimiento"},
            {"name":"amortiguador","system":"suspensión","syn":"amortiguador|shock","cues":"cilindro cerca de rueda, posible fuga aceite"},
            {"name":"balata / pastilla de freno","system":"frenos","syn":"balata|pastilla","cues":"pieza rectangular en mordaza, desgaste irregular"},
            {"name":"rin","system":"carrocería","syn":"rin|aro","cues":"aro metálico rueda, posible fisura/fuga llanta"},
            {"name":"llanta","system":"carrocería","syn":"llanta|neumático","cues":"banda de rodadura, desgaste, burbujas"},
            {"name":"alternador","system":"eléctrico","syn":"alternador","cues":"cuerpo con polea y ventilado"},
            {"name":"bomba (genérica)","system":"motor","syn":"bomba","cues":"cuerpo con conexiones y mangueras"},
            {"name":"filtro de aceite","system":"motor","syn":"filtro aceite","cues":"cilindro atornillado al bloque"},
            {"name":"tapa de diferencial / cárter de diferencial","system":"powertrain","syn":"diferencial|tapa diferencial|cárter diferencial|fuga diferencial|gear oil","cues":"carcasa trasera con tornillos perimetrales; aceite viscoso en housing/eje"},
            {"name":"sello de piñón (diferencial)","system":"powertrain","syn":"sello piñón|pinion seal|fuga en yugo|salida cardán","cues":"fuga en entrada del diferencial (yugo) donde conecta el cardán"},
            {"name":"sello de flecha/retén de eje","system":"powertrain","syn":"sello flecha|retén eje|axle seal","cues":"fuga en extremos del eje, cerca de las ruedas"},
            {"name":"garrafa/etiqueta de refrigerante","system":"refrigeración","syn":"garrafa|bidón|etiqueta|label|coolant|refrigerante|anticongelante|valucraft","cues":"envase con etiqueta de marca, color indicado (verde/rojo/azul), texto de especificación"},
        ]
        kb_txt = "\n".join([f"- {c['name']} · sistema={c['system']} · sinónimos={c['syn']} · cues={c['cues']}" for c in COMP_KB])
        base_prompt = (
            "Eres técnico de postventa Higer. Analiza la imagen y clasifica la pieza.\n"
            "Catálogo de referencia (ayuda a tu reconocimiento):\n" + kb_txt + "\n\n"
            "Reglas: nombra la pieza lo MÁS específico posible (español). Si ambiguo, elige la más probable y agrega 'confidence'.\n"
            "Determina 'system' (motor/escape/refrigeración/frenos/suspensión/eléctrico/carrocería/powertrain/otro).\n"
            "Determina 'condition': ok/usada/gastada/dañada/no_clara.\n"
            "Si ves burbujas/escurrimientos/manchas, marca 'evidence_type=fuga_liquido'.\n"
            "Si son garrafas/etiquetas de insumos, devuelve además brand (marca), fluid_guess (oil/diff_oil/atf/coolant/brake_fluid/washer), color y standard (si se lee).\n"
            "Devuelve SOLO JSON con: part_guess, synonyms (lista), system, condition, damage_signs (lista), evidence_type,\n+            recommended_checks (3–6 pasos), risk_level (normal/urgent/critical), confidence (0..1), ask_user (si <0.6), notes, brand, fluid_guess, color, standard."
        )
        def ask(prompt_text: str) -> dict:
            raw = None
            try:
                rsp = client.responses.create(
                    model=ocr_model,
                    input=[{"role":"user","content":[{"type":"input_text","text":prompt_text},{"type":"input_image","image_url":input_image_url}]}],
                    temperature=0.0,
                    response_format={"type":"json_object"},
                )
                try:
                    raw = rsp.output_text
                except Exception:
                    raw = None
            except Exception:
                raw = None
            if not raw:
                msg = client.chat.completions.create(
                    model=ocr_model,
                    messages=[{"role":"user","content":[{"type":"text","text":prompt_text},{"type":"image_url","image_url":{"url":input_image_url}}]}],
                    temperature=0.0,
                    response_format={"type":"json_object"},
                )
                raw = msg.choices[0].message.content
            import json as _json
            try:
                return _json.loads(raw)
            except Exception:
                return {"raw": raw}

        data = ask(base_prompt)
        if isinstance(data, dict):
            data.setdefault('evidence_type', '')
            data.setdefault('recommended_checks', [])
            data.setdefault('confidence', 0.0)
            data.setdefault('ask_user', '')
            data.setdefault('notes', '')
            data.setdefault('brand', '')
            data.setdefault('fluid_guess', '')
            data.setdefault('color', '')
            data.setdefault('standard', '')
        conf = 0.0
        try:
            conf = float(data.get('confidence') or 0.0)
        except Exception:
            conf = 0.0
        if conf < 0.6 or not data.get('part_guess'):
            guide = (
                "Segundo intento: decide entre estas familias si dudabas: "
                "catalizador/convertidor, sensor O2/lambda, depósito de anticongelante, radiador, manguera de refrigerante, rin/llanta, amortiguador, balata, tapa de diferencial/sello piñón/sello flecha, garrafa/etiqueta de refrigerante. "
                "Si ves garrafas/etiquetas, extrae brand/color/standard y fija fluid_guess; si ves housing de diferencial con aceite, fija system=powertrain y evidencia 'fuga_liquido'. "
                "Sé específico y llena 'recommended_checks' con pasos accionables. Devuelve SOLO JSON."
            )
            d2 = ask(guide)
            if isinstance(d2, dict):
                for k, v in d2.items():
                    if not data.get(k):
                        data[k] = v
                try:
                    conf2 = float(d2.get('confidence') or 0.0)
                    if conf2 > conf:
                        data['confidence'] = conf2
                except Exception:
                    pass
        # Si es etiqueta/garrafa y faltan brand/fluid/color, hacer un pase específico de etiqueta
        try:
            need_label = (not data.get('brand')) or (not data.get('fluid_guess')) or (not data.get('color'))
            maybe_label = 'garrafa' in (data.get('part_guess') or '').lower() or 'etiqueta' in (data.get('part_guess') or '').lower()
        except Exception:
            need_label = False; maybe_label = False
        if need_label or maybe_label:
            label_prompt = (
                "Lee la etiqueta del producto en la imagen. Devuelve SOLO JSON con: "
                "brand (marca), product (nombre), fluid_guess (oil/diff_oil/atf/coolant/brake_fluid/washer), "
                "color (si aplica), standard (norma/tipo como ASTM u OAT/IAT)."
            )
            label = ask(label_prompt)
            if isinstance(label, dict):
                for k in ['brand','product','fluid_guess','color','standard']:
                    if label.get(k) and not data.get(k):
                        data[k] = label.get(k)
        # OEM lookup (parts catalog)
        try:
            from .parts_lookup import search_parts_catalog as _spl  # type: ignore
        except Exception:
            try:
                from parts_lookup import search_parts_catalog as _spl  # type: ignore
            except Exception:
                _spl = None
        try:
            if _spl and (data.get('part_guess') or data.get('synonyms')):
                q = ' '.join([str(data.get('part_guess') or '')] + list(data.get('synonyms') or [])).strip()
                hits = (_spl(q, top_k=3) or []) if q else []
                if hits:
                    data['oem_hits'] = [{'part_name':h.get('part_name'), 'oem':h.get('oem'), 'page_label':h.get('page_label'), 'score':h.get('score')} for h in hits]
        except Exception:
            pass
        # Fallback de checks por heurística si faltan y hay claves obvias
        try:
            if not data.get('recommended_checks'):
                t = (str(data.get('part_guess') or '') + ' ' + ' '.join(data.get('synonyms') or []) + ' ' + str(data.get('notes') or '')).lower()
                checks = []
                if 'diferencial' in t or data.get('system') == 'powertrain' or (data.get('fluid_guess') or '') == 'diff_oil':
                    checks = [
                        'Limpia el housing y ubica el punto exacto de fuga',
                        'Revisa sello de piñón (yugo) y sellos de flecha (ambos lados)',
                        'Verifica tapa/cárter de diferencial: tornillos y junta',
                        'Revisa tapones de dren/fill y ventilación (respiradero)',
                        'Rellena con aceite de diferencial según manual y verifica nivel'
                    ]
                elif (data.get('fluid_guess') or '') == 'coolant' or 'refrigerante' in t or 'anticongelante' in t:
                    checks = [
                        'Confirma tipo (IAT/OAT) y no mezclar distintos',
                        'Usa mezcla 50/50 si es concentrado; purga el circuito',
                        'Verifica fugas en mangueras, abrazaderas y radiador',
                        'Arranque y observa activación de ventiladores y nivel en depósito'
                    ]
                if checks:
                    data['recommended_checks'] = checks
        except Exception:
            pass
        return data
    except Exception:
        return None
