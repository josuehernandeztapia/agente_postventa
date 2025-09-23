# Higer RAG API

Asistente de postventa que combina recuperación híbrida (Pinecone + BM25), prompting contextual y gestión de casos para responder consultas técnicas desde WhatsApp, Make o cualquier cliente HTTP.

## Documentación rápida

- **Operaciones** → [`OPERATIONS.md`](OPERATIONS.md) — runbook de despliegue, variables y endpoints admin.
- **Arquitectura interna** → [`docs/agent-internals.md`](docs/agent-internals.md) — prompts, flujo híbrido, casos/playbooks, garantía.
- **Metodología** → [`Metodologia.md`](Metodologia.md) — prácticas de trabajo (actualízala conforme evolucionen procesos).

## Visión general

1. **API FastAPI (`app/api.py`, expuesta como `main:app`)**: maneja `/query`, `/query_hybrid`, webhook de WhatsApp y endpoints admin.
2. **RAG híbrido**: embeddings `text-embedding-3-small` en Pinecone + BM25 (`bm25_index_unstructured.pkl`).
3. **Gestión de casos**: memoria por contacto, playbooks por categoría, almacenamiento en archivos locales + Postgres opcional.
4. **Pipeline de medios**: OCR, clasificación de partes y transcripción para enriquecer la conversación.

## Estructura del bot

- `app/api.py`: FastAPI + lógica del webhook, prompts híbridos, gestión de casos y endpoints admin.
- `app/vision_openai.py`, `app/audio_transcribe.py`, `app/storage.py`, `app/warranty.py`, etc.: módulos especializados para medios, persistencia y garantías.
- `app/cli/__init__.py`: entrypoints reutilizables (por ejemplo, `run_ingesta`) que orquestan `app.ingesta_unificada`.
- `scripts/`: utilidades operativas (`ingest.py`, `process_media_queue.py`, `daily_report.py`, …) ya apuntando al paquete `app`.
- `tests/`: cobertura para Twilio, medios, storage, ingesta.
- `Makefile`, `run.sh`: tooling que exporta `PYTHONPATH=app` para que los comandos funcionen desde la raíz.

> Para publicar el bot en otro repositorio (p.ej. `agente_postventa`), copia `app/`, `scripts/`, `tests/`, los archivos de tooling (`Makefile`, `run.sh`, `requirements.txt`) y la documentación necesaria. **No incluyas** `.env`, `secrets.local.txt`, logs ni datasets privados; apóyate en `.gitignore` para proteger credenciales.

```
Usuario → (/query_hybrid o webhook) → rewrites + retrieve (Pinecone + BM25 + casos) → LLM (prompt dinámico) → respuesta con fuentes + recordatorio de evidencias
```

## Prompts y tono

- Prompt principal: `system_prompt_hybrid()` en `app/api.py`. Ajusta tono por severidad/categoría y recuerda evidencias pendientes.
- Bloques incluidos en cada respuesta híbrida:
  - Resumen breve en tono cercano (tuteo y término “vagoneta”).
  - Pasos numerados respaldados por el contexto recuperado.
  - Cierre natural (`¿Seguimos?`, `¿Te late?`, etc.) salvo casos críticos.
- Para cambios de tono o plantillas específicas por categoría, ajusta el bloque correspondiente en `app/api.py` (sección prompts híbridos).

## Pipeline de medios (WhatsApp / Make)

1. **Recepción**: cada `MediaUrl*` se normaliza y, si `MEDIA_PROCESSING=inline`, se procesa en el mismo request.
2. **OCR (`app.vision_openai.ocr_image_openai`)**: extrae VIN, placas, odómetro, fecha de entrega, tipo de evidencia.
3. **Clasificación de partes (`app.vision_openai.classify_part_image`)**: sugiere parte, checks y OEM potenciales.
4. **Audio → texto (`app.audio_transcribe.transcribe_audio_from_url`)**: Whisper + heurísticas para severidad.
5. **Persistencia**: adjuntos y metadatos se guardan en el caso local (`logs/cases_state.json`) y en Postgres si `POSTGRES_URL` está configurado.
6. **Respuesta**: la evidencia detectada se antepone al prompt y los pendientes se listan al final para evitar repetir pedidos.

Si deseas mover el procesamiento a segundo plano, habilita `MEDIA_PROCESSING=queue` y ejecuta `make media-worker` (loop que procesa la cola en segundo plano).

## Ingesta y catálogo de partes

