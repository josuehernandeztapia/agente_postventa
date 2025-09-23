# Guía Interna del Agente Higer RAG

Este documento explica el flujo completo del asistente: prompting, recuperación híbrida, manejo de medios, ingesta y gestión de casos. Complementa al `README.md` (visión general) y a `OPERATIONS.md` (runbook operativo).

## 1. Prompting y estilo de respuesta

- **`main.py:169` (`PROMPT`)** — Prompt base para `POST /query`. Expone la identidad (“técnico de postventa Higer”), exige tono cercano, formato con resumen/pasos/fuente/cierre y reglas de seguridad (alertas ante síntomas críticos, evitar pedir modelo si no cambia el procedimiento).
- **`main.py:936` (`system_prompt_hybrid()`)** — Prompt para `/query_hybrid` y webhooks WhatsApp. Añade instrucciones conversacionales: tuteo, agradecimiento por evidencia, confirmaciones (“¿correcto?”), límite de preguntas, manejo de modos (`solo_imagenes`, `solo_audio`).
- **Límites y resúmenes** — `_effective_limit` ( `main.py:212` ) ajusta longitud según canal y severidad. `_summarize_to_limit` ( `main.py:233` ) puede llamar al LLM para condensar la respuesta manteniendo estructura.
- **Rewriting** — `rewrite_query` ( `main.py:972` ) refuerza la consulta con “Manual técnico Higer”, OEM y términos relevantes antes de embedir.

## 2. Recuperación híbrida

1. **Embeddings Pinecone** — `startup_event` ( `main.py:502` ) levanta `RetrievalQA` para `/query`.
2. **Pipeline híbrido** — `/query_hybrid` ( `main.py:656` en adelante) combina:
   - Pinecone manual técnico (`PINECONE_INDEX`).
   - BM25 (`bm25_index_unstructured.pkl`).
   - Índice de casos (`PINECONE_INDEX_CASES`).
   - Catálogo de partes y tablas (`source='table'`).
3. **Fusión** — `hybrid_merge` ( `main.py:820` ) normaliza scores, aplica boosts por tablas, casos y tendencias (`trends.trend_boost`). `lexical_rerank` termina con orden léxico.
4. **Contexto** — `build_context` arma bloques prefijados con prev/next window para el prompt; se añade historial (`storage.get_conversation`) y resumen del caso (severidad, categorías).
5. **Fuentes** — Se ordenan por número de página (arábigo → romano → alfanumérico) y se devuelven en la respuesta API.

## 3. Manejo de medios (WhatsApp / JSON)

- **Normalización** — `_guess_content_type` ( `main.py:360` ) deduce tipo de archivo via extensión, HEAD sin/sobre credenciales Twilio.
- **Webhook `/twilio/whatsapp`** ( `main.py:2360` ): procesa form-urlencoded, valida firma con HMAC (puedes desactivar con `TWILIO_VALIDATE=0`), aplica dedupe por `MessageSid` y ejecuta el pipeline híbrido completo.
- **Adjuntos** — por cada `MediaUrl`:
  - Audio → `audio_transcribe.transcribe_audio_from_url` (Whisper). El texto se agrega a `transcripts`, se recalcula severidad/categoría.
  - Imagen → `vision_openai.ocr_image_openai` + `classify_part_image`. Extrae VIN, placa, odómetro, `evidence_type`, `recommended_checks`, `oem_hits`, marcas de insumo, etc.
  - Solo se procesan imágenes `image/jpeg|png|webp` y audio `audio/ogg|mpeg|mp3|wav|mp4|m4a`; HEIC/PDF u otros se descartan como `media_skipped`.
  - Los adjuntos locales se deduplican por hash/URL y guardan `first_seen_at`/`last_seen_at`; se registran en `storage.log_event` (`ocr_detected`, `ocr_success`, `ocr_timeout`, `vision_classified`, `asr_detected`, `asr_success`, `asr_timeout`, `asr_short_transcript`) y, si existe caso Neon, se guardan vía `db_cases.add_attachment`.
- **Modos** — `mode` se infiere (`solo_audio`, `solo_imagenes`, mixto). El prompt híbrido cambia instrucciones dependiendo del modo.
- **JSON endpoint** (`/twilio/whatsapp_json`) replica el pipeline pero recibe una carga JSON y responde con `{answer, case_id, pending, warranty}`.

## 4. Gestión de casos y playbooks

- `storage.py` almacena estado por contacto (`get_or_create_case`, `update_case`, `mark_provided`) con persistencia en `logs/cases_state.json` o Postgres/Neo4j.
- `load_playbooks()` difiere qué evidencia solicitar según categoría (`playbooks.json`).
- `extract_signals()` clasifica categoría, severidad, modelo, OEM y problema (fuga, desgaste, etc.). También detecta códigos de falla (`P/B/C/U + ####`), consulta `data/dtc_catalog.json` y ajusta categoría/severidad con esa referencia.
- `log_event()` soporta múltiples backends: JSONL, Neo4j, Postgres (`STORAGE_BACKEND`).
- Seguimiento Neon: `db_cases.py` expone `create_case`, `add_attachment`, `upsert_case_meta`. El webhook los usa para mantener casos sincronizados.
- Equivalencias de refacciones: `data/parts_equivalences.json` (generado con `scripts/build_parts_equivalences.py`) expone OEM → proveedores Toyota/aftermarket; la migración `migrations/*_part_equivalences.sql` permite sincronizarlo con Postgres.

## 5. Garantía

- `warranty.policy_evaluate` cruza meses/kilómetros con la categoría (`falla_tipo`).
- Requiere `delivered_at` + `odo_km`; si faltan, el bot pide confirmación o intenta resolver automáticamente vía `warranty.resolve_delivered_at_by_vin`.
- Las decisiones se anexan a la respuesta del webhook (bloque “Garantía: …”).

## 6. Ingesta e índices

| Índice | Script | Notas |
| --- | --- | --- |
| Manual técnico | `scripts/ingest.py` | CLI unificado (`--ocr`, `--bm25-only`, `--incremental`, `--recreate`, `--pages`), añade build_ts/ingest_version y BM25. |
| Diagramas | `ingesta_diagramas.py` | Filtra elementos "diagram-like"; usa `source='diagram'`. |
| Casos históricos | `prep_cases_index.py` | Funde CSV de chat + visión + evidencia aislada. |
| Catálogo de partes | `build_parts_catalog.py` | `pdfplumber` + heurísticas de tablas; produce JSON para `/parts/search`. |

`scripts/ingest.py` genera `bm25_index_unstructured.pkl` utilizado por `/query_hybrid` y el webhook. Los scripts `ingesta.py`, `ingesta_mejorada.py` e `ingesta_final.py` se mantienen como compatibilidad y delegan en el script unificado.

## 7. Observabilidad

- Middleware `/metrics` ( `main.py:1456` ) contabiliza requests y latencias por método/path, condicionados a `METRICS_ENABLE`.
- Eventos de entrada/salida: `storage.log_event` con `kind` `api_query`, `whatsapp_in`, `whatsapp_out`, `vision_classified`, etc.
- Exportadores (`storage.export_csv`) convierten logs a CSV usando Postgres si está disponible.

## 8. Puntos de mejora identificados

- Cacheo de OCR/ASR y clasificación de piezas por URL/MessageSid.
- Ajustar `_summarize_to_limit` para usar modelos más ligeros o enfoques extractivos.
- Consolidar scripts de ingesta y exponer CLI con banderas.
- Documentar playbooks y políticas de garantía en un anexo específico.

---

Última revisión: _(actualiza esta sección cuando modifiques el pipeline)_.
