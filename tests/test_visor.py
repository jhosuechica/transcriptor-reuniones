"""Pruebas de escuchar y corregir. No abren ningún dispositivo de audio."""

import copy
import os
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import flujo
import gui
import merger as m
import reproductor
import visor
from merger import Frase, Intervencion, Transcripcion
from test_gui import TestVentana as Base


def transcripcion():
    return Transcripcion(
        "T", "", {"yo": "Yo", "h1": "Ana", "h2": "Luis"},
        [Intervencion("yo", 0, 2, [Frase(0, 2, "Hola.")]),
         Intervencion("h1", 2.5, 8, [Frase(2.5, 4, "Uno."), Frase(4, 6, "Dos."), Frase(6, 8, "Tres.")]),
         Intervencion("h2", 9, 10, [Frase(9, 10, "Fin.")])])


def resumen(t):
    return [(i.hablante, i.texto) for i in t.intervenciones]


class TestCorregir(unittest.TestCase):
    def setUp(self):
        self.t = transcripcion()

    def test_reagrupar_sin_cambios_da_lo_mismo(self):
        antes = copy.deepcopy(self.t.intervenciones)
        self.assertEqual(m.juntar(m.frases(self.t)), antes)

    def test_texto(self):
        m.corregir(self.t, 2, "  Dos  y\nmedio. ")
        self.assertEqual(resumen(self.t)[1], ("h1", "Uno. Dos y medio. Tres."))
        self.assertTrue(self.t.corregida)

    def test_texto_vacio_no_cambia_nada(self):
        with self.assertRaises(m.ErrorUnion):
            m.corregir(self.t, 2, "  ")
        self.assertEqual(resumen(self.t), resumen(transcripcion()))

    def test_hablante_de_una_frase_parte_la_intervencion(self):
        m.corregir(self.t, 2, hablante="h2")
        self.assertEqual(resumen(self.t), [
            ("yo", "Hola."), ("h1", "Uno."), ("h2", "Dos."), ("h1", "Tres."), ("h2", "Fin.")])

    def test_hablante_de_toda_la_intervencion(self):
        m.corregir(self.t, 2, hablante="h2", toda=True)
        self.assertEqual(resumen(self.t), [("yo", "Hola."), ("h2", "Uno. Dos. Tres. Fin.")])

    def test_hablante_desconocido(self):
        with self.assertRaises(m.ErrorUnion):
            m.corregir(self.t, 0, hablante="h9")

    def test_borrar_y_dejar_vacia(self):
        m.borrar(self.t, 2)
        self.assertEqual(resumen(self.t)[1], ("h1", "Uno. Tres."))
        for _ in range(4):
            m.borrar(self.t, 0)
        self.assertEqual(self.t.intervenciones, [])

    def test_hablante_nuevo(self):
        self.assertEqual(m.agregar_hablante(self.t, " Eva  Paz "), "h3")
        self.assertEqual(self.t.hablantes["h3"], "Eva Paz")
        with self.assertRaises(m.ErrorUnion):
            m.agregar_hablante(self.t, " ")

    def test_la_marca_se_guarda_y_lo_antiguo_se_lee(self):
        with tempfile.TemporaryDirectory() as d:
            carpeta = Path(d)
            m.corregir(self.t, 0, "Adiós.")
            ruta = m.guardar(self.t, carpeta)
            self.assertTrue(m.cargar(carpeta).corregida)
            texto = ruta.read_text(encoding="utf-8").replace('"corregida": true', '"otra": 1')
            ruta.write_text(texto, encoding="utf-8")
            self.assertFalse(m.cargar(carpeta).corregida)

    def test_repetir_conserva_una_copia(self):
        with tempfile.TemporaryDirectory() as d:
            carpeta = Path(d)
            m.corregir(self.t, 0, "Mi corrección.")
            m.guardar(self.t, carpeta)
            nombres = [flujo._guardar_copia(carpeta) for _ in range(2)]
            self.assertEqual(len(set(nombres)), 2)
            self.assertIn("Mi corrección.", (carpeta / nombres[0]).read_text(encoding="utf-8"))