- Script único (`app/cli`): `python3 scripts/ingest.py [--ocr] [--bm25-only] [--incremental] [--recreate] [--pages 12,15-18]`
  - Genera chunks, tabla de ventanas, BM25 y upserts en Pinecone.
  - `--incremental` reutiliza el índice y elimina únicamente las páginas indicadas (cuando se usa `--pages`).
  - `--recreate` fuerza la recreación completa del índice aun en modo incremental.
- Compatibilidad: los archivos `ingesta.py`, `ingesta_mejorada.py` e `ingesta_final.py` siguen presentes para entornos legados, pero delegan al script unificado.
- Catálogo de partes (`build_parts_catalog.py`) produce `parts_index.json`, utilizado por `/parts/search`.

## Roadmap

- **Fase 1 – Consolidación (completada):** catálogo nacionalizado ingestable, pipeline de medios con validaciones, prompts y garantías refinadas, endpoints de administración y tooling (`smoke-postdeploy`, `daily-report`).
- **Fase 2 – Especialización (en construcción):**
  - Endpoint `/spareparts` con equivalencias/proveedores y lógica en el webhook para sugerir compra cuando la garantía no aplica.
  - Worker de cola (`MEDIA_PROCESSING=queue`) para OCR/ASR fuera del request.
  - Integración con backoffice (Neon/Odoo) para tickets y cotizaciones automáticas.
- Detalles de integraciones planificadas en `docs/integration-plan.md`.
- Las fases posteriores (agentes de mantenimiento, garantías avanzadas, CSAT, revenue) están delineadas en `PMA 2.0.txt` y en los agentes recomendados del roadmap interno.

## Puesta en marcha

1. **Instala dependencias**
   ```bash
   pip3 install -r requirements.txt
   ```
2. **Configura variables** en `.env` (ver `OPERATIONS.md` para lista completa). Puedes sobreescribir con `secrets.local.txt`.
3. **Ingesta inicial/incremental**
   ```bash
   # Ingesta completa con OCR
   python3 scripts/ingest.py --ocr --recreate

   # Ingesta incremental de páginas 420-430 (sin recrear)
   python3 scripts/ingest.py --incremental --pages 420-430
   ```
4. **Levanta la API**
   ```bash
   uvicorn main:app --reload
   ```
5. **Exposición pública (opcional)**
   ```bash
   ngrok http 8000
   ```

## Endpoints principales

| Endpoint | Descripción |
| --- | --- |
| `POST /query` | RAG básico (sin memoria ni casos). |
| `POST /query_hybrid` | Recuperación híbrida con memoria por contacto y playbooks. |
| `POST /twilio/whatsapp` | Webhook oficial. Maneja medios, casos y atajos de refacciones/garantía. |
| `GET /parts/search?name=...` | Catálogo local con BM25. |
| `GET /admin/cases/<contact>` | (Protegido) Estado del caso: evidencias entregadas, pendientes, adjuntos en Postgres. |
| `GET /admin/index/status` / `POST /admin/index/rotate` | Gestión de índices Pinecone (requiere `MAINTENANCE_ENABLE` + `ADMIN_TOKEN`). |
| `GET /health` / `GET /version` / `GET /metrics` | Diagnóstico rápido, build info y métricas estilo Prometheus. |

## Flujos comunes

- **Integración Make / WhatsApp**: envía `question` + `meta.contact` al endpoint `/query_hybrid` y reenvía `answer`. Revisa [`OPERATIONS.md`](OPERATIONS.md#9-integración-make--twilio-memoria-por-contacto) para payloads exactos.
- **Casos y evidencias**: usa `/admin/cases/<contact>` para auditar qué falta recabar antes de cerrar un ticket.
- **Smoke después de deploy**: ejecuta `./run-smoke-and-dump.sh` (ejecuta `make smoke` + `curl /health|/version|/metrics`).
- **Procesamiento de medios en background**: si defines `MEDIA_PROCESSING=queue`, monta un worker (`python3 scripts/process_media_queue.py --loop`) para procesar la cola (ver OPERATIONS §6).

## Desarrollo

- Código principal de prompts y recuperación: `app/api.py` (expuesta como `main:app` para compatibilidad).
- Playbooks: `playbooks.json` — define `ask_for`, `route_suggestion` y checklists por categoría.
- Tests pendientes sugeridos: `storage.extract_signals`, `_detect_topic_switch`, `hybrid_merge`, fixtures Twilio.

## Licencia / créditos

Uso interno para Higer. Ajusta, versiona y documenta cualquier cambio mayor en este README y en `OPERATIONS.md` para mantener la trazabilidad del sistema.
