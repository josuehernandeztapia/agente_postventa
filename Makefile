.PHONY: help start stop status restart build-parts build-equivalences ingest query health version search parts-search smoke smoke-postdeploy media-worker install-agent uninstall-agent logs-tail logs-init logs-test-db export-csv export-xlsx test daily-report

ORIG_PYTHONPATH := $(value PYTHONPATH)
export PYTHONPATH := $(CURDIR)/app$(if $(strip $(ORIG_PYTHONPATH)),:$(strip $(ORIG_PYTHONPATH)))

help:
	@echo "Targets disponibles:"
	@echo "  start         - Inicia uvicorn + ngrok (usa .env)"
	@echo "  stop          - Detiene uvicorn + ngrok"
	@echo "  status        - Muestra estado de procesos"
	@echo "  restart       - Reinicia servicios"
	@echo "  build-parts   - Genera parts_index.json (usa PARTS_PAGES)"
	@echo "  ingest        - Reingesta a Pinecone (usa .env)"
	@echo "  build-equivalences - Regenera data/parts_equivalences.* y migración SQL"
	@echo "  query Q=...   - Ejecuta consulta local (CLI híbrido)"
	@echo "  health        - GET /health"
	@echo "  version       - GET /version"
	@echo "  search Q=...  - GET /search?q=..."
	@echo "  smoke [SMOKE_BASE=...] - Ejecuta smoke test local"
	@echo "  smoke-postdeploy [SMOKE_BASE=...] - Verifica /health, /version y /metrics"
	@echo "  media-worker       - Procesa la cola de medios (OCR/ASR en background)"
	@echo "  parts-search Q=... - GET /parts/search?name=..."
	@echo "  install-agent - Instala y carga LaunchAgent (autostart)"
	@echo "  uninstall-agent - Descarga y elimina LaunchAgent"
	@echo "  logs-init     - Crea carpeta y archivo de logs"
	@echo "  logs-tail     - Muestra logs en vivo (events.jsonl)"
	@echo "  logs-test-db  - Inserta un evento de prueba en Postgres/Neon"
	@echo "  export-csv    - Exporta events.csv y sources.csv (Neon o JSONL)"
	@echo "  export-xlsx   - Exporta a logs/events_and_sources.xlsx"
	@echo "  daily-report [OUT=...] - Genera reporte diario de casos"
	@echo "  test          - Ejecuta unittest sobre tests/"
	@echo "  admin-status  - Muestra estado del índice Pinecone (admin)"
	@echo "  rotate-index  - Crea índice nuevo y reingesta (admin)"
	@echo "  build-cases   - Construye índice Pinecone con casos (chat+visión)"
	@echo "  export-ft-style    - Genera datasets/ft_style.jsonl (fine-tune estilo)"
	@echo "  export-ft-classify - Genera datasets/ft_classify.jsonl (clasificador)"
	@echo "  eval-ft-style - Evalúa dataset curado contra un modelo (o FT)"
	@echo "  switch-llm MODEL=ft:... - Cambia LLM_MODEL en .env y reinicia"
	@echo "  trends-report - Muestra top tendencias (categorías, evidencias, keywords)"
	@echo "  forms-enrich  - Extrae VIN/placa/km/fecha/tipo y enriquece (falla/piezas) desde chat"
	@echo "  forms-master  - Consolida maestros (mínimos + enriquecidos)"
	@echo "  forms-import  - Importa maestro a casos locales (storage)"
	@echo "  analysis-bundle - Empaqueta CSVs + README en exports/analysis_bundle.zip"
	@echo "  export-geotab  - Exporta tabla (DSN) a CSV con filtro por fecha"

start:
	bash ./run.sh start

stop:
	bash ./run.sh stop

status:
	bash ./run.sh status

restart:
	bash ./run.sh restart

build-parts:
	python3 build_parts_catalog.py

build-equivalences:
	python3 scripts/build_parts_equivalences.py

ingest:
	python3 scripts/ingest.py

ingest-diagrams:
	python3 ingesta_diagramas.py

query:
	@[ -n "$(Q)" ] || (echo "Uso: make query Q='tu pregunta'" && exit 1)
	python3 query_mejorado.py "$(Q)"

build-cases:
	python3 prep_cases_index.py

export-ft-style:
	@mkdir -p datasets
	python3 prep_ft_style.py datasets/ft_style.jsonl

export-ft-classify:
	@mkdir -p datasets
	python3 prep_ft_classify.py datasets/ft_classify.jsonl

eval-ft-style:
	python3 eval_ft_style.py rag-pinecone/datasets/ft_style_curated.jsonl --model "$${MODEL:-gpt-4o}" --out rag-pinecone/logs/ft_eval.csv

switch-llm:
	@[ -n "$(MODEL)" ] || (echo "Uso: make switch-llm MODEL=ft:your-id" && exit 1)
	python3 switch_llm.py "$(MODEL)"

trends-report:
	python3 trends_dump.py

