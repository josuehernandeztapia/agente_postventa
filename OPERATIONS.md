# Operación y Mantenimiento — Higer RAG API

## 1. Visión General

Asistente RAG con API FastAPI, recuperación híbrida (BM25 + Pinecone) y “modo transporte”: tono cercano, pasos accionables, memoria de conversación y gestión de casos (case_id) con playbooks por categoría.

### Layout del código

- `app/api.py`: aplicación FastAPI (expuesta como `main:app`) + webhook y endpoints admin.
- `app/vision_openai.py`, `app/audio_transcribe.py`, `app/storage.py`, `app/warranty.py`: manejo de medios, almacenamiento y garantías.
- `app/cli/__init__.py`: entrypoints reutilizables (p.ej. `run_ingesta`) empleados por los wrappers en la raíz.
- `scripts/`: utilerías operativas (`ingest.py`, `process_media_queue.py`, `daily_report.py`, …) que importan desde `app`.
- `tests/`: pruebas unitarias clave (Twilio, media, storage, ingesta).
- `Makefile` y `run.sh`: tooling que fija `PYTHONPATH=app` para que los comandos funcionen desde la raíz.

> Al versionar o publicar el bot en otro repositorio, copia las carpetas `app/`, `scripts/`, `tests/`, la documentación necesaria y el tooling. Mantén fuera de git archivos sensibles (`.env`, `secrets.local.txt`), logs y datasets privados.

## 2. Arranque y Salud

- Iniciar: `make start` (o `make restart`)
- Salud local: `make health` → `http://127.0.0.1:8000/health`
- Salud público (ngrok): `https://<tu-dominio>.ngrok.app/health`

## 3. Variables de Entorno (resumen útil)

- Claves: `OPENAI_API_KEY`, `PINECONE_API_KEY`, `PINECONE_ENV`, `PINECONE_INDEX`.
- Longitudes: `ANSWER_MAX_CHARS`, `WHATSAPP_MAX_CHARS`, `CRITICAL_MAX_CHARS`, `URGENT_MAX_CHARS`.
- Conversación: `HISTORY_LIMIT`.
- Casos/Playbooks: `CASE_PREFIX`, `CASE_TTL_HOURS`, `PLAYBOOKS_FILE`.
- Híbrido: `HYBRID_TOP_K`, `HYBRID_ALPHA`, `HYBRID_DEDUPE_THRESHOLD`.
- Filtro Pinecone: `FORCE_MODEL_FILTER`.
- Medios: `MEDIA_PROCESSING` (`inline`/`queue`), `MEDIA_MAX_ITEMS`, `MEDIA_MAX_BYTES`, `MEDIA_CACHE_MAX`, `OCR_MODEL`, `ASR_MODEL`.
- Catálogo: `CATALOG_TOP_K` (máx. ítems insertados ante consultas OEM).
- Logging Neon: `STORAGE_BACKEND=dual|postgres`, `POSTGRES_URL`.
- Admin: `MAINTENANCE_ENABLE`, `ADMIN_TOKEN`, `ALLOW_NONLOCAL_ADMIN`.
- ngrok: `NGROK_DOMAIN` (dominio reservado).

## 4. Endpoints Clave

- `POST /query`: RAG simple con fuentes y resumen inteligente.
- `POST /query_hybrid`: RAG híbrido, memoria por contacto y caso + playbook.
  - Body típico (Make): `{"question":"{{Body}}","meta":{"contact":"{{From}}","channel":"whatsapp","max_chars":1450}}`
- `POST /twilio/whatsapp`: Webhook, soporta medios y gestiona casos automáticamente.
- `GET /parts/search?name=...`: catálogo local.
- `GET /search?q=...`: híbrido simple (catálogo + vectorial).
- `GET /health`, `GET /version`.

## 5. Casos (case_id), Playbooks y Evidencias

- Estado local:
  - `logs/cases_state.json`: estado al día por contacto
  - `logs/cases.jsonl`: historial de cambios (create/update)
- Flujo por contacto:
  - Se crea case_id al primer mensaje (formato `CASE_PREFIX-YYYYMMDD-XXXX`).
  - Se agregan `severity` (worst-of) y `categories` acumuladas.
  - Se aplican playbooks por categoría (archivo `playbooks.json`).
