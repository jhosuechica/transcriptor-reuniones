"""Ejecuta trabajo pesado en un proceso aparte y recibe sus mensajes.

El trabajador recibe una conexión y envía tuplas (tipo, dato):
    estado, log, progreso, resultado, error_usuario, error, fin
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import threading
import time
from collections.abc import Callable

import windows

log = logging.getLogger(__name__)


class Cancelado(Exception):
    """El usuario canceló el trabajo."""


class FalloProceso(Exception):
    """El trabajo falló, pero puede funcionar con otra configuración."""


class ErrorUsuario(Exception):
    """Error que no se arregla reintentando, con un mensaje para el usuario."""


def ejecutar(
    trabajador: Callable,
    args: tuple,
    al_estado: Callable[[str], None] = lambda _t: None,
    al_progreso: Callable[[float], None] = lambda _p: None,
    cancelar: threading.Event | None = None,
    limite: float | None = None,
) -> list:
    """Devuelve los resultados enviados. limite: segundos máximos sin mensajes."""
    contexto = mp.get_context("spawn")
    receptor, emisor = contexto.Pipe(duplex=False)
    proceso = contexto.Process(target=trabajador, args=(emisor, *args), daemon=True)
    proceso.start()
    windows.bajar_prioridad(proceso.sentinel)
    emisor.close()  # así recv() detecta si el hijo muere
    resultados: list = []
    ultimo = time.monotonic()
    try:
        while True:
            if cancelar is not None and cancelar.is_set():
                proceso.kill()      # no hay nada que esperar
                raise Cancelado
            if not receptor.poll(0.2):
                if limite is not None and time.monotonic() - ultimo > limite:
                    raise FalloProceso("el proceso dejó de responder")
                continue
            try:
                tipo, dato = receptor.recv()
            except (EOFError, OSError):
                proceso.join(10)
                raise FalloProceso(
                    f"el proceso se cerró de forma inesperada (código {proceso.exitcode})"
                ) from None
            ultimo = time.monotonic()
            if tipo == "estado":
                log.info(dato)
                al_estado(dato)
            elif tipo == "log":
                log.info(dato)
            elif tipo == "progreso":
                al_progreso(dato)
            elif tipo == "resultado":
                resultados.append(dato)
            elif tipo == "error_usuario":
                raise ErrorUsuario(dato)
            elif tipo == "error":
                raise FalloProceso(dato)
            elif tipo == "fin":
                return resultados
    finally:
        receptor.close()
        proceso.join(5)
        if proceso.is_alive():
            proceso.kill()
            proceso.join(10)


def explicar(detalle: str) -> str:
    """Traduce los errores de memoria, que son los más habituales."""
    if "out of memory" in detalle.lower() or "MemoryError" in detalle:
        return "no hay memoria suficiente"
    return detalle
