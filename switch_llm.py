import sys
from pathlib import Path

def update_env_llm(env_path: Path, model: str) -> bool:
    lines = []
    changed = False
    if env_path.exists():
        lines = env_path.read_text(encoding='utf-8').splitlines()
        for i, ln in enumerate(lines):
            if ln.strip().startswith('LLM_MODEL='):
                lines[i] = f'LLM_MODEL="{model}"'
                changed = True
                break
    if not changed:
        lines.append(f'LLM_MODEL="{model}"')
    env_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return True

def main():
    if len(sys.argv) < 2:
        print('Uso: python3 switch_llm.py <MODEL_OR_FT_ID>')
        sys.exit(1)
    model = sys.argv[1]
    env_path = Path(__file__).parent / '.env'
    update_env_llm(env_path, model)
    print(f'LLM_MODEL actualizado a: {model} en {env_path}')
    # Reiniciar servicios
    import subprocess, os
    subprocess.run(['bash', './run.sh', 'restart'], cwd=str(Path(__file__).parent))

if __name__ == '__main__':
    main()

