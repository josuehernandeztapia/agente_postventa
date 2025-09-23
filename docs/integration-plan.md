# Integración Odoo / Neon — Plan Inicial

## Objetivo
Conectar la API FastAPI/Neon (casos, refacciones, garantías) con sistemas de backoffice (Odoo para compras/cotizaciones y Neon para tickets/fallas) para cerrar el ciclo postventa: diagnóstico → garantía → cotización → orden → seguimiento.

## Componentes Existentes
- **Neon (Postgres)**
  - Tabla `cases`: creada y utilizada por el webhook (id, contact_id, vin, plate, odo_km, falla_tipo, warranty_status, etc.).
  - Tabla `case_attachments`: guarda adjuntos con dedupe por hash.
- **FastAPI (`main.py`)**
  - Endpoints `/query_hybrid`, `/twilio/whatsapp`, `/admin/cases`, `/admin/cases/{contact}`.
  - `storage.py` maneja estado local (required/provided, timestamps, queue de medios).
- **Pinecone/BM25**
  - Índices para manual técnico (`PINECONE_INDEX`) e ingest incremental (`scripts/ingest.py`).

## Integraciones Planificadas

### 1. Garantías ↔ Neon/Odoo
- **Evento:** `warranty.policy_evaluate` retorna `eligible`, `review` o `no_eligible`.
- **Acciones pendientes:**
  1. Si `eligible`: crear ticket interno (Neon/Odoo) con los campos de la tabla `fault_catalog` y adjuntos.
  2. Si `no_eligible`: lanzar flujo `spareparts` (cuando exista) y sugerir compra; además, registrar seguimiento en Odoo (cotización).
  3. Loggear decisión + proveedor recomendado para analytics.

### 2. Refacciones & cotizaciones
- **Endpoint futuro `/spareparts`** (ver backlog Fase 2):
  - Datos: número de parte, equivalencia Toyota, proveedores nacionales, URL/contacto, stock y precio.
  - Integración con Odoo: crear cotización con líneas (refacción + proveedores sugeridos) y estado.
- **Eventos a modelar:** `quote.created`, `quote.approved` (para cerrar loop en chatbot y PWA).

### 3. Flujo de medios en cola
- Con `MEDIA_PROCESSING=queue` y `make media-worker`, el estado local se mantiene actualizado; se requiere:
  - **Webhook** → guarda evidencias y escribe en `media_queue.jsonl`.
  - **Worker** → procesa, actualiza `case.attachments`, marca `provided` y logea `media_processed`.
  - **Pendiente:** si Neon está activo, sincronizar `case_attachments` con timestamps `first_seen_at`/`last_seen_at` (crear migración si falta).

### 4. Eventos hacia Make/n8n
- Estandarizar payloads (JSON) para:
  - `vehicle.delivered` (VIN, cliente, odómetro, paquete) → dispara encuestas y apertura de expediente.
  - `warranty.opened` → crea ticket y agenda seguimiento.
  - `quote.requested` → consulta inventario y genera cotización Odoo.
  - `service.due/service.done` → recordatorios preventivos.
- Acción: definir plantilla webhook o tabla en Neon para registrar eventos; Make/n8n consume y distribuye (plantillas WhatsApp, tareas, etc.).

## Roadmap Técnico
1. **Fase 2 (en curso)**
   - Construir `/spareparts` y lógica de sugerencia de compra (webhook + PWA).
   - Activar worker de medios en ambientes productivos (`make media-worker`).
   - Crear tabla `fault_catalog` en Neon (archivo SQL adjunto en `PMA 2.0`).
2. **Fase 3**
   - Integración con Odoo
     - Plantilla de cotización auto-generada (líneas + mano de obra).
     - Orden de compra a proveedores según SLA.
   - Sincronizar tickets de garantía con Neon (estado, costos, partes).  
3. **Fase 4+**
   - Agentes específicos (Mantenimiento, CSAT, Revenue) usando la misma base de datos y eventos.
   - Métricas: attach rate, tiempo de resolución, stock rotación, costo garantía.

## TODO Inmediato
- [ ] Exponer `/spareparts` y definir esquema JSON de respuesta con `garantia`, `proveedores`, `links`, `stock`.
- [ ] En webhook WhatsApp, agregar flujo “garantía no aplica” → sugerencia proveedores.
- [ ] Ejecutar worker en entorno staging (`make media-worker`) y monitorear eventos `media_processed`.
- [ ] Materializar `fault_catalog` en Neon usando script SQL (ver PMA 2.0) y conectar `warranty.policy_evaluate` a esa tabla.
- [ ] Diseñar endpoint de eventos (`/admin/events` o tabla Neon) para Make/n8n.

Documentar cada integración y mantener este archivo actualizado conforme se implementen los pasos.
