import json
import csv
import argparse
from pathlib import Path

def load_jsonl(path: Path, limit: int | None = None):
    out = []
    with path.open(encoding='utf-8') as f:
        for i, line in enumerate(f, 1):
            try:
                out.append(json.loads(line))
                if limit and len(out) >= limit:
                    break
            except Exception:
                continue
    return out

def run_eval(in_file: Path, out_csv: Path, model: str):
    from openai import OpenAI
    cli = OpenAI()
    rows = []
    for rec in load_jsonl(in_file):
        msgs = rec.get('messages') or []
        # soporta mensajes estilo OpenAI chat
        try:
            r = cli.chat.completions.create(model=model, messages=msgs, temperature=0.2)
            txt = r.choices[0].message.content
        except Exception as e:
            txt = f"<error: {e}>"
        rows.append({'prompt_len': sum(len((m.get('content') or '')) for m in msgs), 'answer_len': len(txt or ''), 'answer': txt})
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=['prompt_len','answer_len','answer'])
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"OK: evaluated {len(rows)} examples -> {out_csv}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('infile', nargs='?', default='rag-pinecone/datasets/ft_style_curated.jsonl')
    ap.add_argument('--model', default='gpt-4o')
    ap.add_argument('--out', default='rag-pinecone/logs/ft_eval.csv')
    args = ap.parse_args()
    run_eval(Path(args.infile), Path(args.out), args.model)

if __name__ == '__main__':
    main()