def pista(ruta, muestras, frecuencia=1000, canales=1):
    with wave.open(str(ruta), "wb") as w:
        w.setnchannels(canales), w.setsampwidth(2), w.setframerate(frecuencia)
        w.writeframes(np.asarray(muestras, np.int16).tobytes())


class FalsaSalida:
    def __init__(self, al_escribir=lambda _s: None):
        self.datos, self.cerrada, self.parada = [], False, False
        self.al_escribir = al_escribir

    def get_output_latency(self):
        return 0.0

    def write(self, b):
        self.datos.append(np.frombuffer(b, np.int16))
        self.al_escribir(self)

    def stop_stream(self):
        self.parada = True

    def close(self):
        self.cerrada = True

    @property
    def todo(self):
        return np.concatenate(self.datos) if self.datos else np.zeros(0, np.int16)


class TestReproductor(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        pista(self.dir / "mic.wav", np.arange(1000))
        pista(self.dir / "sistema.wav", np.full(1200, 10))
        self.salida = FalsaSalida()
        self.pa = mock.Mock()
        self.pa.open.side_effect = lambda **k: self.salida
        self.disp = {"defaultSampleRate": 1000.0, "maxOutputChannels": 1, "index": 7}
        self.pa.get_default_output_device_info.return_value = self.disp
        parche = mock.patch.object(reproductor.pyaudio, "PyAudio", return_value=self.pa)
        parche.start()
        self.addCleanup(parche.stop)

    def tocar(self, *a):
        s = reproductor.Sesion(self.dir, *a)
        s.ejecutar()
        self.assertTrue(self.salida.cerrada)
        self.pa.terminate.assert_called_once()
        return s

    def test_mezcla_entera_con_pistas_de_distinto_largo(self):
        s = self.tocar(0.0)
        esperado = np.full(1200, 10)
        esperado[:1000] += np.arange(1000)
        np.testing.assert_array_equal(self.salida.todo, esperado)
        self.assertEqual(s.posicion, 1.2)
        self.assertTrue(self.salida.parada)     # dejó sonar el final

    def test_tramo(self):
        self.tocar(0.5, 0.73)
        np.testing.assert_array_equal(self.salida.todo, np.arange(500, 730) + 10)

    def test_se_adapta_al_dispositivo(self):
        for n in ("mic.wav", "sistema.wav"):
            pista(self.dir / n, np.sin(np.arange(48000) * 2 * np.pi * 440 / 48000) * 8000, 48000)
        self.disp.update(defaultSampleRate=44100.0, maxOutputChannels=8)
        s = self.tocar(0.0)
        k = self.pa.open.call_args.kwargs
        self.assertEqual((k["rate"], k["channels"], k["output_device_index"]), (44100, 2, 7))
        x = self.salida.todo.reshape(-1, 2)
        np.testing.assert_array_equal(x[:, 0], x[:, 1])
        self.assertAlmostEqual(len(x), 44100, delta=2)
        # Sigue siendo un tono de 440 Hz con el doble de amplitud.
        self.assertEqual(int(np.argmax(np.abs(np.fft.rfft(x[:44100, 0])))), 440)
        self.assertAlmostEqual(int(x[:, 0].max()), 16000, delta=200)
        self.assertEqual(s.posicion, 1.0)

    def test_no_desborda(self):
        r = reproductor.mezclar([np.array([30000, -30000], np.int16)] * 2)
        np.testing.assert_array_equal(r, [32767, -32768])
        self.assertEqual(len(reproductor.mezclar([])), 0)

    def test_mas_alla_del_final(self):
        self.assertEqual(len(self.salida.todo), 0)
        self.tocar(50.0)

    def test_parar(self):
        s = reproductor.Sesion(self.dir, 0.0)
        self.salida.al_escribir = lambda _: s.parar()
        s.ejecutar()
        self.assertEqual(len(self.salida.datos), 1)
        self.assertAlmostEqual(s.posicion, 0.05)

    def test_parar_antes_de_empezar_no_abre_el_audio(self):
        s = reproductor.Sesion(self.dir, 0.0)
        s.parar()
        s.ejecutar()
        self.pa.open.assert_not_called()

    def test_saltar_atras_y_adelante(self):
        s = reproductor.Sesion(self.dir, 0.4, 0.6)
        saltos = [0.1, 1.15]

        def al_escribir(_):
            if saltos:
                s.saltar(saltos.pop(0))
        self.salida.al_escribir = al_escribir
        s.ejecutar()
        self.assertEqual([d[0] for d in self.salida.datos], [410, 110, 10])
        self.assertEqual(s.posicion, 1.2)       # tras un salto sigue hasta el final

    def test_una_sola_pista(self):
        (self.dir / "sistema.wav").unlink()
        self.tocar(0.9)
        np.testing.assert_array_equal(self.salida.todo, np.arange(900, 1000))

    def test_errores_claros(self):
        casos = []
        pista(self.dir / "mic.wav", np.zeros(10), frecuencia=500)
        casos.append("formato")
        with self.assertRaisesRegex(reproductor.ErrorReproduccion, "formato"):
            reproductor.Sesion(self.dir, 0).ejecutar()
        (self.dir / "mic.wav").write_bytes(b"roto")
        with self.assertRaisesRegex(reproductor.ErrorReproduccion, "No se pudo"):
            reproductor.Sesion(self.dir, 0).ejecutar()
        for n in ("mic.wav", "sistema.wav"):
            (self.dir / n).unlink()
        with self.assertRaisesRegex(reproductor.ErrorReproduccion, "No hay audio"):
            reproductor.Sesion(self.dir, 0).ejecutar()

    def test_sin_dispositivo_de_salida(self):
        self.pa.open.side_effect = OSError("sin salida")
        with self.assertRaisesRegex(reproductor.ErrorReproduccion, "sin salida"):
            reproductor.Sesion(self.dir, 0).ejecutar()
        self.pa.terminate.assert_called_once()


class FalsaSesion:
    """Como Sesion, pero sin sonido: avanza cuando lo dice la prueba."""

    todas: list = []

    def __init__(self, carpeta, desde, hasta=None):
        self.carpeta, self.posicion, self.desde, self.hasta = carpeta, desde, desde, hasta
        self.hilo = None
        self.fallo = None
        self._fin = threading.Event()
        FalsaSesion.todas.append(self)

    def parar(self):
        self._fin.set()

    def saltar(self, t):
        self.posicion = t

    def ejecutar(self):
        self.hilo = threading.current_thread().name
        self._fin.wait(5)
        if self.fallo:
            raise self.fallo


class TestVisor(Base):
    """Hereda la preparación de la ventana, no sus pruebas."""

    def setUp(self):
        FalsaSesion.todas = []
        parche = mock.patch.object(visor, "Sesion", FalsaSesion)
        parche.start()
        self.addCleanup(parche.stop)
        super().setUp()
        m.guardar(transcripcion(), self.demo)
        self.app.refrescar()
        self.v = self.app.visor
        self.bombear(lambda: self.v.texto.winfo_viewable())

    def contenido(self):
        return self.v.texto.get("1.0", "end")

    def evento(self, n, desplazamiento=1):
        (l, c), _ = self.v._lugares[n]
        self.v.texto.see(f"{l}.{c}")
        self.app.update()
        x, y, w, h = self.v.texto.bbox(f"{l}.{c + desplazamiento}")
        return SimpleNamespace(x=x + w // 2, y=y + h // 2, x_root=0, y_root=0)

    def dialogo(self):
        self.v.corregir()
        d = next(w for w in self.app.winfo_children() if isinstance(w, visor.DialogoCorregir)
                 and w.winfo_exists())
        self.bombear(d.winfo_viewable)
        return d

    def escribir(self, d, texto):
        d.texto.delete("1.0", "end")
        d.texto.insert("1.0", texto)

    # --- lectura -----------------------------------------------------------

    def test_aspecto(self):
        self.assertIn("Ana   00:00:02\nUno. Dos. Tres.\n", self.contenido())
        colores = {self.v.texto.tag_cget(f"persona{n}", "foreground") for n in range(3)}
        self.assertEqual(len(colores), 3)

    def test_mismo_nombre_mismo_color(self):
        self.assertTrue(self.v.cambiar(lambda t: m.renombrar(t, {"h2": "Ana"})))
        etiquetas = [self.v.texto.tag_names(f"{l}.0") for l in (3, 5)]
        self.assertIn("persona1", etiquetas[0])
        self.assertIn("persona1", etiquetas[1])

    # --- escuchar ----------------------------------------------------------

    def test_clic_coloca_sin_sonar_y_doble_clic_suena(self):
        self.v._clic(self.evento(2))
        self.assertEqual((self.v.actual, self.v.posicion), (2, 4))
        self.assertEqual(FalsaSesion.todas, [])
        self.v._doble_clic(self.evento(3))
        self.bombear(lambda: FalsaSesion.todas and FalsaSesion.todas[0].hilo)
        s = FalsaSesion.todas[0]
        self.assertEqual((s.desde, s.hasta), (6, None))
        self.assertTrue(s.hilo.startswith("audio"))
        self.assertEqual(self.v.boton_oir.cget("text"), "❚❚")

    def test_clic_en_la_cabecera(self):
        (l, _), _ = self.v._lugares[1]
        x, y, w, h = self.v.texto.bbox(f"{l - 1}.0")
        self.v._clic(SimpleNamespace(x=x + 1, y=y + h // 2))
        self.assertEqual(self.v.actual, 1)

    def test_resalta_lo_que_suena(self):
        self.v.reproducir(0)
        s = FalsaSesion.todas[0]
        for t, n in ((0.5, 0), (4.2, 2), (9.5, 4)):
            s.posicion = t
            self.bombear(lambda: self.v.actual == n)
        self.assertIn("00:00:09", self.v.tiempo.cget("text"))

    def test_pausa_y_continua_donde_estaba(self):
        self.v.alternar()
        FalsaSesion.todas[0].posicion = 4.5
        self.v.alternar()
        self.assertIsNone(self.v.sesion)
        self.assertEqual(self.v.boton_oir.cget("text"), "▶")
        self.v.alternar()
        self.assertEqual(FalsaSesion.todas[1].desde, 4.5)

    def test_una_sesion_vieja_no_para_la_nueva(self):
        self.v.reproducir(0)
        self.v.reproducir(5)
        self.bombear(lambda: FalsaSesion.todas[1].hilo)
        self.bombear(lambda: False, 0.2, exigir=False)
        self.assertIs(self.v.sesion, FalsaSesion.todas[1])

    def test_mover_y_deslizador(self):
        self.v.ir(9)
        self.v.mover(-5)
        self.assertEqual((self.v.posicion, self.v.actual), (4, 2))
        self.v.mover(-50)
        self.assertEqual(self.v.posicion, 0)
        self.v.reproducir(0)
        self.v.deslizador.set(0.65)
        self.v._arrastrar(0.65)
        self.v._soltar(None)
        self.assertAlmostEqual(FalsaSesion.todas[0].posicion, 6.5, places=1)

    def test_error_al_reproducir(self):
        self.v.reproducir(0)
        s = FalsaSesion.todas[0]
        s.fallo = reproductor.ErrorReproduccion("No hay salida.")
        with self.assertLogs("gui"):
            s._fin.set()
            self.bombear(lambda: self.msg.showerror.called)
        self.assertEqual(self.msg.showerror.call_args.args[1], "No hay salida.")
        self.assertIsNone(self.v.sesion)

    def test_al_grabar_se_calla(self):
        self.v.reproducir(0)
        self.grabar()
        self.assertTrue(FalsaSesion.todas[0]._fin.is_set())
        self.assertEqual(self.v.boton_oir.cget("state"), "disabled")
        self.v.reproducir(0)
        self.v.alternar()
        self.assertEqual(len(FalsaSesion.todas), 1)

    # --- corregir ----------------------------------------------------------

    def test_corregir_guarda_exporta_y_se_deshace(self):
        self.v.elegir(2)
        d = self.dialogo()
        self.assertEqual(d.texto.get("1.0", "end").strip(), "Dos.")
        self.escribir(d, "Dos y medio.")
        d.aceptar()
        self.assertIn("Uno. Dos y medio. Tres.", self.contenido())
        self.assertEqual(m.frases(m.cargar(self.demo))[2][1].texto, "Dos y medio.")
        txt = self.demo / "transcripcion.txt"
        self.bombear(lambda: txt.exists() and "Dos y medio." in txt.read_text(encoding="utf-8"))
        self.v.deshacer()
        self.assertIn("Uno. Dos. Tres.", self.contenido())
        self.assertFalse(m.cargar(self.demo).corregida)
        self.assertEqual(self.v.boton_deshacer.cget("state"), "disabled")
        self.bombear(lambda: "Dos y medio." not in txt.read_text(encoding="utf-8"))

    def test_sin_cambios_no_guarda(self):
        antes = (self.demo / "transcripcion.json").read_bytes()
        d = self.dialogo()
        d.mover(1)
        d.aceptar()
        self.assertEqual((self.demo / "transcripcion.json").read_bytes(), antes)
        self.assertEqual(self.v._historial, [])

    def test_recorrer_guardando(self):
        d = self.dialogo()
        self.escribir(d, "Primera.")
        d.mover(1)
        self.assertEqual((d.n, d.texto.get("1.0", "end").strip()), (1, "Uno."))
        self.escribir(d, "Segunda.")
        d.mover(1)
        self.assertEqual([f.texto for _, f in m.frases(m.cargar(self.demo))][:2],
                         ["Primera.", "Segunda."])
        self.assertEqual(len(self.v._historial), 2)
        d.cerrar()

    def test_cambiar_de_persona_y_nombre_nuevo(self):
        self.v.elegir(1)
        d = self.dialogo()
        d.quien.set("luis")         # da igual la mayúscula
        d.toda.select()
        d.mover(1)
        self.assertEqual(resumen(self.v.transcripcion)[1], ("h2", "Uno. Dos. Tres. Fin."))
        d.quien.set("Eva")
        d.aceptar()
        t = m.cargar(self.demo)
        self.assertEqual(t.hablantes["h3"], "Eva")
        self.assertIn(("h3", "Dos."), resumen(t))

    def test_nombre_vacio_deja_el_dialogo_abierto(self):
        d = self.dialogo()
        d.quien.set("  ")
        d.aceptar()
        self.assertTrue(d.winfo_exists())
        self.assertIn("nombre", self.msg.showerror.call_args.args[1])
        self.assertEqual(self.v._historial, [])
        d.cerrar()

    def test_nombres_repetidos_se_distinguen(self):
        self.v.cambiar(lambda t: m.renombrar(t, {"h2": "Ana"}))
        self.assertEqual(self.v.nombres_unicos(),
                         {"Yo": "yo", "Ana (h1)": "h1", "Ana (h2)": "h2"})
        self.v.elegir(4)
        d = self.dialogo()
        self.assertEqual(d.quien.get(), "Ana (h2)")
        d.aceptar()
        self.assertEqual(len(self.v._historial), 1)     # abrir y guardar no cambia nada

    def test_borrar_hasta_el_final(self):
        d = self.dialogo()
        for _ in range(5):
            d.borrar()
        self.assertFalse(d.winfo_exists())
        self.assertIn("No se detectó voz", self.contenido())
        self.assertEqual(self.v.boton_corregir.cget("state"), "disabled")
        for _ in range(5):
            self.v.deshacer()
        self.assertIn("Uno. Dos. Tres.", self.contenido())

    def test_oir_la_frase_desde_el_dialogo(self):
        self.v.elegir(3)
        d = self.dialogo()
        d.oir()
        s = FalsaSesion.todas[-1]
        self.assertEqual((round(s.desde, 1), round(s.hasta, 1)), (5.8, 8.3))
        d.cerrar()
        self.assertTrue(s._fin.is_set())

    def test_cambio_externo_no_se_pisa(self):
        otra = transcripcion()
        otra.titulo = "desde la consola"
        m.guardar(otra, self.demo)
        os.utime(self.demo / "transcripcion.json", ns=(1, 1))
        self.assertFalse(self.v.cambiar(lambda t: m.borrar(t, 0)))
        self.assertIn("fuera de la ventana", self.msg.showerror.call_args.args[1])
        self.assertEqual(m.cargar(self.demo).titulo, "desde la consola")

    def test_word_abierto_avisa_sin_interrumpir(self):
        self.app.exportar()
        self.bombear(lambda: (self.demo / "transcripcion.srt").exists())
        with open(self.demo / "transcripcion.docx", "rb"), self.assertLogs("gui"):
            self.v.cambiar(lambda t: m.corregir(t, 0, "Nuevo."))
            self.v.exportar_pendiente()
            self.bombear(lambda: "Ciérralos" in self.app.linea.cget("text"))
        self.msg.showerror.assert_not_called()
        self.assertEqual(m.frases(m.cargar(self.demo))[0][1].texto, "Nuevo.")

    def test_fin_de_grabacion_no_cambia_lo_que_se_corrige(self):
        self.grabar()
        self.app.lista.selection_set(self.demo.name)
        self.bombear(lambda: self.v.carpeta == self.demo)
        d = self.dialogo()
        self.escribir(d, "A medias.")
        self.app.alternar()
        self.bombear(lambda: self.app.estado == gui.PARADO)
        self.assertEqual(self.v.carpeta, self.demo)
        d.aceptar()
        self.assertEqual(m.frases(m.cargar(self.demo))[0][1].texto, "A medias.")

    def test_al_salir_se_exporta_lo_pendiente(self):
        self.v.cambiar(lambda t: m.corregir(t, 0, "Último cambio."))
        self.app.salir()
        self.bombear(lambda: self.cerrada(self.app))
        self.assertIn("Último cambio.", (self.demo / "transcripcion.txt").read_text(encoding="utf-8"))

    def test_no_se_edita_mientras_se_procesa(self):
        seguir = threading.Event()

        def falso(*_a):
            seguir.wait(5)
            raise gui.proceso.Cancelado

        self.simular_proceso(falso)
        self.app.procesar(self.demo)
        self.assertFalse(self.v.cambiar(lambda t: m.borrar(t, 0)))
        self.assertEqual(self.v.boton_corregir.cget("state"), "disabled")
        seguir.set()
        self.bombear(lambda: self.app.procesando is None)
        self.assertEqual(self.v.boton_corregir.cget("state"), "normal")

    def test_repetir_avisa_de_las_correcciones(self):
        self.v.cambiar(lambda t: m.corregir(t, 0, "X."))
        self.msg.askyesnocancel.return_value = None
        self.app.procesar_seleccion()
        self.assertIn("Tus correcciones", self.msg.askyesnocancel.call_args.args[1])

    def test_reunion_larga_es_agil(self):
        t = transcripcion()
        t.intervenciones = [
            Intervencion(("yo", "h1", "h2")[k % 3], k * 5, k * 5 + 4,
                         [Frase(k * 5 + j, k * 5 + j + 1, f"Frase número {k} con algo de texto.")
                          for j in range(3)]) for k in range(1000)]
        m.guardar(t, self.demo)
        inicio = time.perf_counter()
        self.app.refrescar()
        self.app.update()
        carga = time.perf_counter() - inicio
        inicio = time.perf_counter()
        self.v.cambiar(lambda t: m.corregir(t, 1500, "Cambio."))
        cambio = time.perf_counter() - inicio
        print(f"\n3000 frases: cargar {carga:.2f} s, corregir {cambio:.2f} s")
        self.assertLess(carga, 4)
        self.assertLess(cambio, 4)


# Las pruebas de la clase base ya se ejecutan en test_gui.
for _nombre in [n for n in dir(Base) if n.startswith("test_")]:
    setattr(TestVisor, _nombre, None) if _nombre not in TestVisor.__dict__ else None
del Base

if __name__ == "__main__":
    unittest.main()
