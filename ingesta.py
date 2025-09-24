"""Compatibilidad: delega en la ingesta unificada del paquete `app`."""

from app.cli import run_ingesta


if __name__ == "__main__":
    run_ingesta()
