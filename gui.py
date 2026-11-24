"""Ventana de la aplicación.

Reglas de hilos:
- Solo el hilo principal toca la ventana. Los demás dejan funciones en la cola
  `eventos`, que se vacía cada pocos milisegundos.
- La grabación se inicia y se detiene siempre en el hilo `audio`: las API de
  audio de Windows deben abrirse y cerrarse desde el mismo hilo.
- La transcripción corre en el hilo `trabajo`, que a su vez usa otro proceso.
- La memoria se recoge solo en el hilo principal: si otro hilo destruyera un
  objeto de la ventana, quedaría bloqueado (y podría ser el que graba).
"""

from __future__ import annotations

import os
import sys

# Con pythonw no hay consola y estos flujos son None: las librerías que escriben
# en ellos fallarían. Va aquí arriba porque los procesos hijos importan este módulo.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

import gc
import logging
import multiprocessing
import queue
import threading
import tkinter as tk
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tkinter import messagebox, ttk

import customtkinter as ctk

import config
import exporter
import flujo
import merger
import proceso
import transcriber
import windows
from recorder import SILENCIO_DBFS, Grabadora, listar_dispositivos

log = logging.getLogger(__name__)

NOMBRE_APP = "Transcriptor de reuniones"
ICONO = Path(__file__).resolve().parent / "recursos" / "icono.ico"
CONSENTIMIENTO = (
    "Avisa a los participantes de que vas a grabar y transcribir la reunión, "
    "y pide su consentimiento antes de continuar."
)
INTERVALO_MS = 50
LIMPIEZA_MS = 5000          # cada cuánto se recoge la memoria
NIVEL_MINIMO = -60.0        # dBFS en el extremo izquierdo del medidor
CAIDA_POR_PASO = 2.0        # dB que baja el medidor en cada paso, para poder leerlo
ESPERA_SIN_SENAL = 10.0     # s en silencio antes de avisar
PREDETERMINADO = "Predeterminado de Windows"
AUTOMATICO = "Automático"
ROJO, VERDE, AMARILLO = "#d33a3a", "#2e9e5b", "#d9a400"
ROJO_OSCURO, GRIS = "#a82b2b", "gray50"

PARADO, INICIANDO, GRABANDO, DETENIENDO = "parado", "iniciando", "grabando", "deteniendo"
DISPOSITIVOS_CALCULO = {"Automático": "auto", "Solo GPU": "gpu", "Solo CPU": "cpu"}


def _poner_icono(ventana: ctk.CTk | ctk.CTkToplevel) -> None:
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

    def __init__(self, app: "Aplicacion", titulo: str, ancho: int, alto: int):
        super().__init__(app)
        self.app = app
        self.title(titulo)
        _poner_icono(self)
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


class DialogoInicio(Dialogo):
    """Pide el título y recuerda el consentimiento antes de grabar."""

    def __init__(self, app: "Aplicacion", al_aceptar: Callable[[str], None]):
        super().__init__(app, "Nueva grabación", 440, 270)
        self._al_aceptar = al_aceptar
        # Con el atajo puede haber otra aplicación delante, incluso a pantalla completa.
        self.attributes("-topmost", True)
        self.resizable(False, False)
        ctk.CTkLabel(self, text="Antes de grabar", font=ctk.CTkFont(size=16, weight="bold")).pack(
            padx=20, pady=(18, 4), anchor="w")
        ctk.CTkLabel(self, text=CONSENTIMIENTO, wraplength=400, justify="left").pack(
            padx=20, anchor="w")
        ctk.CTkLabel(self, text="Título de la reunión").pack(padx=20, pady=(14, 2), anchor="w")
        self.titulo = ctk.CTkEntry(self, placeholder_text="Reunión")
        self.titulo.pack(padx=20, fill="x")
        botones = ctk.CTkFrame(self, fg_color="transparent")
        botones.pack(padx=20, pady=18, fill="x", side="bottom")
        self.boton = ctk.CTkButton(
            botones, text="He avisado: grabar", fg_color=ROJO, hover_color=ROJO_OSCURO,
            command=self.aceptar)
        self.boton.pack(side="right")
        ctk.CTkButton(botones, text="Cancelar", fg_color=GRIS, command=self.cerrar).pack(
            side="right", padx=8)
        self.bind("<Return>", lambda _e: self.aceptar())

    def foco_inicial(self) -> None:
        self.titulo.focus_set()

    def aceptar(self) -> None:
        titulo = self.titulo.get().strip() or "Reunión"
        self.cerrar()
        self._al_aceptar(titulo)


class DialogoNombres(Dialogo):
    """Pone nombre a los hablantes, mostrando qué dijo cada uno."""

    def __init__(self, app: "Aplicacion", t: merger.Transcripcion,
                 al_aceptar: Callable[[dict[str, str]], None]):
        super().__init__(app, "Nombres de los hablantes", 560, 520)
        self._al_aceptar = al_aceptar
        self._actuales = dict(t.hablantes)
        ctk.CTkLabel(
            self, text="Si dos hablantes son la misma persona, ponles el mismo nombre.",
        ).pack(padx=16, pady=(12, 4), anchor="w")
        zona = ctk.CTkScrollableFrame(self)
        zona.pack(padx=12, fill="both", expand=True)
        self.campos: dict[str, ctk.CTkEntry] = {}
        for ident, nombre in t.hablantes.items():
            frases = [f for i in t.intervenciones if i.hablante == ident for f in i.frases]
            if not frases:
                continue
            habla = merger.hms(sum(f.fin - f.inicio for f in frases))
            ctk.CTkLabel(zona, text=f"{nombre}  ·  habla {habla}", font=ctk.CTkFont(weight="bold")).pack(
                anchor="w", pady=(10, 0))
            campo = ctk.CTkEntry(zona)
            campo.insert(0, nombre)
            campo.pack(fill="x", pady=2)
            self.campos[ident] = campo
            largas = sorted(frases, key=lambda f: -len(f.texto))[:2]
            for f in sorted(largas, key=lambda f: f.inicio):
                ctk.CTkLabel(
                    zona, text=f"[{merger.hms(f.inicio)}] {f.texto}", text_color=GRIS,
                    wraplength=480, justify="left",
                ).pack(anchor="w")
        botones = ctk.CTkFrame(self, fg_color="transparent")
        botones.pack(padx=12, pady=12, fill="x")
        ctk.CTkButton(botones, text="Guardar", command=self.aceptar).pack(side="right")
        ctk.CTkButton(botones, text="Cancelar", fg_color=GRIS, command=self.cerrar).pack(
            side="right", padx=8)

    def aceptar(self) -> None:
        cambios = {}
        for ident, campo in self.campos.items():
            nombre = campo.get().strip()
            # Un campo vacío deja el nombre como estaba.
            if nombre and nombre != self._actuales[ident]:
                cambios[ident] = nombre
        self.cerrar()
        if cambios:
            self._al_aceptar(cambios)


