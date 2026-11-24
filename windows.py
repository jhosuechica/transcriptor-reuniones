"""Funciones propias de Windows: atajo global, instancia única y suspensión."""

from __future__ import annotations

import ctypes
import logging
import threading
from collections.abc import Callable
from ctypes import wintypes

log = logging.getLogger(__name__)

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
_user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
_user32.GetMessageW.argtypes = (
    ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
)
_user32.PostThreadMessageW.argtypes = (
    wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
)
_kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
_kernel32.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
_kernel32.SetThreadExecutionState.argtypes = (wintypes.DWORD,)
_kernel32.SetThreadExecutionState.restype = wintypes.DWORD

WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
MODIFICADORES = {"alt": 0x1, "ctrl": 0x2, "shift": 0x4}
SIN_REPETICION = 0x4000         # mantener pulsado cuenta como una sola vez
YA_REGISTRADO = 1409
YA_EXISTE = 183
PRIORIDAD_BAJA = 0x4000         # BELOW_NORMAL_PRIORITY_CLASS
CONTINUO, SISTEMA_ACTIVO = 0x80000000, 0x1


class ErrorAtajo(Exception):
    """Error del atajo, con un mensaje apto para mostrar al usuario."""


def interpretar(texto: str) -> tuple[int, int]:
    """Convierte 'ctrl+alt+r' en (modificadores, tecla) de Windows."""
    partes = [p.strip() for p in texto.lower().split("+")]
    *mods, tecla = partes
    if any(m not in MODIFICADORES for m in mods) or len(set(mods)) != len(mods):
        raise ErrorAtajo(f"Atajo no válido: {texto}. Ejemplo: ctrl+alt+r")
    if len(tecla) == 1 and tecla.isascii() and tecla.isalnum():
        codigo = ord(tecla.upper())
    elif tecla[:1] == "f" and tecla[1:].isdecimal() and 1 <= int(tecla[1:]) <= 24:
        codigo = 0x6F + int(tecla[1:])
    else:
        raise ErrorAtajo(f"Tecla no válida: {tecla}. Usa una letra, un número o F1 a F24.")
    if not {"ctrl", "alt"} & set(mods):
        raise ErrorAtajo("El atajo debe incluir ctrl o alt, para no bloquear una tecla normal.")
    return sum(MODIFICADORES[m] for m in mods), codigo


class Atajo:
    """Atajo global. Windows avisa solo de esta combinación: no se lee el teclado."""

    ID = 1

    def __init__(self, texto: str, al_pulsar: Callable[[], None]):
        self._mods, self._tecla = interpretar(texto)
        self.texto = texto
        self._al_pulsar = al_pulsar
        self._hilo: threading.Thread | None = None
        self._id_hilo = 0
        self._listo = threading.Event()
        self._error = 0

    def iniciar(self) -> None:
        # El registro pertenece al hilo que lo hace: los mensajes llegan a él.
        self._listo.clear()
        self._id_hilo = self._error = 0
        self._hilo = threading.Thread(target=self._bucle, name="atajo", daemon=True)
        self._hilo.start()
        self._listo.wait(5)
        if not self._id_hilo:
            self._hilo = None
            if self._error == YA_REGISTRADO:
                raise ErrorAtajo(f"Otro programa ya usa el atajo {self.texto}. Cámbialo en Ajustes.")
            raise ErrorAtajo(f"No se pudo registrar el atajo {self.texto} (error {self._error}).")

    def detener(self) -> None:
        if self._hilo is not None:
            _user32.PostThreadMessageW(self._id_hilo, WM_QUIT, 0, 0)
            self._hilo.join(2)
            self._hilo = None

    def _bucle(self) -> None:
        if not _user32.RegisterHotKey(None, self.ID, self._mods | SIN_REPETICION, self._tecla):
            self._error = ctypes.get_last_error()
            self._listo.set()
            return
        self._id_hilo = threading.get_native_id()
        self._listo.set()
        mensaje = wintypes.MSG()
        try:
            # GetMessageW devuelve 0 con WM_QUIT y -1 si falla.
            while _user32.GetMessageW(ctypes.byref(mensaje), None, 0, 0) > 0:
                if mensaje.message == WM_HOTKEY:
                    self._al_pulsar()
        finally:
            _user32.UnregisterHotKey(None, self.ID)


class InstanciaUnica:
    """Impide abrir la aplicación dos veces."""

    def __init__(self, nombre: str = "Local\\TranscriptorReuniones"):
        ctypes.set_last_error(0)
        self._mutex = _kernel32.CreateMutexW(None, False, nombre)
        self.es_la_primera = ctypes.get_last_error() != YA_EXISTE

    def cerrar(self) -> None:
        if self._mutex:
            _kernel32.CloseHandle(self._mutex)
            self._mutex = None


def impedir_suspension(activo: bool) -> None:
    """Evita que el equipo se suspenda. Vale para el hilo que la llama."""
    _kernel32.SetThreadExecutionState(CONTINUO | (SISTEMA_ACTIVO if activo else 0))


def bajar_prioridad(manejador: int) -> None:
    """Da prioridad a la grabación y a la ventana sobre la transcripción."""
    if not _kernel32.SetPriorityClass(manejador, PRIORIDAD_BAJA):
        log.warning("No se pudo bajar la prioridad (error %s)", ctypes.get_last_error())
