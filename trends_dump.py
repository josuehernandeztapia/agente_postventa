import os
import json
from pathlib import Path
from dotenv import load_dotenv  # type: ignore

def load_env():
    base = Path(__file__).parent
    for p in [base/'.env', base/'secrets.local.txt']:
        if p.exists():
            try:
                load_dotenv(dotenv_path=p, override=True)
            except Exception:
                pass

def main():
    load_env()
    from trends import trends  # lazy import after env
    data = trends() or {}
    cats = sorted((data.get('categories') or {}).items(), key=lambda x: x[1], reverse=True)
    evs = sorted((data.get('evidence') or {}).items(), key=lambda x: x[1], reverse=True)
    kws = sorted((data.get('keywords') or {}).items(), key=lambda x: x[1], reverse=True)
    print('--- Tendencias (normalizadas 0..1) ---')
    def show(name, items):
        print(f'[{name}]')
        if not items:
            print('  (sin datos)')
            return
        for k,v in items[:10]:
            print(f'  {k:16s} {v:.2f}')
    show('categorías', cats)
    show('evidencias', evs)
    show('keywords', kws)
    # Dump JSON opcional
    if os.getenv('TRENDS_DUMP_JSON','0').strip().lower() in {'1','true','yes','on'}:
        print('\nJSON:')
        print(json.dumps(data, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()