class DialogoAjustes(Dialogo):
    def __init__(self, app: "Aplicacion", micros: list[str], salidas: list[str]):
        super().__init__(app, "Ajustes", 560, 560)
        cfg = app.cfg
        zona = ctk.CTkFrame(self, fg_color="transparent")
        zona.pack(padx=18, pady=(14, 0), fill="both", expand=True)

        def etiqueta(texto: str) -> None:
            ctk.CTkLabel(zona, text=texto).pack(anchor="w", pady=(6, 0))

        def entrada(texto: str, valor: str) -> ctk.CTkEntry:
            etiqueta(texto)
            e = ctk.CTkEntry(zona)
            e.insert(0, valor)
            e.pack(fill="x")
            return e

        def lista(texto: str, opciones: list[str], valor: str) -> ctk.CTkOptionMenu:
            etiqueta(texto)
            m = ctk.CTkOptionMenu(zona, values=opciones, dynamic_resizing=False)
            m.set(valor)
            m.pack(fill="x")
            return m

        self.nombre = entrada("Tu nombre en la transcripción", cfg.nombre_usuario)
        self.micro = lista("Micrófono", *self._opciones(micros, cfg.microfono))
        self.salida = lista("Salida por la que suena la reunión", *self._opciones(salidas, cfg.salida))
        actual = next((k for k, v in DISPOSITIVOS_CALCULO.items() if v == cfg.dispositivo), AUTOMATICO)
        self.dispositivo = lista("Dónde transcribir", list(DISPOSITIVOS_CALCULO), actual)
        self.vocabulario = entrada("Nombres y términos frecuentes, separados por comas", cfg.vocabulario)
        self.atajo = entrada("Atajo para grabar y detener", cfg.atajo)
        self.flotante = ctk.CTkCheckBox(zona, text="Mostrar el indicador flotante al grabar")
        self.flotante.pack(anchor="w", pady=(12, 0))
        self._micro_inicial, self._salida_inicial = self.micro.get(), self.salida.get()
        if cfg.indicador_flotante:
            self.flotante.select()
        botones = ctk.CTkFrame(self, fg_color="transparent")
        botones.pack(padx=18, pady=14, fill="x")
        ctk.CTkButton(botones, text="Guardar", command=self.aceptar).pack(side="right")
        ctk.CTkButton(botones, text="Cancelar", fg_color=GRIS, command=self.cerrar).pack(
            side="right", padx=8)

    @staticmethod
    def _opciones(nombres: list[str], configurado: str) -> tuple[list[str], str]:
        """Opciones del menú y la elegida. La configuración puede ser parte del nombre."""
        if not configurado:
            return [PREDETERMINADO, *nombres], PREDETERMINADO
        elegido = next((n for n in nombres if configurado.lower() in n.lower()), None)
        if elegido is None:     # desconectado ahora: se conserva
            return [PREDETERMINADO, configurado, *nombres], configurado
        return [PREDETERMINADO, *nombres], elegido

    def aceptar(self) -> None:
        def valor(menu: ctk.CTkOptionMenu, inicial: str, configurado: str) -> str:
            # Sin cambios se conserva lo configurado, aunque sea parte del nombre:
            # sigue valiendo si Windows renumera el dispositivo.
            if menu.get() == inicial:
                return configurado
            return "" if menu.get() == PREDETERMINADO else menu.get()

        nueva = replace(
            self.app.cfg,
            nombre_usuario=self.nombre.get().strip() or "Yo",
            microfono=valor(self.micro, self._micro_inicial, self.app.cfg.microfono),
            salida=valor(self.salida, self._salida_inicial, self.app.cfg.salida),
            dispositivo=DISPOSITIVOS_CALCULO[self.dispositivo.get()],
            vocabulario=self.vocabulario.get().strip(),
            atajo=self.atajo.get().strip().lower(),
            indicador_flotante=bool(self.flotante.get()),
        )
        # Si algo no es válido, el diálogo sigue abierto para corregirlo.
        if self.app.aplicar_ajustes(nueva, self):
            self.cerrar()


class Indicador(tk.Toplevel):
    """Aviso pequeño y siempre visible mientras se graba. Se puede arrastrar."""

    def __init__(self, app: "Aplicacion"):
        super().__init__(app, bg=ROJO)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.texto = tk.Label(
            self, bg=ROJO, fg="white", font=("Segoe UI", 11, "bold"), padx=12, pady=5)
        self.texto.pack()
        self.poner("● GRABANDO 00:00:00")
        self.update_idletasks()
        self.geometry(f"+{self.winfo_screenwidth() - self.winfo_reqwidth() - 24}+24")
        self._origen = (0, 0)
        self.texto.bind("<Button-1>", self._agarrar)
        self.texto.bind("<B1-Motion>", self._arrastrar)
        self.texto.bind("<Double-Button-1>", lambda _e: app.mostrar())

    def poner(self, texto: str) -> None:
        self.texto.configure(text=texto)

    def _agarrar(self, e) -> None:
        self._origen = (e.x, e.y)

    def _arrastrar(self, e) -> None:
        self.geometry(f"+{e.x_root - self._origen[0]}+{e.y_root - self._origen[1]}")