- Evidencia mínima (ask_for): `foto_vin`, `foto_odometro`, `foto_tarjeta_circulacion`, `foto_placa_unidad`, `foto_problema` (y específicas por categoría como `foto_testigo_aceite`, `foto_varilla_aceite`, `fotos_fugas_frenos`, `foto_tablero_temp`, `video_falla`).
- Adjuntos (WhatsApp): el webhook registra `NumMedia`/`MediaUrl*` en el caso (campo `attachments`) y, si hay Postgres, los guarda con hash (`case_attachments.url_hash`) para evitar duplicados.
  - OCR/voz por adjunto: se extraen `vin`, `plate`, `odo_km`, `evidence_type`, `notes`.
- delivered_at (fecha de entrega):
  - No se solicita factura. El agente intenta resolverla automáticamente por dos vías:
    1) OCR si viene explícita en la imagen (tarjeta/circulación).
    2) Búsqueda interna por VIN (`warranty.resolve_delivered_at_by_vin`).
  - Alternativamente, el usuario puede escribir la fecha (AAAA-MM-DD o DD/MM/AAAA) y se registra.
- Auditoría rápida: `GET /admin/cases/<contact>?token=...` devuelve severidad, categorías, evidencias entregadas/pending y adjuntos en Postgres.

## 6. Pipeline de medios

1. Recepción (WhatsApp/JSON): se normalizan URLs y content-type; `MEDIA_MAX_ITEMS` limita procesamiento inline.
2. OCR (`vision_openai.ocr_image_openai`): detecta VIN/placa/odómetro, fecha y tipo de evidencia.
3. Clasificación de partes (`vision_openai.classify_part_image`): genera pieza probable, checks sugeridos y top OEM.
4. Audio (`audio_transcribe.transcribe_audio_from_url`): transcribe y extrae señales para severidad/categoría.
5. Persistencia: `storage.attach_media` actualiza el caso local y `db_cases.add_attachment` guarda en Postgres con dedupe por hash.
6. Prompt final: prioriza evidencia detectada y añade bloque “Pendiente (evidencia mínima)” con lo faltante.

### Procesamiento diferido (`MEDIA_PROCESSING=queue`)

- Si defines `MEDIA_PROCESSING=queue`, el webhook solo adjunta la evidencia y no invoca OCR/ASR/visión en línea.
- Captura: revisa `logs/cases_state.json` o Postgres (`case_attachments`) para ver qué falta procesar.
- Worker incluido: `make media-worker` (internamente `python3 scripts/process_media_queue.py --loop --verbose`). Procesa `logs/media_queue.jsonl`, actualiza evidencias (`storage.mark_provided`) y registra eventos `media_processed`. Usa `--dry-run` si quieres simular.
- Ideal cuando quieras mover el trabajo pesado a segundo plano o balancear cuotas de OpenAI.


## 7. Severidad y Categorías (reglas heurísticas)

- Severidad: `critical` (p.ej., frenos al fondo, luz de aceite, sobrecalentamiento, humo), `urgent` (no enciende, jaloneos), `normal`.
- Categorías: `brakes`, `oil`, `cooling`, `electrical`, `fuel`, `transmission`, `chassis`, `tires`, `general`.

## 8. Resumen Inteligente y Límites

- Estructura: Resumen breve + Puntos clave (3–6) + Siguiente paso (+ páginas si caben).
- Límite efectivo: combina `ANSWER_MAX_CHARS`, canal y severidad (`CRITICAL_MAX_CHARS`, `URGENT_MAX_CHARS`).
- Evita cortes duros y conserva claridad para WhatsApp.

## 9. Integración Make + Twilio (memoria por contacto)

- Módulo HTTP:
  - URL: `https://<tu-dominio>.ngrok.app/query_hybrid`
  - Method: `POST` — Headers: `Content-Type: application/json`
  - Body: `{"question":"{{1.Body}}","meta":{"contact":"{{1.From}}","channel":"whatsapp","max_chars":1450}}`
  - Parse response: Yes
- Módulo Twilio (Send a message):
  - From: tu número
  - To: `{{1.From}}`
  - Body: `{{2.answer}}`

## 10. Logging y Exportación

- Eventos (local): `logs/events.jsonl`.
- Eventos (Neon opcional): tablas `events` y `sources`.
- Exportadores: `make export-csv` (CSV en `logs/`) y `make export-xlsx` (Excel).

## 11. Ingesta y Catálogo

