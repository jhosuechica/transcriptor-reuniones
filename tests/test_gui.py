"""Pruebas de la ventana, el atajo y el flujo. No abren ningún dispositivo de audio."""

import gc
import json
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cli
import config
import diarizer
import exporter
import flujo
import gui
import merger
import proceso
import transcriber
import windows
from merger import Frase, Intervencion, Transcripcion
from recorder import ErrorGrabacion
from transcriber import Palabra, Resultado, Segmento

# Combinación improbable, para no estorbar al usuario durante las pruebas.
ATAJO = "ctrl+alt+shift+f23"


def crear_reunion(raiz: Path, nombre="2026-01-02_10-00_Demo", terminada=True, texto=True) -> Path:
    c = raiz / nombre
    c.mkdir(parents=True)
    for n in ("mic", "sistema"):
        with wave.open(str(c / f"{n}.wav"), "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(48000)
            w.writeframes(b"\0\0" * 48000 * 2)
    (c / "sesion.json").write_text(json.dumps(
        {"titulo": "Demo", "inicio": "2026-01-02T10:00:00-05:00", "terminada": terminada}))
    if texto:
        merger.guardar(Transcripcion(
            "Demo", "2026-01-02T10:00:00-05:00", {"yo": "Yo", "h1": "Hablante 1"},
            [Intervencion("yo", 1, 2, [Frase(1, 2, "Hola.")]),
             Intervencion("h1", 3, 4, [Frase(3, 4, "Buenas.")])]), c)
    return c


class FalsaGrabadora:
    def __init__(self, raiz):
        self.raiz, self.avisos, self.fallo = raiz, [], None
        self.transcurrido, self.lista = 65.0, []
        self.hilos = []
        self.mic = self.sistema = SimpleNamespace(
            estado=SimpleNamespace(pico_dbfs=-20.0, pico_max_dbfs=-10.0))
        self.sesion = {"avisos": []}

    def iniciar(self, titulo):
        self.hilos.append(threading.get_ident())
        if self.fallo:
            raise self.fallo
        self.carpeta = crear_reunion(self.raiz, f"2026-05-05_10-00_{titulo}", False, False)
        return self.carpeta

    def detener(self):
        self.hilos.append(threading.get_ident())
        return self.sesion

    def problemas(self):
        return self.lista


class TestAtajo(unittest.TestCase):
    def test_interpretar(self):
        self.assertEqual(windows.interpretar("Ctrl+Alt+R"), (3, 0x52))
        self.assertEqual(windows.interpretar("alt + shift + f12"), (5, 0x7B))
        for malo in ("", "r", "ctrl", "ctrl+", "ctrl+ñ", "ctrl+alt+f25", "shift+a", "win+r",
                     "ctrl+ctrl+r", "ctrl+f1x"):
            with self.assertRaises(windows.ErrorAtajo, msg=malo):
                windows.interpretar(malo)

    def test_ocupado_y_liberado(self):
        a, b = windows.Atajo(ATAJO, lambda: None), windows.Atajo(ATAJO, lambda: None)
        a.iniciar()
        try:
            with self.assertRaisesRegex(windows.ErrorAtajo, "Otro programa"):
                b.iniciar()
        finally:
            a.detener()
        b.iniciar()
        b.detener()

    def test_avisa_al_pulsar(self):
        pulsado = threading.Event()
        a = windows.Atajo(ATAJO, pulsado.set)
        a.iniciar()
        windows._user32.PostThreadMessageW(a._id_hilo, windows.WM_HOTKEY, 1, 0)
        self.assertTrue(pulsado.wait(2))
        a.detener()

    def test_instancia_unica(self):
        a = windows.InstanciaUnica("Local\\PruebaTranscriptor")
        b = windows.InstanciaUnica("Local\\PruebaTranscriptor")
        self.assertEqual((a.es_la_primera, b.es_la_primera), (True, False))
        a.cerrar(), b.cerrar()
        c = windows.InstanciaUnica("Local\\PruebaTranscriptor")
        self.assertTrue(c.es_la_primera)
        c.cerrar()


class TestFlujo(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.raiz = Path(tmp.name)

    def test_listar(self):
        crear_reunion(self.raiz, "2026-01-01_10-00_a", texto=False)
        crear_reunion(self.raiz, "2026-01-03_10-00_c")
        rota = crear_reunion(self.raiz, "2026-01-02_10-00_b", terminada=False, texto=False)
        (self.raiz / "otra").mkdir()
        (self.raiz / "nota.txt").write_text("x")
        r = flujo.listar(self.raiz)
        self.assertEqual([x.estado for x in r],
                         [flujo.TRANSCRITA, flujo.INTERRUMPIDA, flujo.GRABADA])
        self.assertEqual(r[0].duracion, 2.0)
        (rota / "sesion.json").write_text("{mal")
        (rota / "mic.wav").write_bytes(b"")
        x = flujo.listar(self.raiz)[1]
        self.assertEqual((x.titulo, x.inicio, x.duracion), (rota.name, None, 0.0))

    def test_sin_carpeta(self):
        self.assertEqual(flujo.listar(self.raiz / "nada"), [])

    def test_procesar_conserva_nombres_y_tolera_fallo(self):
        c = crear_reunion(self.raiz, texto=False)
        s = Segmento(1, 3, "Hola a todos", [Palabra(1, 3, " Hola a todos", .9)])
        res = [Resultado("mic.wav", 2, "small", "cpu", "int8", 1, []),
               Resultado("sistema.wav", 2, "small", "cpu", "int8", 1, [s])]
        cfg = config.Config()
        with (mock.patch.object(diarizer, "comprobar"),
              mock.patch.object(transcriber, "transcribir", return_value=res) as tr,
              mock.patch.object(diarizer, "diarizar",
                                return_value=diarizer.Diarizacion([diarizer.Turno(0, 4, "A")])) as d):
            p = flujo.procesar(c, cfg)
            self.assertFalse(p.incompleto)
            self.assertTrue((c / "transcripcion.docx").exists())
            exporter.renombrar(c, {"h1": "Ana"})
            p = flujo.procesar(c, cfg)
            self.assertEqual(tr.call_count, 1)      # reutiliza la transcripción
            self.assertEqual(p.transcripcion.hablantes["h1"], "Ana")
            flujo.procesar(c, cfg, retranscribir=True)
            self.assertEqual(tr.call_count, 2)
            d.side_effect = diarizer.ErrorDiarizacion("x.")
            p = flujo.procesar(c, cfg)
            self.assertTrue(p.incompleto)
            self.assertIn("un solo hablante", p.avisos[0])
            d.side_effect = proceso.Cancelado
            with self.assertRaises(proceso.Cancelado):
                flujo.procesar(c, cfg)


class TestConsola(unittest.TestCase):
    def test_los_errores_se_explican(self):
        # Proceso real: main() cambia la salida estándar.
        with tempfile.TemporaryDirectory() as d:
            p = subprocess.run(
                [sys.executable, str(Path(cli.__file__)), "exportar", d],
                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(p.returncode, 1)
        self.assertIn("ERROR: No existe", p.stderr)
        self.assertNotIn("Traceback", p.stderr)


class TestVentana(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.raiz = Path(tmp.name)
        self.demo = crear_reunion(self.raiz / "R")
        self.cfg = config.Config(carpeta_reuniones=str(self.raiz / "R"), atajo=ATAJO)
        self.grabadora = FalsaGrabadora(self.raiz / "R")
        parche = mock.patch.object(gui, "messagebox")
        self.msg = parche.start()
        self.addCleanup(parche.stop)
        self.app = self.abrir()

    def abrir(self):
        app = gui.Aplicacion(self.cfg, self.grabadora, self.raiz / "config.json")
        self.addCleanup(self.cerrar, app)
        return app

    def cerrar(self, app):
        if not self.cerrada(app):
            app.salir()
            self.bombear(lambda: self.cerrada(app), 10, app)
        # Los restos se recogen en este hilo, antes de abrir otra ventana.
        self.app = None
        gc.unfreeze()
        gc.collect()

    @staticmethod
    def cerrada(app):
        try:
            return not app.winfo_exists()
        except tk.TclError:
            return True

    def bombear(self, condicion, segundos=5, app=None, exigir=True):
        """Deja funcionar la ventana hasta que se cumpla la condición."""
        fin = time.monotonic() + segundos
        while time.monotonic() < fin:
            if condicion():
                return
            (app or self.app).update()
            time.sleep(0.01)
        if exigir:
            self.fail(f"no se cumplió la condición; mensajes: {self.msg.method_calls}")

    def grabar(self, titulo="Sprint"):
        self.app.alternar()
        d = self.app.dialogo_inicio
        self.bombear(d.winfo_viewable)
        d.titulo.insert(0, titulo)
        d.aceptar()
        self.bombear(lambda: self.app.estado in (gui.GRABANDO, gui.PARADO))

    def test_grabar_y_detener(self):
        self.grabar()
        self.assertEqual(self.app.estado, gui.GRABANDO)
        self.bombear(lambda: "GRABANDO" in self.app.title())
        self.assertIn("00:01:05", self.app.reloj.cget("text"))
        self.assertGreater(self.app.medidor_mic.barra.get(), 0.5)
        self.assertTrue(self.app.indicador.winfo_exists())
        fila = self.app.lista.item(self.grabadora.carpeta.name, "values")
        self.assertEqual(fila[3], "Grabando")
        self.assertEqual(self.app.boton_procesar.cget("state"), "disabled")
        self.app.alternar()
        self.bombear(lambda: self.app.estado == gui.PARADO)
        self.assertIsNone(self.app.indicador)
        self.assertEqual(self.app.medidor_mic.barra.get(), 0)
        self.assertEqual(self.app.title(), gui.NOMBRE_APP)
        # Se abre y se cierra en el mismo hilo, distinto del de la ventana.
        self.assertEqual(len(set(self.grabadora.hilos)), 1)
        self.assertNotIn(threading.get_ident(), self.grabadora.hilos)

    def test_sin_consentimiento_no_graba(self):
        self.app.alternar()
        self.bombear(self.app.dialogo_inicio.winfo_viewable)
        d = self.app.dialogo_inicio
        self.app.alternar()         # repetir el atajo no salta el aviso
        self.assertIs(self.app.dialogo_inicio, d)
        self.assertEqual(self.app.estado, gui.PARADO)
        d.cerrar()
        self.bombear(lambda: False, 0.2, exigir=False)
        self.assertEqual(self.grabadora.hilos, [])

    def test_fallo_al_iniciar(self):
        self.grabadora.fallo = ErrorGrabacion("No hay micrófono.")
        with self.assertLogs("gui"):
            self.grabar()
        self.assertEqual(self.app.estado, gui.PARADO)
        self.assertEqual(self.msg.showerror.call_args.args[1], "No hay micrófono.")
        self.assertEqual(self.app.boton_grabar.cget("state"), "normal")

    def test_aviso_durante_la_grabacion(self):
        self.grabar()
        self.grabadora.lista = ["Se perdió el micrófono."]
        self.bombear(lambda: self.app.aviso.winfo_ismapped())
        self.grabadora.mic.estado.pico_max_dbfs = -90
        self.bombear(lambda: self.app.medidor_mic.valor.cget("text") == "Sin señal")
        self.assertEqual(self.app.estado, gui.GRABANDO)     # no se corta sola
        self.msg.showerror.assert_not_called()

    def test_atajo_global(self):
        windows._user32.PostThreadMessageW(
            self.app.atajo._id_hilo, windows.WM_HOTKEY, 1, 0)
        self.bombear(lambda: self.app.dialogo_inicio is not None)
        self.app.dialogo_inicio.cerrar()

    def test_atajo_ocupado_no_impide_abrir(self):
        with self.assertLogs("gui", "WARNING"):
            otra = self.abrir()
        self.assertIsNone(otra.atajo)
        self.assertIn("Otro programa", otra.linea.cget("text"))

    def test_icono(self):
        self.assertTrue(gui.ICONO.exists())
        # Con esta marca, CustomTkinter no pone después el suyo.
        self.assertTrue(self.app._iconbitmap_method_called)

    def test_boton_al_abrir(self):
        self.assertIn("Grabar", self.app.boton_grabar.cget("text"))

    def test_muestra_el_texto(self):
        self.assertIn("Hablante 1   00:00:03\nBuenas.", self.app.texto.get("1.0", "end"))

    def test_transcripcion_danada(self):
        (self.demo / "transcripcion.json").write_text("{")
        self.app.refrescar()
        self.app.update()
        self.assertIn("dañado", self.app.texto.get("1.0", "end"))

    def test_renombrar(self):
        self.app.abrir_nombres()
        d = next(w for w in self.app.winfo_children() if isinstance(w, gui.DialogoNombres))
        self.bombear(d.winfo_viewable)
        d.campos["h1"].delete(0, "end")
        d.campos["h1"].insert(0, "Ana")
        d.campos["yo"].delete(0, "end")     # vacío: se queda como estaba
        d.aceptar()
        self.bombear(lambda: "Ana   00:00:03" in self.app.texto.get("1.0", "end"))
        txt = self.demo / "transcripcion.txt"
        self.bombear(lambda: txt.exists() and "Ana: Buenas." in txt.read_text(encoding="utf-8"))
        self.assertIn("Yo: Hola.", txt.read_text(encoding="utf-8"))

    def test_exportar_con_archivo_abierto(self):
        exporter.exportar(merger.cargar(self.demo), self.demo)
        with open(self.demo / "transcripcion.docx", "rb"), self.assertLogs("gui"):
            self.app.exportar()
            self.bombear(lambda: self.msg.showerror.called)
        self.assertIn("abierto en otro programa", self.msg.showerror.call_args.args[1])

    def test_otro_hilo_no_destruye_ventanas(self):
        # Un diálogo cerrado deja objetos de Tk en ciclos de referencias.
        self.app.abrir_nombres()
        d = next(w for w in self.app.winfo_children() if isinstance(w, gui.DialogoNombres))
        self.bombear(d.winfo_viewable)
        d.cerrar()
        del d
        # Crea muchos objetos para forzar al recolector automático, si estuviera activo.
        hilo = threading.Thread(target=lambda: [[[]] for _ in range(300_000)], daemon=True)
        hilo.start()
        hilo.join(5)
        self.assertFalse(hilo.is_alive())
        self.assertFalse(gc.isenabled())

    def simular_proceso(self, funcion):
        parche = mock.patch.object(flujo, "procesar", side_effect=funcion)
        parche.start()
        self.addCleanup(parche.stop)

    def test_procesar_con_avance(self):
        visto = {}

        def falso(carpeta, cfg, personas, repetir, al_estado, al_progreso, cancelar):
            visto.update(carpeta=carpeta, personas=personas, repetir=repetir, modelo=cfg.modelo)
            al_estado("Transcribiendo")
            al_progreso(0.5)
            time.sleep(0.4)
            t = merger.cargar(carpeta)
            return flujo.Procesado(t, exporter.exportar(t, carpeta), [], 1.0, False)

        self.simular_proceso(falso)
        self.app.personas.set("3")
        self.msg.askyesnocancel.return_value = False
        self.app.procesar_seleccion()
        self.bombear(lambda: self.app.progreso.get() == 0.5)
        self.assertEqual(self.app.boton_nombres.cget("state"), "disabled")
        self.app.procesar(self.demo)        # no se lanza otra a la vez
        self.bombear(lambda: self.app.procesando is None)
        self.assertEqual(visto, {"carpeta": self.demo, "personas": 3, "repetir": False,
                                 "modelo": "auto"})
        self.assertIn("Listo", self.app.linea.cget("text"))
        self.assertEqual(self.app.boton_nombres.cget("state"), "normal")

    def test_cancelar(self):
        def falso(*a):
            a[-1].wait(5)
            raise proceso.Cancelado

        self.simular_proceso(falso)
        self.app.procesar(self.demo)
        self.bombear(lambda: self.app.boton_cancelar.cget("state") == "normal")
        self.app.cancelar_proceso()
        self.bombear(lambda: self.app.procesando is None)
        self.assertIn("cancelada", self.app.linea.cget("text"))
        self.msg.showerror.assert_not_called()

    def test_error_inesperado_se_muestra(self):
        def falso(*_a):
            raise ZeroDivisionError("raro")

        self.simular_proceso(falso)
        with self.assertLogs("gui"):
            self.app.procesar(self.demo)
            self.bombear(lambda: self.msg.showerror.called)
        self.assertIn("app.log", self.msg.showerror.call_args.args[1])
        self.assertIsNone(self.app.procesando)

    def test_grabar_mientras_se_transcribe(self):
        seguir = threading.Event()

        def falso(carpeta, *_a):
            seguir.wait(10)
            t = merger.cargar(carpeta)
            return flujo.Procesado(t, exporter.exportar(t, carpeta), [], 1.0, False)

        self.simular_proceso(falso)
        self.app.procesar(self.demo)
        self.grabar()
        self.assertEqual(self.app.estado, gui.GRABANDO)
        seguir.set()
        self.bombear(lambda: self.app.procesando is None)
        self.assertEqual(self.app.estado, gui.GRABANDO)
        self.assertEqual(self.app.seleccion().carpeta, self.grabadora.carpeta)

    def test_transcribir_al_terminar(self):
        self.app.auto.select()
        self.app._guardar_auto()
        self.simular_proceso(ErrorGrabacion("fin"))
        self.grabar()
        with self.assertLogs("gui"):
            self.app.alternar()
            self.bombear(lambda: flujo.procesar.called and self.app.procesando is None)
        self.assertEqual(flujo.procesar.call_args.args[0], self.grabadora.carpeta)
        self.assertTrue(config.cargar(self.raiz / "config.json").transcribir_al_terminar)

    def test_ajustes(self):
        self.app.cfg = replace(self.app.cfg, microfono="Micrófono (Realtek")
        d = gui.DialogoAjustes(self.app, ["Micrófono (USB)", "Micrófono (Realtek(R) Audio)"], ["Altavoz"])
        self.bombear(d.winfo_viewable)
        self.assertEqual(d.micro.get(), "Micrófono (Realtek(R) Audio)")
        self.assertEqual(d.salida.get(), gui.PREDETERMINADO)
        # Un atajo no válido deja el diálogo abierto y no cambia nada.
        d.atajo.delete(0, "end")
        d.atajo.insert(0, "r")
        d.aceptar()
        self.assertTrue(d.winfo_exists())
        self.assertEqual(self.app.cfg.atajo, ATAJO)
        d.atajo.delete(0, "end")
        d.atajo.insert(0, "Ctrl+Alt+Shift+F22")
        d.nombre.delete(0, "end")
        d.nombre.insert(0, "Nora")
        d.salida.set("Altavoz")
        d.aceptar()
        c = config.cargar(self.raiz / "config.json")
        self.assertEqual((c.nombre_usuario, c.salida, c.microfono, c.atajo),
                         ("Nora", "Altavoz", "Micrófono (Realtek", "ctrl+alt+shift+f22"))
        libre = windows.Atajo(ATAJO, lambda: None)      # el anterior quedó libre
        libre.iniciar()
        libre.detener()

    def test_salir_grabando(self):
        self.grabar()
        self.msg.askokcancel.return_value = False
        self.app.salir()
        self.assertEqual(self.app.estado, gui.GRABANDO)
        self.msg.askokcancel.return_value = True
        self.app.salir()
        self.bombear(lambda: self.cerrada(self.app))
        self.assertEqual(len(self.grabadora.hilos), 2)      # se guardó antes de salir

    def test_salir_transcribiendo(self):
        cancelado = threading.Event()

        def falso(*a):
            a[-1].wait(5)
            cancelado.set()
            raise proceso.Cancelado

        self.simular_proceso(falso)
        self.app.procesar(self.demo)
        self.bombear(lambda: flujo.procesar.called)
        self.app.salir()
        self.bombear(lambda: self.cerrada(self.app))
        self.assertTrue(cancelado.is_set())


if __name__ == "__main__":
    unittest.main()
