"""Arranque de producción (Raspberry Pi): waitress escuchando en la red local.

Uso:
    python serve.py

Lee la configuración de `.env` (junto a este fichero) y de variables de entorno:
    HOST     dirección de escucha (por defecto 0.0.0.0 = toda la red local)
    PORT     puerto (por defecto 5000)
    THREADS  hilos de waitress (por defecto 4; suficiente para la Pi Zero 2 W)

A diferencia de `python app.py` (desarrollo), aquí no hay modo debug ni
recargador: un único proceso, así que el scheduler arranca una sola vez.
"""
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent


def server_settings(env):
    """Extrae y valida host, puerto e hilos de un diccionario de entorno."""
    host = env.get("HOST", "").strip() or "0.0.0.0"
    try:
        port = int(env.get("PORT", "5000"))
        threads = int(env.get("THREADS", "4"))
    except ValueError as exc:
        raise SystemExit(f"PORT y THREADS deben ser números: {exc}") from exc
    if not 1 <= port <= 65535:
        raise SystemExit(f"PORT fuera de rango: {port}")
    if threads < 1:
        raise SystemExit(f"THREADS debe ser al menos 1: {threads}")
    return host, port, threads


def main():
    # Ruta explícita: con systemd el directorio de trabajo podría no ser este.
    load_dotenv(BASE_DIR / ".env")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    host, port, threads = server_settings(os.environ)
    if not os.environ.get("SECRET_KEY"):
        logging.warning("SECRET_KEY no está definida en .env: se usa una clave de desarrollo.")

    # Importar después de load_dotenv: create_app lee las variables de entorno.
    from waitress import serve

    from app import create_app

    app = create_app()
    logging.info("Escuchando en http://%s:%s (%s hilos)", host, port, threads)
    serve(app, host=host, port=port, threads=threads)


if __name__ == "__main__":
    main()