- CLI unificado: `python3 scripts/ingest.py [--ocr] [--bm25-only] [--incremental] [--recreate] [--pages ...]` (agrega `build_ts`/`ingest_version` a metadatos y permite reingestas parciales por página).
- Wrapper Make: `make ingest` llama al CLI con `--ocr`.
- Catálogo de partes: `make build-parts` (usa `PARTS_PAGES` para acotar páginas).

## 12. ngrok y Auto‑arranque

- Dominio fijo: en `.env` set `NGROK_DOMAIN=higer-rag.ngrok.app` y `make restart`.
- LaunchAgent macOS: `make install-agent` / `make uninstall-agent`.

## 13. Troubleshooting rápido

- WhatsApp Body > 1600 chars: la API resume automáticamente; si usas Make, no apliques substring hoy.
- ngrok cambia de URL: usa dominio reservado y setéalo en `.env`.
- Uvicorn “Address already in use”: espera y reintenta `make restart` (reloader puede duplicar si hay cambio continuo).
- Sin memoria de conversación: revisa que envías `meta.contact` en HTTP o usa el webhook de WhatsApp.
- Neon no exporta: valida `POSTGRES_URL` y `STORAGE_BACKEND=dual|postgres`.
- OCR/ASR fallan siempre: revisa `OCR_MODEL`, `ASR_MODEL`, latencias en `logs/events.jsonl` y el endpoint `/metrics` (busca tasas anómalas en `ocr_success`/`ocr_timeout` y `asr_success`/`asr_timeout`, además de alertas `asr_short_transcript`).
- Warranty sin datos: confirma que el caso tenga VIN/fecha/odómetro (`/admin/cases/<contact>`) y que la tabla Neon `cases` esté poblada.
- Adjuntos duplicados: verifica que `case_attachments` tenga `url_hash`; reingresa con la versión que deduplica (marcarás `duplicate: true` en `/v1/cases/.../attachments/confirm`).
- Adjuntos rechazados: tipos fuera de `image/jpeg|png|webp` o `audio/ogg|mpeg|mp3|wav|mp4|m4a`, o archivos que excedan `MEDIA_MAX_BYTES`, quedan como `media_skipped` en eventos.
- Estado de casos: usa `GET /admin/cases?token=...` para ver pendientes/missing por contacto; `GET /admin/cases/{contact}?token=...` detalla timestamps de evidencias.

## 14. Mantenimiento de Índices (Admin)

- Seguridad: requiere `MAINTENANCE_ENABLE=1` y `ADMIN_TOKEN` en el entorno. Por defecto, solo acepta desde localhost.
- Status: `GET /admin/index/status` — dimensión esperada vs real, siblings y stats.
- Rotación: `POST /admin/index/rotate`
  - Parámetros: `dry_run` (bool), `include_diagrams` (bool), `update_env` (bool), `confirm=ROTATE`.
  - Flujo: crea índice nuevo `PINECONE_INDEX-<timestamp>-<dim>`, lanza ingesta(s) en background, opcionalmente actualiza `.env`. Reinicia con `make restart` cuando termine la ingesta.
  - Nota: si usas alias nativos de Pinecone, puedes gestionarlos manualmente; este endpoint hace blue/green por nombre.

## 15. Notas recientes

- `/health` ahora reporta: initialized, modelos (LLM/OCR/ASR), embeddings/índice (dim esperada y real), presencia de BM25 y estado del índice de diagramas.
- Validación de firma de Twilio: se aplica por defecto con `PUBLIC_BASE_URL` + `TWILIO_AUTH_TOKEN`; desactívala solo en pruebas poniendo `TWILIO_VALIDATE=0`.
- CORS configurable vía `CORS_ORIGINS` (coma-separado) o `*` para permitir todos los orígenes.

## 16. Post‑deploy / smoke

1. Ejecuta `./run-smoke-and-dump.sh` (usa `make smoke` y consulta `/health`, `/version`, `/metrics`).
   - `SMOKE_BASE_URL` controla la URL (ej. `export SMOKE_BASE_URL=https://tu-dominio.ngrok.app`).
   - Si `/metrics` falla, verifica `METRICS_ENABLE` o revisa el pod correspondiente.
2. Revisa `logs/events.jsonl` / Postgres para ver si hay `kind=whatsapp_out` con errores.
3. Si la ingesta cambió, valida `GET /admin/index/status` para confirmar la dimensión esperada y el conteo de vectores.
