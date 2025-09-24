"""Compatibilidad: ingesta con OCR utilizando la versión unificada."""

from app.cli import run_ingesta


if __name__ == "__main__":
    run_ingesta(["--ocr"])
