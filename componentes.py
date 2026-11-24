"""Piezas que comparten las ventanas: colores, icono y base de los diálogos."""

from __future__ import annotations

import logging
import tkinter as tk
from pathlib import Path

import customtkinter as ctk

log = logging.getLogger(__name__)

NOMBRE_APP = "Transcriptor de reuniones"
ICONO = Path(__file__).resolve().parent / "recursos" / "icono.ico"
ROJO, VERDE, AMARILLO = "#d33a3a", "#2e9e5b", "#d9a400"
ROJO_OSCURO, GRIS = "#a82b2b", "gray50"


def poner_icono(ventana: ctk.CTk | ctk.CTkToplevel) -> None:
    try:
        ventana.iconbitmap(ICONO)
    except tk.TclError:
        log.warning("No se pudo cargar el icono %s", ICONO)


def _centrar(ventana: tk.Misc, padre: tk.Misc, ancho: int, alto: int) -> None:
    """Coloca un diálogo sobre la ventana o, si está minimizada, en la pantalla."""
    escala = ctk.ScalingTracker.get_window_scaling(ventana)
    w, h = int(ancho * escala), int(alto * escala)
    if padre.winfo_viewable():
        x = padre.winfo_rootx() + (padre.winfo_width() - w) // 2
        y = padre.winfo_rooty() + (padre.winfo_height() - h) // 2
    else:
        x = (ventana.winfo_screenwidth() - w) // 2
        y = (ventana.winfo_screenheight() - h) // 3
    # CTk escala el tamaño, pero no la posición.
    ventana.geometry(f"{ancho}x{alto}+{max(0, x)}+{max(0, y)}")


class Dialogo(ctk.CTkToplevel):
    """Base de los diálogos: modal, centrado y con Escape para cerrar."""

    def __init__(self, app: ctk.CTk, titulo: str, ancho: int, alto: int):
        super().__init__(app)
        self.app = app
        self.title(titulo)
        poner_icono(self)
        _centrar(self, app, ancho, alto)
        self.protocol("WM_DELETE_WINDOW", self.cerrar)
        self.bind("<Escape>", lambda _e: self.cerrar())
        # La ventana debe ser visible para poder capturar el foco.
        self.after(150, self._enfocar)

    def _enfocar(self) -> None:
        if not self.winfo_exists():
            return
        self.lift()
        self.focus_force()
        try:
            self.grab_set()
        except tk.TclError:
            log.warning("El diálogo no es modal", exc_info=True)
        self.foco_inicial()

    def foco_inicial(self) -> None:
        pass

    def cerrar(self) -> None:
        self.grab_release()
        self.destroy()