class Medidor(ctk.CTkFrame):
    """Barra de nivel de una pista, de -60 a 0 dBFS."""

    def __init__(self, padre, nombre: str):
        super().__init__(padre, fg_color="transparent")
        ctk.CTkLabel(self, text=nombre, width=90, anchor="w").pack(side="left")
        self.barra = ctk.CTkProgressBar(self, height=14, progress_color=VERDE)
        self.barra.pack(side="left", fill="x", expand=True, padx=8)
        self.valor = ctk.CTkLabel(self, text="", width=90, anchor="e")
        self.valor.pack(side="left")
        self.nivel = SILENCIO_DBFS
        self._dibujado: tuple | None = None
        self.poner(SILENCIO_DBFS, activo=False)

    def poner(self, dbfs: float, activo: bool = True, sin_senal: bool = False) -> None:
        # Sube al instante y baja despacio, como un medidor de sonido.
        self.nivel = max(dbfs, self.nivel - CAIDA_POR_PASO) if activo else SILENCIO_DBFS
        n = round(max(0.0, min(1.0, 1 - self.nivel / NIVEL_MINIMO)), 3)
        color = ROJO if self.nivel > -1 else AMARILLO if self.nivel > -6 else VERDE
        texto = "" if not activo else "Sin señal" if sin_senal else f"{max(self.nivel, NIVEL_MINIMO):.0f} dB"
        if (n, color, texto) != self._dibujado:     # dibujar es lo más costoso
            self._dibujado = (n, color, texto)
            self.barra.configure(progress_color=color if n else self.barra.cget("fg_color"))
            self.barra.set(n)
            self.valor.configure(text=texto, text_color=ROJO if sin_senal else ("gray10", "gray90"))


