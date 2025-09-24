import re
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_DIR = ROOT / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

try:
    from app import storage  # type: ignore
except Exception:
    storage = None  # type: ignore

TS_RE = re.compile(r"^\[(\d{2})\/(\d{2})\/(\d{2}),\s*([0-9:]+)\s*([^\]]*)\]\s*(.+?):\s*(.*)$")
ATTACH_RE = re.compile(r"^\u200e?<adjunto:\s*(.+?)>\s*$")


def parse_chat_lines(text: str):
    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        # limpiar marcas de dirección/utf-8 BOM al inicio
        line = line.lstrip('\ufeff\u200e\u200f')
        if not line:
            continue
        m = TS_RE.match(line)
        if not m:
            # Puede ser continuación o sistema; emitimos como misc
            yield {
                'ts': '', 'author': '', 'text': line, 'attachment': '', 'kind': 'misc'
            }
            continue
        dd, mm, yy, hhmmss, ampm, author, body = m.groups()
        # Anexos
        att = ''
        b = body.strip()
        att_m = ATTACH_RE.match(b)
        if att_m:
            att = att_m.group(1)
            body = ''
        else:
            # a veces adjunto y texto vienen en el mismo renglón
            m2 = ATTACH_RE.search(b)
            if m2:
                att = m2.group(1)
                body = ATTACH_RE.sub('', b).strip()
        yield {
            'ts': f"20{yy}-{mm}-{dd} {hhmmss} {ampm}",
            'author': author.strip(),
            'text': body.strip(),
            'attachment': att,
            'kind': 'msg',
        }


def main(path: str, out_csv: str = 'logs/chat_text.csv'):
    p = Path(path)
    txt = p.read_text(encoding='utf-8', errors='ignore')
    rows = []
    for rec in parse_chat_lines(txt):
        text = rec.get('text') or ''
        cat = sev = prob = model = oem = oil = None
        if storage and text:
            try:
                sig = storage.extract_signals(text)
                if isinstance(sig, dict):
                    model = sig.get('model')
                    oem = sig.get('oem')
                    oil = sig.get('oil_code')
                    cat = sig.get('category')
                    sev = sig.get('severity')
                    prob = sig.get('problem')
            except Exception:
                pass
        rows.append({
            'ts': rec.get('ts',''),
            'author': rec.get('author',''),
            'text': text,
            'attachment': rec.get('attachment',''),
            'category': cat or '',
            'severity': sev or '',
            'problem': prob or '',
            'model': model or '',
            'oem': oem or '',
            'oil_code': oil or '',
        })
    outp = Path(out_csv)
    outp.parent.mkdir(parents=True, exist_ok=True)
    with outp.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ['ts','author','text','attachment','category','severity','problem','model','oem','oil_code'])
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"OK: wrote {len(rows)} rows to {outp}")


if __name__ == '__main__':
    path = sys.argv[1] if len(sys.argv)>1 else 'WhatsApp Chat - Higer Postventa AGS 2/_chat.txt'
    out = sys.argv[2] if len(sys.argv)>2 else 'rag-pinecone/logs/chat_text.csv'
    main(path, out)
