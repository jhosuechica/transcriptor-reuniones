"""Pruebas de niveles, espacio, títulos, papelera y cola. Sin audio real."""

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
import exporter
import flujo
import gui
import merger
import recorder
import windows
from recorder import ErrorGrabacion
from test_gui import TestVentana as Base
from test_gui import crear_reunion

GB = 1_000_000_000


class TestEspacio(unittest.TestCase):
    def test_carpeta_que_aun_no_existe(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertGreater(recorder.espacio_libre(Path(d) / "a" / "b"), 0)
        self.assertIsNone(recorder.espacio_libre(Path("Q:/no/existe")))

    def test_tiempo(self):
        hora = 3600 * recorder.BYTES_POR_SEGUNDO
        self.assertEqual(recorder.tiempo_disponible(recorder.ESPACIO_PARADA + hora), "1 h 0 min")
        self.assertEqual(recorder.tiempo_disponible(recorder.ESPACIO_PARADA + hora // 2), "30 min")
        self.assertEqual(recorder.tiempo_disponible(0), "0 min")

    def test_sin_espacio_no_se_abre_nada(self):
        g = recorder.Grabadora(config.Config())
        with (mock.patch.object(recorder, "espacio_libre", return_value=GB // 10),
              mock.patch.object(recorder.pyaudio, "PyAudio") as pa):
            with self.assertRaisesRegex(ErrorGrabacion, "0.1 GB"):
                g.iniciar("x")
        pa.assert_not_called()
        self.assertFalse(g.grabando)


class TestNiveles(unittest.TestCase):
    def test_valoracion(self):
        casos = {-120: "no capta nada", -1: "demasiado alto", -40: "muy bajo", -12: "correcto"}
        for pico, texto in casos.items():
            self.assertIn(texto, recorder.valorar_niveles(pico, -20)[0])
        self.assertIn("no sonó nada", recorder.valorar_niveles(-12, -120)[1])
        self.assertIn("se oye", recorder.valorar_niveles(-12, -20)[1])

    def preparar(self):
        self.pa = mock.Mock()
        self.streams = []
        disp = {"index": 1, "name": "Mic", "defaultSampleRate": 48000.0, "maxInputChannels": 2}
        parches = [
            mock.patch.object(recorder.pyaudio, "PyAudio", return_value=self.pa),
            mock.patch.object(recorder, "elegir_microfono", return_value=(disp, "aviso")),
            mock.patch.object(recorder, "elegir_loopback",
                              return_value=(disp | {"index": 2, "name": "Salida"}, None)),
        ]
        for p in parches:
            p.start()
            self.addCleanup(p.stop)

    def test_mide_y_cierra(self):
        self.preparar()
        prueba = recorder.PruebaNiveles(config.Config())

        def abrir(**k):
            valor = 16384 if k["input_device_index"] == 1 else 0
            k["stream_callback"](np.full(200, valor, np.int16).tobytes(), 100, None, 0)
            k["stream_callback"](np.zeros(200, np.int16).tobytes(), 100, None, 0)
            s = mock.Mock()
            self.streams.append(s)
            prueba.parar()
            return s

        self.pa.open.side_effect = abrir
        prueba.ejecutar()
        self.assertAlmostEqual(prueba.mic.pico_max_dbfs, -6.0, delta=0.1)
        self.assertEqual(prueba.mic.pico_dbfs, recorder.SILENCIO_DBFS)
        self.assertEqual(prueba.sistema.pico_max_dbfs, recorder.SILENCIO_DBFS)
        self.assertEqual(prueba.avisos, ["aviso"])
        self.assertEqual(len(self.streams), 2)
        for s in self.streams:
            s.close.assert_called_once()
        self.pa.terminate.assert_called_once()

    def test_fallo_al_abrir_cierra_lo_abierto(self):
        self.preparar()
        primero = mock.Mock()
        self.pa.open.side_effect = [primero, OSError("ocupado")]
        with self.assertRaisesRegex(ErrorGrabacion, "Salida.*ocupado"):
            recorder.PruebaNiveles(config.Config()).ejecutar()
        primero.close.assert_called_once()
        self.pa.terminate.assert_called_once()

    def test_parada_antes_de_empezar_no_abre_el_microfono(self):
        self.preparar()
        prueba = recorder.PruebaNiveles(config.Config())
        prueba.parar()
        prueba.ejecutar()
        recorder.pyaudio.PyAudio.assert_not_called()


class TestTitulo(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.raiz = Path(tmp.name)
        self.c = crear_reunion(self.raiz)

    def test_cambia_carpeta_y_archivos(self):
        nueva = flujo.renombrar_reunion(self.c, "  Revisión:  del sprint ")
        self.assertEqual(nueva.name, "2026-01-02_10-00_Revisión-del-sprint")
        self.assertFalse(self.c.exists())
        s = json.loads((nueva / "sesion.json").read_text(encoding="utf-8"))
        self.assertEqual((s["titulo"], s["terminada"]), ("Revisión: del sprint", True))
        self.assertEqual(merger.cargar(nueva).titulo, "Revisión: del sprint")
        self.assertTrue((nueva / "transcripcion.txt").read_text(encoding="utf-8")
                        .startswith("Revisión: del sprint"))

    def test_sin_transcribir(self):
        c = crear_reunion(self.raiz, "2026-01-03_10-00_x", texto=False)
        nueva = flujo.renombrar_reunion(c, "Otra")
        self.assertEqual(flujo.listar(self.raiz)[0].titulo, "Otra")
        self.assertFalse((nueva / "transcripcion.json").exists())

    def test_nombre_ocupado_y_mayusculas(self):
        crear_reunion(self.raiz, "2026-01-02_10-00_Otra")
        nueva = flujo.renombrar_reunion(self.c, "Otra")
        self.assertEqual(nueva.name, "2026-01-02_10-00_Otra_2")
        nueva = flujo.renombrar_reunion(nueva, "demo")
        self.assertEqual(flujo.renombrar_reunion(nueva, "DEMO").name, "2026-01-02_10-00_DEMO")
        self.assertIn("2026-01-02_10-00_DEMO", [p.name for p in self.raiz.iterdir()])

    def test_carpeta_ajena_conserva_su_nombre(self):
        c = crear_reunion(self.raiz, "mi carpeta")
        self.assertEqual(flujo.renombrar_reunion(c, "Nuevo"), c)
        self.assertEqual(merger.cargar(c).titulo, "Nuevo")

    def test_titulos_no_validos(self):
        for t in ("", "   ", "a\x00b", "x" * 101):
            with self.assertRaises(ErrorGrabacion):
                flujo.renombrar_reunion(self.c, t)
        self.assertEqual(merger.cargar(self.c).titulo, "Demo")

    def test_archivo_abierto_no_cambia_nada(self):
        with open(self.c / "mic.wav", "rb"):
            with self.assertRaisesRegex(ErrorGrabacion, "Cierra"):
                flujo.renombrar_reunion(self.c, "Nuevo")
        self.assertEqual(flujo.leer_sesion(self.c)["titulo"], "Demo")
        self.assertEqual(merger.cargar(self.c).titulo, "Demo")


class TestPapelera(unittest.TestCase):
    def test_pide_la_papelera_y_avisa_si_no_cabe(self):
        with tempfile.TemporaryDirectory() as d:
            ruta = Path(d) / "con espacios ñ"
            ruta.mkdir()
            visto = {}

            def falso(ref):
                op = ref._obj
                visto.update(f=op.wFunc, banderas=op.fFlags, origen=op.pFrom)
                ruta.rmdir()
                return 0

            with mock.patch.object(windows, "_shell32", mock.Mock(SHFileOperationW=falso)):
                windows.a_papelera(ruta)
        self.assertEqual(visto["f"], 3)
        self.assertTrue(visto["banderas"] & 0x0040)     # papelera
        self.assertTrue(visto["banderas"] & 0x4000)     # avisa antes de borrar de verdad
        self.assertTrue(visto["origen"].endswith("con espacios ñ"))

    def test_fallos(self):
        with tempfile.TemporaryDirectory() as d:
            ruta = Path(d) / "x"
            with self.assertRaises(windows.ErrorPapelera):
                windows.a_papelera(ruta)
            ruta.mkdir()
            falso = mock.Mock(SHFileOperationW=lambda _r: 32)
            with mock.patch.object(windows, "_shell32", falso), self.assertLogs("windows"):
                with self.assertRaisesRegex(windows.ErrorPapelera, "Cierra"):
                    windows.a_papelera(ruta)
            self.assertTrue(ruta.exists())


class FalsaPrueba:
    def __init__(self, cfg):
        self.mic, self.sistema = recorder.EstadoPista(-15, -12), recorder.EstadoPista(-25, -20)
        self.avisos, self.fallo = [], None
        self.fin = threading.Event()
        self.hilo = None
        FalsaPrueba.ultima = self

    def parar(self):
        self.fin.set()

    def ejecutar(self):
        self.hilo = threading.current_thread().name
        self.fin.wait(5)
        if self.fallo:
            raise self.fallo


class TestVentanaMejoras(Base):
    def setUp(self):
        for objeto, nombre, valor in (
            (gui, "PruebaNiveles", FalsaPrueba),
            (windows, "llamar_atencion", mock.Mock()),
        ):
            p = mock.patch.object(objeto, nombre, valor)
            p.start()
            self.addCleanup(p.stop)
        super().setUp()

    def fila(self, carpeta):
        return self.app.lista.item(carpeta.name, "values")

    # --- prueba de niveles -------------------------------------------------

    def test_probar(self):
        self.app.alternar_prueba()
        p = FalsaPrueba.ultima
        self.bombear(lambda: self.app.medidor_mic.barra.get() > 0.5)
        self.assertEqual(self.app.boton_probar.cget("text"), "Terminar")
        self.assertTrue(p.hilo.startswith("audio"))
        self.app.alternar_prueba()
        self.bombear(lambda: self.msg.showinfo.called)
        self.assertIn("nivel correcto", self.msg.showinfo.call_args.args[1])
        self.assertEqual(self.app.medidor_mic.barra.get(), 0)
        self.assertEqual(self.app.boton_probar.cget("text"), "Probar")

    def test_grabar_corta_la_prueba_sin_mostrar_resultado(self):
        self.app.alternar_prueba()
        self.grabar()
        self.assertEqual(self.app.estado, gui.GRABANDO)
        self.assertTrue(FalsaPrueba.ultima.fin.is_set())
        self.msg.showinfo.assert_not_called()
        self.assertEqual(self.app.boton_probar.cget("state"), "disabled")

    def test_prueba_con_error(self):
        self.app.alternar_prueba()
        FalsaPrueba.ultima.fallo = ErrorGrabacion("No hay micrófono.")
        with self.assertLogs("gui"):
            FalsaPrueba.ultima.parar()
            self.bombear(lambda: self.msg.showerror.called)
        self.assertIsNone(self.app.prueba)

    def test_salir_durante_la_prueba(self):
        self.app.alternar_prueba()
        self.app.salir()
        self.bombear(lambda: self.cerrada(self.app))
        self.msg.showinfo.assert_not_called()

    # --- espacio -----------------------------------------------------------

    def test_aviso_de_poco_espacio_al_empezar(self):
        with mock.patch.object(gui, "espacio_libre", return_value=GB):
            self.app.alternar()
        d = self.app.dialogo_inicio
        self.bombear(d.winfo_viewable)
        textos = [w.cget("text") for w in d.winfo_children() if isinstance(w, gui.ctk.CTkLabel)]
        self.assertTrue(any("cabe una grabación de 1 h" in t for t in textos), textos)
        d.cerrar()

    def test_disco_casi_lleno_detiene_y_guarda(self):
        self.grabar()
        with mock.patch.object(gui, "espacio_libre", return_value=GB // 2):
            self.app._revisar_espacio()
        self.assertEqual(self.app.estado, gui.GRABANDO)
        self.assertIn("poco espacio", self.app.aviso.cget("text"))
        with (mock.patch.object(gui, "espacio_libre", return_value=GB // 10),
              self.assertLogs("gui")):
            self.app._revisar_espacio()
        self.bombear(lambda: self.msg.showwarning.called)
        self.assertEqual(self.app.estado, gui.PARADO)
        self.assertIn("disco está casi lleno", self.msg.showwarning.call_args.args[1])
        self.assertEqual(len(self.grabadora.hilos), 2)

    # --- títulos y papelera ------------------------------------------------

    def test_cambiar_titulo(self):
        self.app.cambiar_titulo()
        d = next(w for w in self.app.winfo_children() if isinstance(w, gui.DialogoTitulo))
        self.bombear(d.winfo_viewable)
        self.assertEqual(d.campo.get(), "Demo")
        d.campo.delete(0, "end")
        d.campo.insert(0, "Planificación")
        d.aceptar()
        self.bombear(lambda: self.app.moviendo is None)
        r = self.app.seleccion()
        self.assertEqual((r.titulo, r.carpeta.name),
                         ("Planificación", "2026-01-02_10-00_Planificación"))
        self.assertEqual(self.app.visor.carpeta, r.carpeta)
        # Después se puede seguir corrigiendo.
        self.assertTrue(self.app.visor.cambiar(lambda t: merger.corregir(t, 0, "Nuevo.")))

    def test_cambiar_titulo_con_una_correccion_pendiente(self):
        self.app.visor.cambiar(lambda t: merger.corregir(t, 0, "Corregido."))
        self.app._mover(self.demo, lambda: flujo.renombrar_reunion(self.demo, "Otra"), "Hecho.")
        self.assertFalse(self.app.visor.cambiar(lambda t: merger.borrar(t, 0)))
        self.bombear(lambda: self.app.moviendo is None)
        c = self.app.seleccion().carpeta
        self.assertIn("Corregido.", (c / "transcripcion.txt").read_text(encoding="utf-8"))
        self.assertFalse(self.demo.exists())

    def test_titulo_con_archivo_abierto(self):
        with open(self.demo / "mic.wav", "rb"), self.assertLogs("gui"):
            self.app._mover(self.demo, lambda: flujo.renombrar_reunion(self.demo, "X"), "Hecho.")
            self.bombear(lambda: self.msg.showerror.called)
        self.assertIsNone(self.app.moviendo)
        self.assertEqual(self.app.seleccion().carpeta, self.demo)

    def test_borrar(self):
        otra = crear_reunion(self.raiz / "R", "2026-01-03_10-00_Otra")
        self.app.refrescar()
        self.app.lista.selection_set(self.demo.name)
        self.msg.askokcancel.return_value = False
        with mock.patch.object(windows, "a_papelera") as papelera:
            self.app.borrar()
            papelera.assert_not_called()
            self.msg.askokcancel.return_value = True
            papelera.side_effect = lambda c: __import__("shutil").rmtree(c)
            self.app.borrar()
            self.bombear(lambda: self.app.moviendo is None)
            papelera.assert_called_once_with(self.demo)
        self.assertEqual(list(self.app.reuniones), [otra.name])
        self.assertEqual(self.app.visor.carpeta, otra)

    def test_no_se_borra_lo_que_se_usa(self):
        self.grabar()
        self.assertEqual(self.app.boton_borrar.cget("state"), "disabled")
        with mock.patch.object(windows, "a_papelera") as papelera:
            self.app.borrar()
        papelera.assert_not_called()
        self.msg.askokcancel.assert_not_called()

    # --- cola --------------------------------------------------------------

    def preparar_cola(self):
        self.orden, self.seguir = [], threading.Semaphore(0)

        def falso(carpeta, cfg, personas, repetir, al_estado, al_progreso, cancelar):
            self.orden.append((carpeta.name, personas, cfg.modelo))
            while not self.seguir.acquire(timeout=0.02):
                if cancelar.is_set():
                    raise gui.proceso.Cancelado
            t = merger.cargar(carpeta)
            return flujo.Procesado(t, exporter.exportar(t, carpeta), [], 1.0, False)

        self.simular_proceso(falso)
        # Al terminar la prueba, que nada quede esperando.
        self.addCleanup(lambda: [self.seguir.release() for _ in range(9)])
        self.b = crear_reunion(self.raiz / "R", "2026-01-03_10-00_B")
        self.c = crear_reunion(self.raiz / "R", "2026-01-04_10-00_C")
        self.app.refrescar()

    def test_cola_en_orden(self):
        self.preparar_cola()
        self.app.procesar(self.demo)
        self.app.procesar(self.b, 2)
        self.app.cfg = gui.replace(self.app.cfg, modelo="small")
        self.app.procesar(self.c)
        self.app.procesar(self.b)       # repetida: se ignora
        self.assertEqual([self.fila(x)[3] for x in (self.demo, self.b, self.c)],
                         ["Procesando", "En cola (1)", "En cola (2)"])
        for _ in range(3):
            self.seguir.release()
        self.bombear(lambda: self.app.procesando is None and not self.app.cola)
        self.assertEqual(self.orden, [(self.demo.name, None, "auto"), (self.b.name, 2, "auto"),
                                      (self.c.name, None, "small")])
        self.assertEqual(self.fila(self.c)[3], flujo.TRANSCRITA)

    def test_quitar_de_la_cola(self):
        self.preparar_cola()
        self.app.procesar(self.demo)
        self.app.procesar(self.b)
        self.app.lista.selection_set(self.b.name)
        self.bombear(lambda: self.app.boton_procesar.cget("text") == "Quitar de la cola")
        self.assertEqual(self.app.boton_borrar.cget("state"), "disabled")
        self.app.procesar_seleccion()
        self.assertEqual(self.app.cola, [])
        self.assertEqual(self.fila(self.b)[3], flujo.TRANSCRITA)
        self.assertEqual(self.app.boton_borrar.cget("state"), "normal")

    def test_cancelar_pasa_a_la_siguiente(self):
        self.preparar_cola()
        self.app.procesar(self.demo)
        self.app.procesar(self.b)
        self.app.cancelar_proceso()
        self.bombear(lambda: self.app.procesando == self.b)
        self.assertFalse(self.app.cancelar.is_set())

    def test_la_cola_espera_a_que_se_termine_de_grabar(self):
        self.preparar_cola()
        self.grabar()
        self.app.procesar(self.b)
        self.bombear(lambda: False, 0.3, exigir=False)
        self.assertEqual(self.orden, [])
        self.app.alternar()
        self.bombear(lambda: self.app.procesando == self.b)

    def test_carpeta_desaparecida_se_salta(self):
        self.preparar_cola()
        self.app.procesar(self.demo)
        self.app.procesar(self.b)
        self.app.procesar(self.c)
        __import__("shutil").rmtree(self.b)
        self.seguir.release()
        self.bombear(lambda: self.app.procesando == self.c)

    def test_salir_con_cola_no_empieza_otra(self):
        self.preparar_cola()
        self.app.procesar(self.demo)
        self.app.procesar(self.b)
        self.app.salir()
        self.assertIn("1 en cola", self.msg.askokcancel.call_args.args[1])
        self.bombear(lambda: self.cerrada(self.app))
        self.assertEqual(len(self.orden), 1)

    def test_tiempo_restante(self):
        self.preparar_cola()
        self.app.procesar(self.demo)
        with mock.patch.object(gui.time, "monotonic") as reloj:
            reloj.return_value = 100.0
            self.app._mostrar_avance("Transcribiendo", 0.0)
            self.app._mostrar_avance(None, 0.5)     # el avance puede empezar a medias
            reloj.return_value = 160.0
            self.app._mostrar_avance(None, 0.6)
            self.assertIn("quedan unos 4 min", self.app.linea.cget("text"))
            reloj.return_value = 300.0
            self.app._mostrar_avance(None, 0.99)
            self.assertIn("menos de 1 min", self.app.linea.cget("text"))
            self.app._mostrar_avance("Otro paso", 0.0)
            self.assertEqual(self.app.linea.cget("text"), "Otro paso")

    def test_avisa_al_terminar_si_se_usa_otro_programa(self):
        self.preparar_cola()
        with mock.patch.object(self.app, "focus_displayof", return_value=None):
            self.app.procesar(self.demo)
            self.seguir.release()
            self.bombear(lambda: self.app.procesando is None)
        windows.llamar_atencion.assert_called()


for _nombre in [n for n in dir(Base) if n.startswith("test_")]:
    if _nombre not in TestVentanaMejoras.__dict__:
        setattr(TestVentanaMejoras, _nombre, None)
del Base

if __name__ == "__main__":
    unittest.main()