class Aplicacion(ctk.CTk):
    def __init__(self, cfg: config.Config, grabadora: Grabadora | None = None,
                 ruta_config: Path = config.RUTA_CONFIG):
        super().__init__()
        gc.disable()    # ver las reglas de hilos, al principio del archivo
        self.cfg = cfg
        self.ruta_config = ruta_config
        self.grabadora = grabadora or Grabadora(cfg)
        self.eventos: queue.Queue[Callable[[], None]] = queue.Queue()
        self.audio = ThreadPoolExecutor(1, "audio")
        self.trabajo = ThreadPoolExecutor(1, "trabajo")
        self.varios = ThreadPoolExecutor(2, "varios")
        self.estado = PARADO
        self.carpeta_activa: Path | None = None
        self.procesando: Path | None = None
        self.cancelar = threading.Event()
        self.reuniones: dict[str, flujo.Reunion] = {}
        self.atajo: windows.Atajo | None = None
        self.dialogo_inicio: DialogoInicio | None = None
        self.indicador: Indicador | None = None
        self._avisados: set[str] = set()
        self._pasos = 0
        self._cerrando = False
        self._mostrando_error = False
        self._mostrada: tuple | None = None

        self._construir()
        self.protocol("WM_DELETE_WINDOW", self.salir)
        self.refrescar()
        self._sondear()
        self._activar_atajo()
        # Lo creado hasta aquí dura toda la sesión: no hace falta revisarlo en
        # cada limpieza, que así es breve y no interrumpe la grabación.
        gc.collect()
        gc.freeze()
        self._tarea_limpieza = self.after(LIMPIEZA_MS, self._limpiar_memoria)

    # ----------------------------------------------------------------------
    # Construcción
    # ----------------------------------------------------------------------

    def _construir(self) -> None:
        self.title(NOMBRE_APP)
        _poner_icono(self)
        self.geometry("1080x700")
        self.minsize(900, 560)
        self.grid_columnconfigure(0, weight=2, uniform="col")
        self.grid_columnconfigure(1, weight=3, uniform="col")
        self.grid_rowconfigure(1, weight=1)

        # Grabación
        arriba = ctk.CTkFrame(self)
        arriba.grid(row=0, column=0, columnspan=2, sticky="ew", padx=12, pady=(12, 6))
        arriba.grid_columnconfigure(1, weight=1)
        self.boton_grabar = ctk.CTkButton(
            arriba, text="●  Grabar", width=170, height=56, font=ctk.CTkFont(size=17, weight="bold"),
            fg_color=ROJO, hover_color=ROJO_OSCURO, command=self.alternar)
        self.boton_grabar.grid(row=0, column=0, rowspan=2, padx=14, pady=14)
        self.medidor_mic = Medidor(arriba, "Micrófono")
        self.medidor_mic.grid(row=0, column=1, sticky="ew", pady=(12, 2))
        self.medidor_sis = Medidor(arriba, "Sistema")
        self.medidor_sis.grid(row=1, column=1, sticky="ew", pady=(2, 12))
        self.reloj = ctk.CTkLabel(arriba, text="00:00:00", width=170,
                                  font=ctk.CTkFont(size=24, weight="bold"))
        self.reloj.grid(row=0, column=2, rowspan=2, padx=14)
        self.boton_ajustes = ctk.CTkButton(
            arriba, text="Ajustes", width=90, fg_color=GRIS, command=self.abrir_ajustes)
        self.boton_ajustes.grid(row=0, column=3, rowspan=2, padx=(0, 14))
        self.aviso = ctk.CTkLabel(arriba, text="", text_color=ROJO, wraplength=980, justify="left")
        self.aviso.grid(row=2, column=0, columnspan=4, sticky="w", padx=14)
        self.aviso.grid_remove()

        # Lista de reuniones
        izquierda = ctk.CTkFrame(self)
        izquierda.grid(row=1, column=0, sticky="nsew", padx=(12, 6), pady=6)
        izquierda.grid_rowconfigure(1, weight=1)
        izquierda.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(izquierda, text="Grabaciones", font=ctk.CTkFont(size=15, weight="bold")).grid(
            row=0, column=0, sticky="w", padx=12, pady=(8, 4))
        self.lista = ttk.Treeview(
            izquierda, columns=("fecha", "titulo", "duracion", "estado"), show="headings",
            selectmode="browse")
        escala = ctk.ScalingTracker.get_window_scaling(self)
        for col, texto, ancho in (("fecha", "Fecha", 116), ("titulo", "Título", 100),
                                  ("duracion", "Duración", 66), ("estado", "Estado", 92)):
            self.lista.heading(col, text=texto, anchor="w")
            self.lista.column(col, width=int(ancho * escala), minwidth=int(50 * escala),
                              stretch=col == "titulo")
        self.lista.grid(row=1, column=0, sticky="nsew", padx=(10, 0), pady=(0, 10))
        barra = ctk.CTkScrollbar(izquierda, command=self.lista.yview)
        barra.grid(row=1, column=1, sticky="ns", pady=(0, 10), padx=(0, 4))
        self.lista.configure(yscrollcommand=barra.set)
        self.lista.bind("<<TreeviewSelect>>", lambda _e: self._mostrar_seleccion())
        self._estilo_lista()

        # Detalle
        derecha = ctk.CTkFrame(self)
        derecha.grid(row=1, column=1, sticky="nsew", padx=(6, 12), pady=6)
        derecha.grid_rowconfigure(2, weight=1)
        derecha.grid_columnconfigure(0, weight=1)
        opciones = ctk.CTkFrame(derecha, fg_color="transparent")
        opciones.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        ctk.CTkLabel(opciones, text="Modelo").pack(side="left")
        self.modelo = ctk.CTkOptionMenu(opciones, width=140, values=[AUTOMATICO],
                                        command=lambda _v: self._guardar_modelo())
        self.modelo.pack(side="left", padx=(6, 14))
        ctk.CTkLabel(opciones, text="Personas, sin contarte").pack(side="left")
        self.personas = ctk.CTkOptionMenu(
            opciones, width=110, values=[AUTOMATICO, *map(str, range(1, 11))])
        self.personas.pack(side="left", padx=6)
        botones = ctk.CTkFrame(derecha, fg_color="transparent")
        botones.grid(row=1, column=0, sticky="ew", padx=10, pady=4)
        self.boton_procesar = ctk.CTkButton(botones, text="Transcribir", width=110,
                                            command=self.procesar_seleccion)
        self.boton_nombres = ctk.CTkButton(botones, text="Nombres", width=90,
                                           command=self.abrir_nombres)
        self.boton_exportar = ctk.CTkButton(botones, text="Exportar", width=90,
                                            command=self.exportar)
        self.boton_carpeta = ctk.CTkButton(botones, text="Abrir carpeta", width=110, fg_color=GRIS,
                                           command=self.abrir_carpeta)
        for b in (self.boton_procesar, self.boton_nombres, self.boton_exportar, self.boton_carpeta):
            b.pack(side="left", padx=(0, 8))
        self.texto = ctk.CTkTextbox(derecha, wrap="word", font=ctk.CTkFont(size=13))
        self.texto.grid(row=2, column=0, sticky="nsew", padx=10, pady=(4, 10))
        self.texto.tag_config("hora", foreground=GRIS)
        self.texto.tag_config("nombre", foreground="#3b8ed0")
        self.texto.configure(state="disabled")

        # Estado
        abajo = ctk.CTkFrame(self)
        abajo.grid(row=2, column=0, columnspan=2, sticky="ew", padx=12, pady=(6, 12))
        abajo.grid_columnconfigure(0, weight=1)
        self.linea = ctk.CTkLabel(abajo, text="", anchor="w")
        self.linea.grid(row=0, column=0, sticky="ew", padx=12, pady=6)
        self.progreso = ctk.CTkProgressBar(abajo, width=220)
        self.progreso.set(0)
        self.progreso.grid(row=0, column=1, padx=8)
        self.progreso.grid_remove()     # solo se ve mientras se transcribe
        self.boton_cancelar = ctk.CTkButton(
            abajo, text="Cancelar", width=90, fg_color=GRIS, command=self.cancelar_proceso)
        self.boton_cancelar.grid(row=0, column=2, padx=(0, 8))
        self.auto = ctk.CTkCheckBox(abajo, text="Transcribir al terminar de grabar",
                                    command=self._guardar_auto)
        if self.cfg.transcribir_al_terminar:
            self.auto.select()
        self.auto.grid(row=0, column=3, padx=12)
        self._poner_modelos()
        self._actualizar_botones()

    def _set_appearance_mode(self, mode_string) -> None:
        # CustomTkinter lo llama al cambiar Windows entre claro y oscuro.
        super()._set_appearance_mode(mode_string)
        if hasattr(self, "lista"):
            self._estilo_lista()

    def _estilo_lista(self) -> None:
        # La lista es de ttk, que no sigue el tema de CustomTkinter.
        oscuro = ctk.get_appearance_mode() == "Dark"
        fondo, letra = ("#1d1e1e", "#dce4ee") if oscuro else ("#f9f9fa", "#1a1a1a")
        cabecera = "#2b2b2b" if oscuro else "#dbdbdb"
        escala = ctk.ScalingTracker.get_window_scaling(self)
        e = ttk.Style(self)
        e.theme_use("clam")     # el tema de Windows no permite cambiar los colores
        letra_px = -int(13 * escala)
        e.configure("Treeview", background=fondo, fieldbackground=fondo, foreground=letra,
                    borderwidth=0, bordercolor=fondo, lightcolor=fondo, darkcolor=fondo,
                    rowheight=int(28 * escala), font=("Segoe UI", letra_px))
        e.configure("Treeview.Heading", background=cabecera, foreground=letra, relief="flat",
                    font=("Segoe UI", letra_px))
        e.map("Treeview", background=[("selected", "#1f6aa5")], foreground=[("selected", "white")])
        e.map("Treeview.Heading", background=[("active", cabecera)])

    # ----------------------------------------------------------------------
    # Hilos
    # ----------------------------------------------------------------------

    def _sondear(self) -> None:
        # Se programa primero: un cuadro de mensaje no detiene los medidores.
        self._tarea = self.after(INTERVALO_MS, self._sondear)
        self._pasos += 1
        while True:
            try:
                funcion = self.eventos.get_nowait()
            except queue.Empty:
                break
            funcion()
        self._actualizar_grabacion()

    def _limpiar_memoria(self) -> None:
        self._tarea_limpieza = self.after(LIMPIEZA_MS, self._limpiar_memoria)
        gc.collect()

    def en_hilo(self, ejecutor: ThreadPoolExecutor, funcion: Callable,
                al_terminar: Callable[[object], None] = lambda _r: None,
                al_fallar: Callable[[BaseException], None] | None = None) -> None:
        """Ejecuta en otro hilo y entrega el resultado en el hilo de la ventana."""
        def entregar(f: Future) -> None:
            self.eventos.put(lambda: resolver(f))

        def resolver(f: Future) -> None:
            e = f.exception()
            if e is None:
                al_terminar(f.result())
                return
            if not isinstance(e, proceso.Cancelado):
                log.error("Falló una tarea", exc_info=e)
            (al_fallar or self.mostrar_error)(e)

        ejecutor.submit(funcion).add_done_callback(entregar)

    def report_callback_exception(self, tipo, valor, traza) -> None:
        """Errores no previstos en la ventana: se registran y se muestran."""
        log.error("Error en la ventana", exc_info=(tipo, valor, traza))
        # Un fallo que se repite no debe abrir un cuadro tras otro.
        if not self._mostrando_error:
            self._mostrando_error = True
            try:
                self.mostrar_error(valor)
            finally:
                self._mostrando_error = False

    def mostrar_error(self, e: BaseException, padre: tk.Misc | None = None) -> None:
        texto = str(e) if isinstance(e, (*flujo.ERRORES, windows.ErrorAtajo)) else (
            f"Error inesperado: {e or type(e).__name__}\n\nDetalles en {config.RUTA_LOG}")
        messagebox.showerror(NOMBRE_APP, texto, parent=padre or self)

    def mostrar(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()

    # ----------------------------------------------------------------------
    # Grabación
    # ----------------------------------------------------------------------

    def alternar(self) -> None:
        """Botón y atajo: pide iniciar o detiene. Se ignora si está a medias."""
        if self._cerrando:
            return
        if self.estado == PARADO:
            if self.dialogo_inicio is not None and self.dialogo_inicio.winfo_exists():
                self.dialogo_inicio.lift()      # no confirma: el aviso debe leerse
                return
            self.dialogo_inicio = DialogoInicio(self, self.iniciar)
        elif self.estado == GRABANDO:
            self.detener()

    def iniciar(self, titulo: str) -> None:
        if self.estado != PARADO:
            return
        self._poner_estado(INICIANDO)
        self._avisados.clear()
        self.grabadora.cfg = self.cfg

        def tarea():
            carpeta = self.grabadora.iniciar(titulo)
            windows.impedir_suspension(True)
            return carpeta

        self.en_hilo(self.audio, tarea, self._iniciada, self._no_iniciada)

    def _iniciada(self, carpeta: Path) -> None:
        self.carpeta_activa = carpeta
        self._poner_estado(GRABANDO)
        if self.cfg.indicador_flotante:
            self.indicador = Indicador(self)
        for a in self.grabadora.avisos:
            self._avisar(a)
        self.refrescar(carpeta.name)

    def _no_iniciada(self, e: BaseException) -> None:
        self._poner_estado(PARADO)
        self.mostrar_error(e)

    def detener(self, al_terminar: Callable[[], None] | None = None) -> None:
        if self.estado != GRABANDO:
            return
        # Se cambia antes de cerrar el audio, para dejar de consultar sus niveles.
        self._poner_estado(DETENIENDO)

        def tarea():
            try:
                return self.grabadora.detener()
            finally:
                windows.impedir_suspension(False)

        def terminar(sesion: dict | None, error: BaseException | None) -> None:
            carpeta, self.carpeta_activa = self.carpeta_activa, None
            self._poner_estado(PARADO)
            self._quitar_aviso()
            if al_terminar is not None:
                al_terminar()
                return
            self.refrescar(carpeta.name)
            if error is not None:
                self.mostrar_error(error)
                return
            self.linea.configure(text=f"Grabación guardada en {carpeta}")
            # Se lanza antes de mostrar avisos, que esperan al usuario.
            if self.cfg.transcribir_al_terminar:
                self.procesar(carpeta)
            if sesion["avisos"]:
                messagebox.showwarning(NOMBRE_APP, "\n\n".join(sesion["avisos"]), parent=self)

        self.en_hilo(self.audio, tarea, lambda s: terminar(s, None), lambda e: terminar(None, e))

    def _poner_estado(self, estado: str) -> None:
        self.estado = estado
        textos = {PARADO: "●  Grabar", INICIANDO: "Iniciando…",
                  GRABANDO: "■  Detener", DETENIENDO: "Guardando…"}
        self.boton_grabar.configure(
            text=textos[estado], state="normal" if estado in (PARADO, GRABANDO) else "disabled")
        if estado in (PARADO, DETENIENDO):
            if self.indicador is not None:
                self.indicador.destroy()
                self.indicador = None
            self.title(NOMBRE_APP)
            self.reloj.configure(text="00:00:00", text_color=("gray10", "gray90"))
            self.medidor_mic.poner(SILENCIO_DBFS, activo=False)
            self.medidor_sis.poner(SILENCIO_DBFS, activo=False)
        self._actualizar_botones()

    def _actualizar_grabacion(self) -> None:
        if self.estado != GRABANDO:
            return
        g = self.grabadora
        t = g.transcurrido
        for medidor, pista in ((self.medidor_mic, g.mic), (self.medidor_sis, g.sistema)):
            mudo = t > ESPERA_SIN_SENAL and pista.estado.pico_max_dbfs < NIVEL_MINIMO
            medidor.poner(pista.estado.pico_dbfs, sin_senal=mudo)
        if self._pasos % (500 // INTERVALO_MS) == 0:
            # El punto parpadea: se ve que la aplicación sigue viva.
            punto = "●" if self._pasos % (1000 // INTERVALO_MS) == 0 else "○"
            hora = merger.hms(t)
            self.reloj.configure(text=f"{punto} {hora}", text_color=ROJO)
            self.title(f"● {hora} GRABANDO")
            if self.indicador is not None:
                self.indicador.poner(f"{punto} GRABANDO {hora}")
                # Otra ventana siempre visible podría haberlo tapado.
                self.indicador.lift()
            for p in g.problemas():
                self._avisar(p)

    def _avisar(self, texto: str) -> None:
        """Muestra un aviso sin bloquear la grabación."""
        if texto in self._avisados:
            return
        self._avisados.add(texto)
        self.bell()
        self.aviso.configure(text="\n".join(sorted(self._avisados)))
        self.aviso.grid(pady=(0, 10))

    def _quitar_aviso(self) -> None:
        self._avisados.clear()
        self.aviso.grid_remove()

    # ----------------------------------------------------------------------
    # Lista y detalle
    # ----------------------------------------------------------------------

    def refrescar(self, seleccionar: str | None = None) -> None:
        try:
            reuniones = flujo.listar(Path(self.cfg.carpeta_reuniones))
        except flujo.ERRORES as e:
            self.mostrar_error(e)
            return
        previa = seleccionar or next(iter(self.lista.selection()), None)
        self.reuniones = {r.carpeta.name: r for r in reuniones}
        self.lista.delete(*self.lista.get_children())
        for nombre, r in self.reuniones.items():
            activa = r.carpeta == self.carpeta_activa
            self.lista.insert("", "end", iid=nombre, values=(
                f"{r.inicio:%d/%m/%Y %H:%M}" if r.inicio else "",
                r.titulo, "" if activa else merger.hms(r.duracion),
                "Grabando" if activa else
                "Procesando" if r.carpeta == self.procesando else r.estado,
            ))
        if previa in self.reuniones:
            self.lista.selection_set(previa)
            self.lista.see(previa)
        elif self.reuniones:
            self.lista.selection_set(next(iter(self.reuniones)))
        self._mostrar_seleccion()

    def seleccion(self) -> flujo.Reunion | None:
        return self.reuniones.get(next(iter(self.lista.selection()), ""))

    def _mostrar_seleccion(self) -> None:
        r = self.seleccion()
        self._actualizar_botones()
        # Si nada cambió no se reescribe, para no perder la posición de lectura.
        clave = None
        if r is not None:
            try:
                fecha = (r.carpeta / "transcripcion.json").stat().st_mtime_ns
            except OSError:
                fecha = 0
            clave = (r.carpeta, r.estado, r.carpeta == self.carpeta_activa, fecha)
            if clave == self._mostrada:
                return
        self._mostrada = clave
        self.texto.configure(state="normal")
        self.texto.delete("1.0", "end")
        if r is None:
            self.texto.insert("end", "Todavía no hay grabaciones. Pulsa Grabar para empezar.")
        elif r.estado != flujo.TRANSCRITA:
            mensaje = {flujo.GRABADA: "Pulsa Transcribir para obtener el texto.",
                       flujo.INTERRUMPIDA: "La grabación no se cerró bien, pero el audio grabado "
                                           "hasta ese momento se puede transcribir."}[r.estado]
            self.texto.insert(
                "end", "Grabando…" if r.carpeta == self.carpeta_activa else mensaje)
        else:
            try:
                t = merger.cargar(r.carpeta)
            except merger.ErrorUnion as e:
                self.texto.insert("end", str(e))
            else:
                for a in t.avisos:
                    self.texto.insert("end", f"AVISO: {a}\n\n", "hora")
                if not t.intervenciones:
                    self.texto.insert("end", "No se detectó voz en la grabación.")
                for i in t.intervenciones:
                    self.texto.insert("end", f"[{merger.hms(i.inicio)}] ", "hora")
                    self.texto.insert("end", f"{t.hablantes[i.hablante]}: ", "nombre")
                    self.texto.insert("end", f"{i.texto}\n\n")
        self.texto.configure(state="disabled")

    def _actualizar_botones(self) -> None:
        def poner(boton, activo) -> None:
            boton.configure(state="normal" if activo else "disabled")

        r = self.seleccion()
        ocupada = r is None or r.carpeta in (self.carpeta_activa, self.procesando)
        transcrita = not ocupada and r.estado == flujo.TRANSCRITA
        # Empezar a transcribir mientras se graba podría provocar cortes de audio.
        poner(self.boton_procesar,
              not ocupada and self.procesando is None and self.estado == PARADO)
        poner(self.boton_nombres, transcrita)
        poner(self.boton_exportar, transcrita)
        poner(self.boton_carpeta, r is not None)
        poner(self.boton_ajustes, self.estado == PARADO)
        poner(self.boton_cancelar, self.procesando is not None and not self.cancelar.is_set())
        self.boton_procesar.configure(text="Repetir" if transcrita else "Transcribir")

    def abrir_carpeta(self) -> None:
        r = self.seleccion()
        if r is not None:
            os.startfile(r.carpeta)     # abre el Explorador

    # ----------------------------------------------------------------------
    # Transcripción
    # ----------------------------------------------------------------------

    def procesar_seleccion(self) -> None:
        r = self.seleccion()
        if r is None:
            return
        # Un modelo elegido a mano obliga a transcribir de nuevo.
        repetir = self.cfg.modelo != "auto"
        if r.estado == flujo.TRANSCRITA:
            respuesta = messagebox.askyesnocancel(
                NOMBRE_APP,
                "Esta reunión ya está transcrita. ¿Repetir también la transcripción?\n\n"
                "Sí: transcribe de nuevo con el modelo elegido.\n"
                "No: solo vuelve a separar a los hablantes; es más rápido.", parent=self)
            if respuesta is None:
                return
            repetir = respuesta
        p = self.personas.get()
        self.procesar(r.carpeta, None if p == AUTOMATICO else int(p), repetir)

    def procesar(self, carpeta: Path, personas: int | None = None, repetir: bool = False) -> None:
        if self.procesando is not None:
            self.linea.configure(
                text="Hay otra transcripción en curso. Pulsa Transcribir cuando termine.")
            return
        cfg = replace(self.cfg)
        self.procesando = carpeta
        self.cancelar.clear()
        self.progreso.set(0)
        self.progreso.grid()
        self.linea.configure(text="Preparando…")
        self.refrescar()
        self._actualizar_botones()

        def al_estado(texto: str) -> None:
            self.eventos.put(lambda: self._mostrar_avance(texto, 0.0))

        def al_progreso(p: float) -> None:
            self.eventos.put(lambda: self._mostrar_avance(None, p))

        def tarea():
            windows.impedir_suspension(True)
            try:
                return flujo.procesar(
                    carpeta, cfg, personas, repetir, al_estado, al_progreso, self.cancelar)
            finally:
                windows.impedir_suspension(False)

        self.en_hilo(self.trabajo, tarea, self._procesada, self._no_procesada)

    def _mostrar_avance(self, texto: str | None, p: float) -> None:
        # Tras cancelar pueden quedar avances pendientes en la cola.
        if self.procesando is None or self.cancelar.is_set():
            return
        if texto is not None:
            self.linea.configure(text=texto)
        self.progreso.set(p)

    def _fin_proceso(self, texto: str) -> None:
        nombre = self.procesando.name if self.procesando else None
        self.procesando = None
        self.cancelar.clear()
        self.progreso.grid_remove()
        self.linea.configure(text=texto)
        # Si se está grabando no se cambia la reunión que se está viendo.
        self.refrescar(nombre if self.estado == PARADO else None)
        self._actualizar_botones()

    def _procesada(self, p: flujo.Procesado) -> None:
        self._fin_proceso(f"Listo: {p.transcripcion.titulo}. Archivos .txt, .docx y .srt creados.")
        self._avisar_copias(p.exportacion)
        # Los avisos de la transcripción ya se ven en el texto.
        nuevos = [a for a in p.avisos if a not in p.transcripcion.avisos]
        if p.incompleto:
            nuevos.append("No se pudo separar a los hablantes. El motivo está en el texto.")
        if nuevos:
            messagebox.showwarning(NOMBRE_APP, "\n\n".join(nuevos), parent=self)

    def _no_procesada(self, e: BaseException) -> None:
        if isinstance(e, proceso.Cancelado):
            self._fin_proceso("Transcripción cancelada. El audio se conserva.")
            return
        self._fin_proceso("La transcripción no terminó.")
        self.mostrar_error(e)

    def cancelar_proceso(self) -> None:
        if self.procesando is not None:
            self.cancelar.set()
            self.linea.configure(text="Cancelando…")
            self._actualizar_botones()

    # ----------------------------------------------------------------------
    # Nombres y exportación
    # ----------------------------------------------------------------------

    def abrir_nombres(self) -> None:
        r = self.seleccion()
        if r is None:
            return
        try:
            t = merger.cargar(r.carpeta)
        except merger.ErrorUnion as e:
            self.mostrar_error(e)
            return
        DialogoNombres(self, t, lambda cambios: self.renombrar(r.carpeta, cambios))

    def renombrar(self, carpeta: Path, cambios: dict[str, str]) -> None:
        def terminar(resultado) -> None:
            exportacion, avisos = resultado
            self.linea.configure(text="Nombres guardados y archivos actualizados.")
            self.refrescar()
            self._avisar_copias(exportacion)
            if avisos:
                messagebox.showinfo(NOMBRE_APP, "\n".join(avisos), parent=self)

        def fallar(e: BaseException) -> None:
            self.refrescar()    # los nombres pueden haberse guardado
            self.mostrar_error(e)

        self.en_hilo(self.varios, lambda: exporter.renombrar(carpeta, cambios), terminar, fallar)

    def exportar(self) -> None:
        r = self.seleccion()
        if r is None:
            return

        def terminar(e: exporter.Exportacion) -> None:
            self.linea.configure(text="Exportado: " + ", ".join(a.name for a in e.archivos))
            self._avisar_copias(e)

        self.en_hilo(
            self.varios, lambda: exporter.exportar(merger.cargar(r.carpeta), r.carpeta), terminar)

    def _avisar_copias(self, e: exporter.Exportacion) -> None:
        if e.copias:
            messagebox.showinfo(
                NOMBRE_APP, "Habías editado estos archivos; tu versión se conserva en:\n"
                + "\n".join(c.name for c in e.copias), parent=self)

    # ----------------------------------------------------------------------
    # Ajustes
    # ----------------------------------------------------------------------

    def _poner_modelos(self) -> None:
        disponibles = transcriber.modelos_disponibles()
        # Si el configurado ya no está, se muestra: transcribir dirá qué falta.
        if self.cfg.modelo != "auto" and self.cfg.modelo not in disponibles:
            disponibles.append(self.cfg.modelo)
        self.modelo.configure(values=[AUTOMATICO, *disponibles])
        self.modelo.set(AUTOMATICO if self.cfg.modelo == "auto" else self.cfg.modelo)

    def _guardar_modelo(self) -> None:
        m = self.modelo.get()
        self.cfg = replace(self.cfg, modelo="auto" if m == AUTOMATICO else m)
        self._guardar_config()

    def _guardar_auto(self) -> None:
        self.cfg = replace(self.cfg, transcribir_al_terminar=bool(self.auto.get()))
        self._guardar_config()

    def _guardar_config(self) -> bool:
        try:
            config.guardar(self.cfg, self.ruta_config)
            return True
        except OSError as e:
            log.exception("No se pudo guardar la configuración")
            messagebox.showerror(NOMBRE_APP, f"No se pudo guardar {self.ruta_config}: {e}", parent=self)
            return False

    def abrir_ajustes(self) -> None:
        self.boton_ajustes.configure(state="disabled")

        def consultar():
            # En el hilo de audio: no puede coincidir con una grabación.
            import pyaudiowpatch as pyaudio

            pa = pyaudio.PyAudio()
            try:
                micros, salidas = listar_dispositivos(pa)
            finally:
                pa.terminate()
            return [d["name"] for d in micros], [
                d["name"].removesuffix(" [Loopback]") for d in salidas]

        def abrir(listas) -> None:
            self._actualizar_botones()
            if self.estado == PARADO:
                DialogoAjustes(self, *listas)

        def fallar(e: BaseException) -> None:
            self._actualizar_botones()
            self.mostrar_error(e)

        self.en_hilo(self.audio, consultar, abrir, fallar)

    def aplicar_ajustes(self, nueva: config.Config, padre: tk.Misc) -> bool:
        if self.atajo is None or nueva.atajo != self.cfg.atajo:
            try:
                nuevo = windows.Atajo(nueva.atajo, lambda: self.eventos.put(self.alternar))
                # Se prueba el nuevo antes de soltar el anterior.
                nuevo.iniciar()
            except windows.ErrorAtajo as e:
                self.mostrar_error(e, padre)
                return False
            if self.atajo is not None:
                self.atajo.detener()
            self.atajo = nuevo
        # El modelo y la casilla pueden haber cambiado con el diálogo abierto.
        self.cfg = replace(nueva, modelo=self.cfg.modelo,
                           transcribir_al_terminar=self.cfg.transcribir_al_terminar)
        self._texto_atajo()
        return self._guardar_config()

    def _activar_atajo(self) -> None:
        try:
            atajo = windows.Atajo(self.cfg.atajo, lambda: self.eventos.put(self.alternar))
            atajo.iniciar()
        except windows.ErrorAtajo as e:
            # La aplicación sirve igual con el botón.
            log.warning("%s", e)
            self.linea.configure(text=str(e))
            return
        self.atajo = atajo
        self._texto_atajo()

    def _texto_atajo(self) -> None:
        self.linea.configure(text=f"Atajo para grabar y detener: {self.cfg.atajo.title()}")

    # ----------------------------------------------------------------------
    # Cierre
    # ----------------------------------------------------------------------

    def salir(self) -> None:
        if self._cerrando or self.estado in (INICIANDO, DETENIENDO):
            return
        if self.estado == GRABANDO and not messagebox.askokcancel(
            NOMBRE_APP, "Se está grabando. ¿Detener, guardar y salir?", parent=self):
            return
        if self.procesando is not None and not messagebox.askokcancel(
            NOMBRE_APP, "Hay una transcripción en curso. ¿Cancelarla y salir? El audio se conserva.",
            parent=self):
            return
        self._cerrando = True
        self.cancelar_proceso()
        # La grabación puede haber terminado mientras se respondía.
        if self.estado == GRABANDO:
            self.detener(al_terminar=self._esperar_cierre)
        else:
            self._esperar_cierre()

    def _esperar_cierre(self) -> None:
        if self.procesando is not None:
            self.after(100, self._esperar_cierre)
            return
        if self.atajo is not None:
            self.atajo.detener()
        for e in (self.audio, self.trabajo, self.varios):
            e.shutdown(wait=True)       # deja terminar una exportación a medias
        self.after_cancel(self._tarea)
        self.after_cancel(self._tarea_limpieza)
        self.destroy()


def main() -> int:
    config.configurar_logging()
    threading.excepthook = lambda a: log.error(
        "Error en el hilo %s", a.thread.name if a.thread else "?",
        exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    instancia = windows.InstanciaUnica()
    try:
        if not instancia.es_la_primera:
            raise RuntimeError("La aplicación ya está abierta.")
        cfg = config.cargar()
    except (RuntimeError, OSError, TypeError) as e:
        log.error("No se pudo abrir la aplicación: %s", e)
        raiz = tk.Tk()
        raiz.withdraw()
        messagebox.showerror(NOMBRE_APP, str(e), parent=raiz)
        raiz.destroy()
        return 1
    ctk.set_appearance_mode("system")
    ctk.set_default_color_theme("blue")
    try:
        Aplicacion(cfg).mainloop()
    finally:
        instancia.cerrar()
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
