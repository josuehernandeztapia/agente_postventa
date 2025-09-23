#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# Ensure app/ is available as top-level modules (storage, warranty, etc.)
if [[ ":${PYTHONPATH:-}:" != *":$DIR/app:"* ]]; then
  if [[ -n "${PYTHONPATH:-}" ]]; then
    export PYTHONPATH="$DIR/app:$PYTHONPATH"
  else
    export PYTHONPATH="$DIR/app"
  fi
fi

APP="main:app"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

UV_LOG="${UV_LOG:-$DIR/.uvicorn.log}"
NG_LOG="${NG_LOG:-$DIR/.ngrok.log}"
UV_PID_FILE="$DIR/.uvicorn.pid"
NG_PID_FILE="$DIR/.ngrok.pid"

die() { echo "[run.sh] $*" >&2; exit 1; }

start_uvicorn() {
  if [[ -f "$UV_PID_FILE" ]] && kill -0 "$(cat "$UV_PID_FILE")" 2>/dev/null; then
    echo "[run.sh] uvicorn ya está corriendo (PID $(cat "$UV_PID_FILE"))."
    return 0
  fi
  local uv_cmd
  # Ejecutar sin reloader por defecto (no pasar --reload)
  local noreload_args=(--host "$HOST" --port "$PORT")
  if command -v uvicorn >/dev/null 2>&1; then
    uv_cmd=(uvicorn "$APP" "${noreload_args[@]}")
  else
    uv_cmd=(python3 -m uvicorn "$APP" "${noreload_args[@]}")
  fi
  echo "[run.sh] Iniciando uvicorn en http://$HOST:$PORT ..."
  nohup "${uv_cmd[@]}" >"$UV_LOG" 2>&1 & echo $! > "$UV_PID_FILE"
}

start_ngrok() {
  if ! command -v ngrok >/dev/null 2>&1; then
    echo "[run.sh] ngrok no está instalado. Instálalo (brew install ngrok) y ejecuta: ngrok config add-authtoken <TOKEN>" >&2
    return 1
  fi
  if [[ -f "$NG_PID_FILE" ]] && kill -0 "$(cat "$NG_PID_FILE")" 2>/dev/null; then
    echo "[run.sh] ngrok ya está corriendo (PID $(cat "$NG_PID_FILE"))."
  else
    # Enforce IPv4 loopback to evitar que ngrok resuelva 'localhost' a ::1
    local target="127.0.0.1:$PORT"
    if [[ -n "${NGROK_DOMAIN:-}" ]]; then
      echo "[run.sh] Iniciando ngrok http $target con dominio reservado '$NGROK_DOMAIN' ..."
      nohup ngrok http "$target" --domain "$NGROK_DOMAIN" --log=stdout >"$NG_LOG" 2>&1 & echo $! > "$NG_PID_FILE"
    else
      echo "[run.sh] Iniciando ngrok http $target ..."
      nohup ngrok http "$target" --log=stdout >"$NG_LOG" 2>&1 & echo $! > "$NG_PID_FILE"
    fi
  fi
  # Espera a la API local de ngrok y muestra la URL pública
  echo -n "[run.sh] Esperando URL pública de ngrok"
  for i in {1..30}; do
    if curl -fsS http://127.0.0.1:4040/api/tunnels >/dev/null 2>&1; then
      break
    fi
    echo -n "."; sleep 1
  done
  echo
  if curl -fsS http://127.0.0.1:4040/api/tunnels >/dev/null 2>&1; then
    python3 - "$PORT" << 'PY'
import json,sys,urllib.request
try:
    with urllib.request.urlopen('http://127.0.0.1:4040/api/tunnels') as r:
        data=json.load(r)
    urls=[t.get('public_url') for t in data.get('tunnels',[]) if t.get('public_url')]
    https=[u for u in urls if u.startswith('https://')]
    url=(https[0] if https else (urls[0] if urls else None))
    if url:
        print(f"[run.sh] ngrok URL pública: {url}")
        print(f"[run.sh] Endpoint /query: {url}/query")
    else:
        print('[run.sh] No se encontró URL pública de ngrok aún.')
except Exception as e:
    print('[run.sh] Error leyendo API de ngrok:', e)
PY
  else
    echo "[run.sh] La API de ngrok (localhost:4040) no respondió. Revisa $NG_LOG"
  fi
}

stop_all() {
  for f in "$UV_PID_FILE" "$NG_PID_FILE"; do
    if [[ -f "$f" ]]; then
      pid="$(cat "$f" 2>/dev/null || true)"
      if [[ -n "${pid}" ]] && kill -0 "$pid" 2>/dev/null; then
        echo "[run.sh] Deteniendo PID $pid (de $(basename "$f"))"
        kill "$pid" 2>/dev/null || true
      fi
      rm -f "$f"
    fi
  done
}

status() {
  for f in "$UV_PID_FILE" "$NG_PID_FILE"; do
    name=$(basename "$f")
    if [[ -f "$f" ]] && kill -0 "$(cat "$f")" 2>/dev/null; then
      echo "[run.sh] $name: vivo (PID $(cat "$f"))"
    else
      echo "[run.sh] $name: no corre"
    fi
  done
}

load_env() {
  # Carga variables de archivos .env sin ejecutar líneas inválidas
  safe_load() {
    local file="$1"
    [[ -f "$file" ]] || return 0
    while IFS= read -r line || [[ -n "$line" ]]; do
      # quitar CR
      line=${line%$'\r'}
      # ignorar comentarios y vacíos
      [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
      # eliminar prefijo 'export '
      if [[ "$line" == export\ * ]]; then line="${line#export }"; fi
      # aceptar solo KEY=VALUE válidos
      if [[ "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then
        # separar KEY y VALUE y eliminar comillas envolventes del VALUE
        local key="${line%%=*}"
        local val="${line#*=}"
        # quitar comillas simples o dobles al inicio/fin si existen
        [[ "$val" == '"'*'"' || "$val" == "'*'" ]] && val="${val:1:${#val}-2}"
        export "$key=$val"
      fi
    done < "$file"
  }
  # primero .env, luego secrets.local.txt (secrets tiene prioridad)
  safe_load ".env"
  # Preferir secrets.local.txt; compatibilidad con el nombre anterior secrets.loca.txt
  if [[ -f "secrets.local.txt" ]]; then
    safe_load "secrets.local.txt"
  elif [[ -f "secrets.loca.txt" ]]; then
    safe_load "secrets.loca.txt"
  fi
}

cmd="${1:-start}"
case "$cmd" in
  start)
    load_env
    start_uvicorn
    start_ngrok || true
    echo "[run.sh] Logs: uvicorn -> $UV_LOG | ngrok -> $NG_LOG"
    ;;
  stop)
    stop_all
    ;;
  status)
    status
    ;;
  restart)
    stop_all
    sleep 1
    load_env
    start_uvicorn
    start_ngrok || true
    ;;
  *)
    echo "Uso: $0 {start|stop|status|restart}"; exit 1 ;;
esac
