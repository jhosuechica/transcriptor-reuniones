"""Panel de la transcripción: leer, escuchar y corregir.

Cada corrección se guarda al momento en transcripcion.json. Los archivos
exportados se regeneran poco después, para no hacerlo en cada cambio.
"""

from __future__ import annotations

import bisect
import copy
import logging
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import customtkinter as ctk

import exporter
import merger
from componentes import GRIS, ROJO, ROJO_OSCURO, Dialogo
from merger import Transcripcion, hms
from reproductor import Sesion

if TYPE_CHECKING:
    from gui import Aplicacion

log = logging.getLogger(__name__)

# Un color por persona, para fondo claro y oscuro.
COLORES = (
    ("#1f6aa5", "#5aa9e6"), ("#b3541e", "#f0a35e"), ("#2e7d4f", "#6fcf97"),
    ("#8e44ad", "#c792ea"), ("#b03060", "#f28fad"), ("#7a6000", "#e0c341"),
    ("#00796b", "#4dd0c1"), ("#5d4037", "#c9a797"),
)
FONDO_ACTUAL = ("#cfe3f5", "#24455f")
SALTO_S = 5.0
ESPERA_EXPORTAR_MS = 2000
MAX_HISTORIAL = 50


class DialogoCorregir(Dialogo):
    """Corrige una frase y permite pasar a las siguientes sin cerrarlo."""

    def __init__(self, visor: "Visor", n: int):
        super().__init__(visor.app, "Corregir", 600, 360)
        self.visor = visor
        self.carpeta = visor.carpeta
        self.n = n
        fila = ctk.CTkFrame(self, fg_color="transparent")
        fila.pack(padx=16, pady=(14, 6), fill="x")
        self.hora = ctk.CTkLabel(fila, text="")
        self.hora.pack(side="left")
        self.boton_oir = ctk.CTkButton(fila, text="▶  Oír la frase", width=120, command=self.oir)
        self.boton_oir.pack(side="right")
        # Se puede elegir un nombre de la lista o escribir uno nuevo.
        self.quien = ctk.CTkComboBox(self, values=[])
        self.quien.pack(padx=16, fill="x")
        self.toda = ctk.CTkCheckBox(self, text="Cambiar de hablante toda la intervención")
        self.toda.pack(padx=16, pady=8, anchor="w")
        self.texto = ctk.CTkTextbox(self, wrap="word", font=ctk.CTkFont(size=14), height=110)
        self.texto.pack(padx=16, fill="both", expand=True)
        botones = ctk.CTkFrame(self, fg_color="transparent")
        botones.pack(padx=16, pady=14, fill="x")
        self.boton_anterior = ctk.CTkButton(
            botones, text="◀", width=40, command=lambda: self.mover(-1))
        self.boton_siguiente = ctk.CTkButton(
            botones, text="Guardar y siguiente  ▶", command=lambda: self.mover(1))
        self.boton_anterior.pack(side="left")
        self.boton_siguiente.pack(side="left", padx=8)
        ctk.CTkButton(botones, text="Borrar frase", width=110, fg_color=ROJO,
                      hover_color=ROJO_OSCURO, command=self.borrar).pack(side="left")
        ctk.CTkButton(botones, text="Guardar", width=90, command=self.aceptar).pack(side="right")
        ctk.CTkButton(botones, text="Cancelar", width=90, fg_color=GRIS, command=self.cerrar).pack(
            side="right", padx=8)
        self.texto.bind("<Control-Return>", self._siguiente_con_teclado)
        self._cargar()

    def foco_inicial(self) -> None:
        self.texto.focus_set()

    def cerrar(self) -> None:
        self.visor.parar()
        super().cerrar()

    def _siguiente_con_teclado(self, _e) -> str:
        self.mover(1)
        return "break"      # sin insertar el salto de línea

    def _cargar(self) -> None:
        lista = merger.frases(self.visor.transcripcion)
        ident, frase = lista[self.n]
        self._ident, self._frase = ident, frase
        self._opciones = self.visor.nombres_unicos()
        self.quien.configure(values=list(self._opciones))
        self._inicial = next(k for k, v in self._opciones.items() if v == ident)
        self.quien.set(self._inicial)
        self.toda.deselect()
        self.hora.configure(text=f"Frase {self.n + 1} de {len(lista)}  ·  {hms(frase.inicio)}")
        self.texto.delete("1.0", "end")
        self.texto.insert("1.0", frase.texto)
        self.boton_anterior.configure(state="normal" if self.n > 0 else "disabled")
        self.boton_siguiente.configure(
            state="normal" if self.n + 1 < len(lista) else "disabled")
        self.visor.elegir(self.n)

    def _vigente(self) -> bool:
        if self.visor.carpeta == self.carpeta and self.visor.transcripcion is not None:
            return True
        self.visor.app.mostrar_error(merger.ErrorUnion(
            "La reunión que se corregía ya no está abierta. Copia tu texto antes de cerrar."), self)
        return False

    def oir(self) -> None:
        # Un poco de margen: los tiempos de las palabras no son exactos.
        self.visor.reproducir(max(0.0, self._frase.inicio - 0.2), self._frase.fin + 0.3)

    def guardar(self) -> bool:
        """Guarda si hay cambios. Devuelve False si no se pudo."""
        if not self._vigente():
            return False
        texto = " ".join(self.texto.get("1.0", "end").split())
        elegido = self.quien.get().strip()
        nuevo_texto = texto if texto != self._frase.texto else None
        # También vale escribir un nombre que ya existe.
        ident = self._opciones.get(elegido) or next(
            (v for k, v in self._opciones.items() if k.casefold() == elegido.casefold()), None)
        if nuevo_texto is None and ident == self._ident:
            return True
        n, toda = self.n, bool(self.toda.get())

        def cambio(t: Transcripcion) -> None:
            quien = ident or merger.agregar_hablante(t, elegido)
            merger.corregir(t, n, nuevo_texto, quien, toda)

        return self.visor.cambiar(cambio, self)

    def aceptar(self) -> None:
        if self.guardar():
            self.cerrar()

    def mover(self, paso: int) -> None:
        if not self._vigente():
            return
        total = len(merger.frases(self.visor.transcripcion))
        if not 0 <= self.n + paso < total:
            if paso > 0:
                self.aceptar()      # era la última
            return
        if self.guardar():
            self.visor.parar()
            self.n += paso
            self._cargar()

    def borrar(self) -> None:
        n = self.n
        if not self._vigente() or not self.visor.cambiar(lambda t: merger.borrar(t, n), self):
            return
        self.visor.parar()
        total = len(merger.frases(self.visor.transcripcion))
        if not total:
            self.cerrar()
            return
        self.n = min(n, total - 1)
        self._cargar()