forms-enrich:
	@[ -f stems.txt ] || (echo "Coloca stems en rag-pinecone/stems.txt (una por línea)" && exit 1)
	python3 batch_forms_samples.py stems.txt
	python3 enrich_forms_from_chat.py
	@echo "--- forms_samples_enriched.csv ---" && sed -n '1,40p' logs/forms_samples_enriched.csv

forms-master:
	python3 consolidate_forms_master.py
	@echo "--- forms_samples_master.csv ---" && sed -n '1,40p' logs/forms_samples_master.csv

forms-import:
	python3 import_forms_to_cases.py

analysis-bundle:
	python3 export_analysis_bundle.py

SMOKE_BASE ?= http://127.0.0.1:8000

smoke:
	python3 scripts/smoke_test.py --base $(SMOKE_BASE)

smoke-postdeploy:
	python3 scripts/smoke_test.py --base $(SMOKE_BASE) --skip-query

media-worker:
	python3 scripts/process_media_queue.py --loop --verbose

health:
	curl -s http://127.0.0.1:8000/health || true

version:
	curl -s http://127.0.0.1:8000/version || true

search:
	@[ -n "$(Q)" ] || (echo "Uso: make search Q='tu consulta'" && exit 1)
	curl -s "http://127.0.0.1:8000/search?q=$(Q)" || true

parts-search:
	@[ -n "$(Q)" ] || (echo "Uso: make parts-search Q='nombre de pieza'" && exit 1)
	curl -s "http://127.0.0.1:8000/parts/search?name=$(Q)" || true

logs-init:
	@mkdir -p logs && touch logs/events.jsonl && echo "OK logs/"

logs-tail: logs-init
	@echo "Mostrando logs en vivo (Ctrl+C para salir)"
	@tail -f logs/events.jsonl

logs-test-db:
	@python3 -c "import storage; storage.log_event('test', {'endpoint':'/test','question':'ping','answer':'pong','classification':'test','signals':{'ok':True}}); print('OK: evento escrito (si POSTGRES_URL está configurado).')"

export-csv:
	@python3 -c "import storage, json; r=storage.export_csv(); print(json.dumps(r, ensure_ascii=False))" && \
	echo "CSV exportado en logs/events.csv y logs/sources.csv"

export-xlsx:
	@python3 -c "import storage, json; r=storage.export_xlsx(); print(json.dumps(r, ensure_ascii=False))" && \
	echo "XLSX exportado en logs/events_and_sources.xlsx"

test:
	python3 -m unittest discover -s tests

daily-report:
	@if [ -n "$(OUT)" ]; then \
		python3 scripts/daily_report.py --out "$(OUT)" ; \
	else \
		python3 scripts/daily_report.py ; \
	fi

# ============ Neon exports ============
# Requiere: export DATABASE_URL='postgresql://...'
# Uso: make export-geotab TABLE=log_records DATE_COL=created_at SINCE=2025-08-01 OUT=exports/log_records.csv
export-geotab:
	@mkdir -p exports
	@[ -n "$(TABLE)" ] || (echo "Falta TABLE=... (ej: log_records)" && exit 1)
	@[ -n "$(DATE_COL)" ] || (echo "Falta DATE_COL=... (ej: created_at o ts)" && exit 1)
	@[ -n "$(SINCE)" ] || (echo "Falta SINCE=YYYY-MM-DD (ej: 2025-08-01)" && exit 1)
	python3 dump_neon_table.py --table "$(TABLE)" --date-col "$(DATE_COL)" --since "$(SINCE)" --out "$(OUT)"

admin-status:
	@curl -s "http://127.0.0.1:8000/admin/index/status?token=$${ADMIN_TOKEN}"

rotate-index:
	@[ -n "$(ADMIN_TOKEN)" ] || (echo "Falta ADMIN_TOKEN en el entorno" && exit 1)
	@echo "Rotando índice (dry_run=$(DRY) diagrams=$(DIAG) update_env=$(ENV) alias=$(ALIAS))..."
	@curl -s -X POST "http://127.0.0.1:8000/admin/index/rotate?include_diagrams=$${DIAG:-false}&update_env=$${ENV:-false}&use_alias=$${ALIAS:-false}&dry_run=$${DRY:-true}&confirm=ROTATE&token=$(ADMIN_TOKEN)"

install-agent:
	@mkdir -p $$HOME/Library/LaunchAgents
	@cp ./launchd/com.higer.rag.plist $$HOME/Library/LaunchAgents/
	@launchctl unload -w $$HOME/Library/LaunchAgents/com.higer.rag.plist 2>/dev/null || true
	@launchctl load -w $$HOME/Library/LaunchAgents/com.higer.rag.plist
	@echo "LaunchAgent instalado y cargado: com.higer.rag"

uninstall-agent:
	@launchctl unload -w $$HOME/Library/LaunchAgents/com.higer.rag.plist 2>/dev/null || true
	@rm -f $$HOME/Library/LaunchAgents/com.higer.rag.plist
	@echo "LaunchAgent desinstalado: com.higer.rag"
