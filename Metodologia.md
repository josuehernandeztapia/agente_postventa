# Metodología de Trabajo (SSOT + Human-in-the-Loop)

## 1. Preparación
1. Clonar repositorios `conductores` y `pwa_angular`.
2. Descargar datasets locales (telemetría NEON/Geotab, consumos GNV, docs postventa) en `~/Desktop/Accion/2025/conductores`.
3. Configurar `.env` (Pinecone, OpenAI, Neon, Odoo, WhatsApp, etc.).

## 2. Estructura documental
- `docs/ssot/index.html`: tablero operativo (estado, roadmap, enlaces).
- `docs/fichas/*.md`: fichas por dominio con TL;DR, prerequisitos, pasos, validación, bloqueadores.
- `docs/fichas/*-run.txt`: run-guides con comandos shell (EDA, entrenamiento, despliegue, tests).
- `sql/neon/pma/*.sql`: scripts para crear/poblar tablas en Neon.
- `scripts/odoo/*.py`: utilidades JSON-RPC para Odoo.
- `notebooks/*_eda.ipynb`: plantillas de análisis exploratorio.

## 3. Flujo estándar (por agente/proyecto)
1. **Revisar ficha** (`docs/fichas/<dominio>.md`).
2. **Ejecutar run guide** (`docs/fichas/<dominio>-run.txt`).
   - Incluye `jupyter nbconvert --execute notebooks/..._eda.ipynb`.
   - Entrena modelos (`scripts/hase/train_xgb.py`, `scripts/pma/train_model.py`, etc.).
3. **Aplicar scripts**
   - Odoo: `python scripts/odoo/import_catalogs.py`, `apply_protection.py`.
   - Neon: `psql $NEON_URL -f sql/neon/pma/*.sql`.
   - Make/n8n: construir escenarios y versionarlos (`make/scenarios/*.json`).
4. **Validar**
   - PWA tests (`npm run test -- --include ...`).
   - Scripts de verificación (SQL, drift monitor, telemetría).
5. **Registrar evidencia**
   - Guardar logs/capturas en `docs/evidence/<dominio>/`.
   - Actualizar estado en `docs/ssot/index.html` y Notion.

## 4. Orquestación de agentes (resumen)
- HASE (Evaluator) → produce score.
- PIA (Proactive Advisor) → usa score + telemetría para reestructuras.
- PMA (Monitor/Actor) → telemetría → mantenimiento/refacciones.
- AVI (Coordinator) → combina heurístico/científico/voz para Growth.
- Make/n8n → puente con WhatsApp, Mifiel, Metamap, Conekta.
- Odoo → sistema transaccional (contratos, refacciones, contabilidad).
- Neon → catálogo estructurado (fallas, equivalencias, stock).

## 5. Roles Human-in-the-loop
- Revisar notebooks EDA y ajustar parámetros.
- Validar mensajes y plantillas de comunicación.
- Aprobar/ajustar plantillas WhatsApp, workflows Make.
- Confirmar asientos contables y conciliaciones.

## 6. Repetibilidad
1. Mantener actualizados run-guides y fichas cuando cambien APIs/datasets.
2. Versionar escenarios Make y scripts SQL en el repo (`make/scenarios/`, `sql/`).
3. Documentar cada despliegue (fecha, responsable, resultado) en Notion + `docs/evidence/`.
4. Revisar `docs/ssot/README.md` para prompts base y recordar el flujo estándar.

## 7. Próximos pasos sugeridos
- Automatizar pipelines ETL y MLOps (Airflow/Prefect + MLflow).
- Completar módulos Odoo custom y dashboards.
- Convertir este método en plantilla (clonar repo, reemplazar datasets, ejecutar run-guides).

## 8. Higer RAG API — flujo de cambios y QA

1. **Planeación rápida**
   - Registrar alcance, riesgos y referencias (prompts, scripts de ingesta, índices) en ticket o Notion.
   - Revisar documentación vigente: `README.md`, `OPERATIONS.md`, `docs/agent-internals.md`.
2. **Branching / control de cambios**
   - Mantener `main` desplegable.
   - Trabajar en `feature/<tema>` o `fix/<tema>` con commits pequeños y descriptivos.
3. **Pull request checklist**
   - [ ] Pruebas locales relevantes ejecutadas.
   - [ ] Documentación actualizada (README/OPERATIONS/guía interna).
   - [ ] Revisar logs/telemetría si el cambio toca operaciones.
   - [ ] Solicitar revisión cruzada (alguien más valida prompts/ingesta/webhook).

### Validaciones previas a merge/despliegue

- **Smoke test**: `make smoke [SMOKE_BASE=...]` para verificar `/health` y respuesta corta de `/query_hybrid`.
- **Pruebas básicas**: `pytest` (si aplica) o llamadas manuales a `/health`, `/query_hybrid`, `/twilio/whatsapp_json`.
- **Ingesta**: ejecutar `make ingest` o planificar `/admin/index/rotate` si se modifican fuentes.
- **Lint/format**: `ruff check` y `black` cuando existan reglas.
- **Smoke manual**: enviar mensaje real vía Make/Twilio y confirmar fuentes citadas.

### Gestión de datos e índices

- `make ingest` tras cambios en PDF/manuales o prompts que alteren chunks.
- `make build-parts` cuando haya nuevas tablas/opciones OEM.
- `python3 prep_cases_index.py` para refrescar casos (logs/vision + chat).
- `/admin/index/rotate` para blue/green en Pinecone; documentar resultado en `OPERATIONS.md` o Notion.
- Mantener `data/dtc_catalog.json` actualizado cuando se incorporen nuevos códigos de falla del SSOT o de telemetría externa.
- `make build-equivalences` al agregar equivalencias Toyota/aftermarket (regenera JSON + migración SQL).

### Revisión de respuestas

- Checklist de prompts: tono “técnico Higer”, agradece evidencia, alerta en casos críticos, cita páginas cuando existan.
- Escenarios de regresión: frenos, aceite, refrigeración, OEM, garantía, evidencias mixtas (foto/audio).

### Operación continua

- Monitorear `/metrics` y `logs/events.jsonl` (o Neon) diariamente.
- Registrar incidentes en `logs/incidents/YYYY-MM-DD.md` y abrir follow-up.
- Compartir snapshot semanal (casos por severidad, evidencias completas, pendientes de garantía).
