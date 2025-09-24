import json
import sys
from pathlib import Path


def load_csv(path: Path):
    import csv
    if not path.exists():
        return []
    with path.open(newline='', encoding='utf-8') as f:
        r = csv.DictReader(f)
        return list(r)


def main(out_path: str = 'datasets/ft_classify.jsonl'):
    base = Path(__file__).parent
    chat_csv = base/'logs/chat_text.csv'
    rows = load_csv(chat_csv)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out.open('w', encoding='utf-8') as f:
        for r in rows:
            text = (r.get('text') or '').strip()
            if not text:
                continue
            labels = {
                'category': r.get('category') or 'general',
                'severity': r.get('severity') or 'normal',
                'problem': r.get('problem') or ''
            }
            f.write(json.dumps({'text': text, 'labels': labels}, ensure_ascii=False) + '\n')
            n += 1
    print(f'OK: wrote {n} examples to {out}')


if __name__ == '__main__':
    path = sys.argv[1] if len(sys.argv)>1 else 'datasets/ft_classify.jsonl'
    main(path)