class Visor(ctk.CTkFrame):
    def __init__(self, padre, app: "Aplicacion"):
        super().__init__(padre, fg_color="transparent")
        self.app = app
        self.carpeta: Path | None = None
        self.transcripcion: Transcripcion | None = None
        self.sesion: Sesion | None = None
        self.posicion = 0.0
        self.duracion = 0.0
        self.actual: int | None = None      # frase resaltada
        self._fecha = 0
        self._historial: list[Transcripcion] = []
        self._inicios: list[float] = []     # segundo en que empieza cada frase
        self._lugares: list[tuple] = []     # (línea, columna) donde empieza y acaba
        self._arrastrando = False
        self._escuchar = self._editar = False
        self._tarea_exportar: str | None = None
        self._por_exportar: tuple[Path, Transcripcion] | None = None
        self._construir()

    def _construir(self) -> None:
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.barra = ctk.CTkFrame(self, fg_color="transparent")
        self.barra.grid(row=0, column=0, sticky="ew", pady=(2, 6))
        self.barra.grid_columnconfigure(3, weight=1)
        self.boton_oir = ctk.CTkButton(self.barra, text="▶", width=44, command=self.alternar)
        self.boton_atras = ctk.CTkButton(
            self.barra, text="−5 s", width=48, fg_color=GRIS, command=lambda: self.mover(-SALTO_S))
        self.boton_adelante = ctk.CTkButton(
            self.barra, text="+5 s", width=48, fg_color=GRIS, command=lambda: self.mover(SALTO_S))
        self.deslizador = ctk.CTkSlider(
            self.barra, from_=0, to=1, command=self._arrastrar, state="disabled")
        self.deslizador.set(0)
        # Se salta al soltar: hacerlo en cada movimiento sonaría entrecortado.
        self.deslizador.bind("<ButtonRelease-1>", self._soltar, add=True)
        self.tiempo = ctk.CTkLabel(self.barra, text="", width=130)
        self.boton_corregir = ctk.CTkButton(
            self.barra, text="Corregir", width=80, command=self.corregir)
        self.boton_deshacer = ctk.CTkButton(
            self.barra, text="Deshacer", width=80, fg_color=GRIS, command=self.deshacer)
        for col, w in enumerate((self.boton_oir, self.boton_atras, self.boton_adelante,
                                 self.deslizador, self.tiempo, self.boton_corregir,
                                 self.boton_deshacer)):
            w.grid(row=0, column=col, padx=(0, 6), sticky="ew" if w is self.deslizador else "")
        self.texto = ctk.CTkTextbox(self, wrap="word", font=ctk.CTkFont(size=13), cursor="arrow")
        self.texto.grid(row=1, column=0, sticky="nsew")
        self.texto.tag_config("hora", foreground=GRIS)
        self.texto.tag_config("cabecera", spacing1=10, spacing3=2)
        self.texto.tag_config("cuerpo", lmargin1=14, lmargin2=14, spacing2=2)
        self.aplicar_colores()
        self.texto.configure(state="disabled")
        self.texto.bind("<ButtonRelease-1>", self._clic)
        self.texto.bind("<Double-Button-1>", self._doble_clic)
        self.texto.bind("<Button-3>", self._menu)
        self.texto.bind("<space>", lambda _e: self._tecla(self.alternar))
        self.texto.bind("<F2>", lambda _e: self._tecla(self.corregir))
        self.texto.bind("<Control-z>", lambda _e: self._tecla(self.deshacer))
        self.texto.bind("<Left>", lambda _e: self._tecla(lambda: self.mover(-SALTO_S)))
        self.texto.bind("<Right>", lambda _e: self._tecla(lambda: self.mover(SALTO_S)))

    @staticmethod
    def _tecla(funcion: Callable[[], None]) -> str:
        funcion()
        return "break"

    def aplicar_colores(self) -> None:
        oscuro = int(ctk.get_appearance_mode() == "Dark")
        for n, par in enumerate(COLORES):
            self.texto.tag_config(f"persona{n}", foreground=par[oscuro])
        self.texto.tag_config("actual", background=FONDO_ACTUAL[oscuro])

    # ----------------------------------------------------------------------
    # Contenido
    # ----------------------------------------------------------------------

    def _vaciar(self) -> None:
        self.parar()
        self.exportar_pendiente()
        self.carpeta = self.transcripcion = None
        self.posicion = 0.0
        self.actual = None
        self._fecha = 0
        self._historial.clear()

    def _escribir(self, texto: str) -> None:
        self._inicios, self._lugares = [], []
        self.texto.configure(state="normal")
        self.texto.delete("1.0", "end")
        self.texto.insert("end", texto)
        self.texto.configure(state="disabled")

    def mensaje(self, texto: str) -> None:
        self._vaciar()
        self._escribir(texto)
        self._actualizar()

    def mostrar(self, carpeta: Path, duracion: float) -> None:
        fecha = self._fecha_en_disco(carpeta)
        if (carpeta, fecha) == (self.carpeta, self._fecha):
            return      # ya se muestra: conserva la posición y el historial
        self._vaciar()
        try:
            t = merger.cargar(carpeta)
        except merger.ErrorUnion as e:
            self._escribir(str(e))
            self._actualizar()
            return
        self.carpeta, self.transcripcion = carpeta, t
        self._fecha = fecha
        self.duracion = max([duracion, *(f.fin for _, f in merger.frases(t))])
        self._dibujar()
        self.texto.yview_moveto(0)

    @staticmethod
    def _fecha_en_disco(carpeta: Path) -> int:
        try:
            return (carpeta / "transcripcion.json").stat().st_mtime_ns
        except OSError:
            return 0

    def _dibujar(self) -> None:
        t = self.transcripcion
        w = self.texto
        # Dos hablantes con el mismo nombre son la misma persona: mismo color.
        orden = list(dict.fromkeys(t.hablantes.values()))
        self._inicios, self._lugares = [], []
        w.configure(state="normal")
        w.delete("1.0", "end")
        for a in t.avisos:
            w.insert("end", f"AVISO: {a}\n", "hora")
        if not t.intervenciones:
            w.insert("end", "No se detectó voz en la grabación.")
        for i in t.intervenciones:
            nombre = t.hablantes[i.hablante]
            color = f"persona{orden.index(nombre) % len(COLORES)}"
            w.insert("end", nombre, ("cabecera", color))
            w.insert("end", f"   {hms(i.inicio)}\n", ("cabecera", "hora"))
            for k, f in enumerate(i.frases):
                if k:
                    w.insert("end", " ", "cuerpo")
                inicio = w.index("end-1c")
                w.insert("end", f.texto, "cuerpo")
                self._inicios.append(f.inicio)
                self._lugares.append((self._par(inicio), self._par(w.index("end-1c"))))
            w.insert("end", "\n", "cuerpo")
        w.configure(state="disabled")
        self._resaltar(self.actual if self.actual is not None
                       and self.actual < len(self._inicios) else None, forzar=True)
        self._actualizar()

    @staticmethod
    def _par(indice: str) -> tuple[int, int]:
        linea, columna = indice.split(".")
        return int(linea), int(columna)

    def nombres_unicos(self) -> dict[str, str]:
        """Nombre que se muestra -> identificador, sin repetir ninguno."""
        h = self.transcripcion.hablantes
        nombres = list(h.values())
        return {f"{n} ({i})" if nombres.count(n) > 1 else n: i for i, n in h.items()}

    def _resaltar(self, n: int | None, ver: bool = False, forzar: bool = False) -> None:
        if n == self.actual and not forzar:
            return
        self.actual = n
        self.texto.tag_remove("actual", "1.0", "end")
        if n is not None:
            (l1, c1), (l2, c2) = self._lugares[n]
            self.texto.tag_add("actual", f"{l1}.{c1}", f"{l2}.{c2}")
            if ver:
                self.texto.see(f"{l2}.{c2}")
                self.texto.see(f"{l1}.{c1}")

    def elegir(self, n: int) -> None:
        self._resaltar(n, ver=True)
        self.posicion = self._inicios[n]
        self._actualizar_tiempo()

    # ----------------------------------------------------------------------
    # Reproducción
    # ----------------------------------------------------------------------

    def activar(self, escuchar: bool, editar: bool) -> None:
        """Qué permite la aplicación ahora: al grabar, por ejemplo, no se escucha."""
        self._escuchar, self._editar = escuchar, editar
        self._actualizar()

    def _actualizar(self) -> None:
        def poner(boton, activo) -> None:
            boton.configure(state="normal" if activo else "disabled")

        hay = self.transcripcion is not None
        oir = hay and self._escuchar
        for b in (self.boton_oir, self.boton_atras, self.boton_adelante, self.deslizador):
            poner(b, oir)
        poner(self.boton_corregir, hay and self._editar and bool(self._inicios))
        poner(self.boton_deshacer, hay and self._editar and bool(self._historial))
        self.boton_oir.configure(text="❚❚" if self.sesion is not None else "▶")
        self._actualizar_tiempo()

    def _actualizar_tiempo(self) -> None:
        if self.transcripcion is None:
            self.tiempo.configure(text="")
            self.deslizador.set(0)
            return
        if not self._arrastrando:
            self.tiempo.configure(text=f"{hms(self.posicion)} / {hms(self.duracion)}")
            self.deslizador.set(min(1.0, self.posicion / self.duracion) if self.duracion else 0)

    def reproducir(self, desde: float, hasta: float | None = None) -> None:
        if self.transcripcion is None or not self._escuchar:
            return
        self.parar()
        self.app.parar_prueba()     # ocupa el mismo hilo de audio
        sesion = self.sesion = Sesion(self.carpeta, desde, hasta)
        self.posicion = desde

        def terminar() -> None:
            # Puede haber empezado otra mientras esta se cerraba.
            if self.sesion is sesion:
                self.posicion = sesion.posicion
                self.sesion = None
                self._actualizar()

        def fallar(e: BaseException) -> None:
            terminar()
            self.app.mostrar_error(e)

        self.app.en_hilo(self.app.audio, sesion.ejecutar, lambda _r: terminar(), fallar)
        self._actualizar()

    def parar(self) -> None:
        if self.sesion is not None:
            self.posicion = self.sesion.posicion
            self.sesion.parar()
            self.sesion = None
            self._actualizar()

    def alternar(self) -> None:
        if self.sesion is not None:
            self.parar()
        else:
            # Al final del todo se empieza de nuevo.
            self.reproducir(0.0 if self.posicion >= self.duracion - 0.1 else self.posicion)

    def mover(self, segundos: float) -> None:
        if self.transcripcion is not None and self._escuchar:
            self.ir(self.posicion + segundos)

    def ir(self, t: float) -> None:
        self.posicion = max(0.0, min(t, self.duracion))
        if self.sesion is not None:
            self.sesion.saltar(self.posicion)
        self._seguir(ver=True)
        self._actualizar_tiempo()

    def _arrastrar(self, valor: float) -> None:
        self._arrastrando = True
        self.tiempo.configure(text=f"{hms(valor * self.duracion)} / {hms(self.duracion)}")

    def _soltar(self, _e) -> None:
        if self._arrastrando:
            self._arrastrando = False
            self.ir(self.deslizador.get() * self.duracion)

    def _seguir(self, ver: bool) -> None:
        n = bisect.bisect_right(self._inicios, self.posicion + 0.01) - 1
        self._resaltar(n if n >= 0 else None, ver)

    def paso(self) -> None:
        """Se llama varias veces por segundo desde la ventana."""
        if self.sesion is None:
            return
        self.posicion = self.sesion.posicion
        self._seguir(ver=True)
        self._actualizar_tiempo()

    # ----------------------------------------------------------------------
    # Ratón
    # ----------------------------------------------------------------------

    def _frase_en(self, e) -> int | None:
        if not self._lugares:
            return None
        punto = self._par(self.texto.index(f"@{e.x},{e.y}"))
        n = bisect.bisect_right([a for a, _ in self._lugares], punto) - 1
        # En la cabecera de una intervención, su primera frase.
        if n + 1 < len(self._lugares) and self._lugares[n + 1][0][0] == punto[0] + 1:
            return n + 1
        return n if n >= 0 and self._lugares[n][0][0] == punto[0] else None

    def _clic(self, e) -> None:
        self.texto.focus_set()
        # Arrastrar sirve para copiar texto.
        n = self._frase_en(e)
        if n is None or self.texto.tag_ranges("sel"):
            return
        self._resaltar(n)
        self.ir(self._inicios[n])

    def _doble_clic(self, e) -> str:
        n = self._frase_en(e)
        if n is not None:
            self._resaltar(n)
            self.reproducir(self._inicios[n])
        return "break"      # sin seleccionar la palabra

    def _menu(self, e) -> None:
        n = self._frase_en(e)
        if n is None:
            return
        self.elegir(n)
        _, frase = merger.frases(self.transcripcion)[n]
        oir = "normal" if self._escuchar else "disabled"
        editar = "normal" if self._editar else "disabled"
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label="Escuchar desde aquí", state=oir,
                         command=lambda: self.reproducir(frase.inicio))
        menu.add_command(label="Escuchar solo esta frase", state=oir,
                         command=lambda: self.reproducir(frase.inicio, frase.fin + 0.3))
        menu.add_separator()
        menu.add_command(label="Corregir…", state=editar, command=self.corregir)
        personas = tk.Menu(menu, tearoff=False)
        for nombre, ident in self.nombres_unicos().items():
            personas.add_command(
                label=nombre, command=lambda i=ident: self.cambiar(
                    lambda t: merger.corregir(t, n, hablante=i)))
        menu.add_cascade(label="Esta frase la dijo", menu=personas, state=editar)
        menu.add_command(label="Borrar frase", state=editar,
                         command=lambda: self.cambiar(lambda t: merger.borrar(t, n)))
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()

    # ----------------------------------------------------------------------
    # Correcciones
    # ----------------------------------------------------------------------

    def corregir(self) -> None:
        if self.transcripcion is None or not self._editar or not self._inicios:
            return
        self.parar()
        DialogoCorregir(self, self.actual or 0)

    def cambiar(self, cambio: Callable[[Transcripcion], None], padre=None) -> bool:
        """Aplica un cambio, lo guarda y permite deshacerlo. False si no se pudo."""
        if self.transcripcion is None or not self._editar:
            return False
        nueva = copy.deepcopy(self.transcripcion)
        try:
            if self._fecha_en_disco(self.carpeta) != self._fecha:
                raise merger.ErrorUnion(
                    "La transcripción cambió fuera de la ventana. Elige otra reunión y vuelve "
                    "a esta para ver la versión actual.")
            cambio(nueva)
            merger.guardar(nueva, self.carpeta)
        except merger.ErrorUnion as e:
            self.app.mostrar_error(e, padre)
            return False
        self._historial.append(self.transcripcion)
        del self._historial[:-MAX_HISTORIAL]
        self._poner(nueva)
        return True

    def deshacer(self) -> None:
        if not self._historial or not self._editar:
            return
        if self._fecha_en_disco(self.carpeta) != self._fecha:
            self.app.mostrar_error(merger.ErrorUnion(
                "La transcripción cambió fuera de la ventana: no se puede deshacer."))
            return
        try:
            merger.guardar(self._historial[-1], self.carpeta)
        except merger.ErrorUnion as e:
            self.app.mostrar_error(e)
            return
        self._poner(self._historial.pop())

    def _poner(self, t: Transcripcion) -> None:
        arriba = self.texto.yview()[0]
        self.transcripcion = t
        self._fecha = self._fecha_en_disco(self.carpeta)
        self._dibujar()
        self.texto.yview_moveto(arriba)
        self._por_exportar = (self.carpeta, t)
        if self._tarea_exportar is not None:
            self.after_cancel(self._tarea_exportar)
        self._tarea_exportar = self.after(ESPERA_EXPORTAR_MS, self.exportar_pendiente)

    # ----------------------------------------------------------------------
    # Exportación
    # ----------------------------------------------------------------------

    def exportar_pendiente(self) -> None:
        if self._tarea_exportar is not None:
            self.after_cancel(self._tarea_exportar)
            self._tarea_exportar = None
        if self._por_exportar is None:
            return
        carpeta, t = self._por_exportar
        self._por_exportar = None

        def fallar(e: BaseException) -> None:
            # Sin cuadro de mensaje: aparecería en cada corrección.
            self.app.linea.configure(
                text="Corrección guardada, pero no se actualizaron todos los archivos. "
                     "Ciérralos y pulsa Exportar.")

        self.app.en_hilo(
            self.app.archivos, lambda: exporter.exportar(t, carpeta),
            lambda r: self._exportado(r, avisar=False), fallar)

    def exportar(self, avisar: bool) -> None:
        if self.transcripcion is None:
            return
        self._por_exportar = None
        if self._tarea_exportar is not None:
            self.after_cancel(self._tarea_exportar)
            self._tarea_exportar = None
        carpeta, t = self.carpeta, self.transcripcion
        self.app.en_hilo(self.app.archivos, lambda: exporter.exportar(t, carpeta),
                         lambda r: self._exportado(r, avisar))

    def _exportado(self, e: exporter.Exportacion, avisar: bool) -> None:
        texto = "Exportado: " + ", ".join(a.name for a in e.archivos)
        if e.copias and not avisar:
            texto += ". Tu versión editada: " + ", ".join(c.name for c in e.copias)
        self.app.linea.configure(text=texto)
        if avisar:
            self.app.avisar_copias(e)
