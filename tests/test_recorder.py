"""Pruebas sin hardware. Ejecutar: python -m unittest discover -s tests -v"""

import sys
import tempfile
import threading
import unittest
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import FRECUENCIA, Config, cargar, guardar
from recorder import AlineadorPista, Pista, a_mono, crear_carpeta, nombre_seguro

BLOQUE = FRECUENCIA // 50  # 20 ms


def duracion(bloques):
    return sum(len(b) for b in bloques) / FRECUENCIA


def unir(bloques):
    return np.concatenate(bloques) if bloques else np.zeros(0, np.int16)


class TestAlineador(unittest.TestCase):
    def setUp(self):
        self.a = AlineadorPista()
        self.salida = []

    def simular(self, desde, hasta, retraso=lambda i: 0.0, valor=1000, deriva=0.0):
        """Entrega bloques de 20 ms. deriva > 0: la tarjeta va más rápida."""
        paso = 0.02 / (1 + deriva)
        n = round((hasta - desde) / paso)
        for i in range(n):
            t = desde + (i + 1) * paso + retraso(i)
            self.salida += self.a.agregar(np.full(BLOQUE, valor, np.int16), t)

    def cerrar(self, t):
        self.salida += self.a.finalizar(t)

    def test_flujo_continuo_no_se_modifica(self):
        self.simular(0, 60)
        self.cerrar(60)
        x = unir(self.salida)
        self.assertEqual(len(x), 60 * FRECUENCIA)
        self.assertTrue((x == 1000).all())
        self.assertEqual(self.a.huecos, 0)

    def test_arranque_tardio_se_rellena(self):
        self.simular(0.3, 10)
        self.cerrar(10)
        x = unir(self.salida)
        self.assertAlmostEqual(np.argmax(x != 0) / FRECUENCIA, 0.3, delta=0.005)
        self.assertEqual(len(x), 10 * FRECUENCIA)

    def test_hueco_largo_conserva_la_posicion(self):
        self.simular(0, 5)
        self.simular(35, 40, valor=2000)
        self.cerrar(40)
        x = unir(self.salida)
        self.assertAlmostEqual(np.argmax(x == 2000) / FRECUENCIA, 35, delta=0.005)
        self.assertEqual(len(x), 40 * FRECUENCIA)
        self.assertEqual(self.a.huecos, 1)

    def test_retraso_puntual_no_inserta_silencio(self):
        # Entre 4 s y 5 s el PC se bloquea: todo llega de golpe a los 5 s.
        def retraso(i):
            t = (i + 1) * 0.02
            return 5.0 - t if 4.0 < t < 5.0 else 0.0

        self.simular(0, 20, retraso)
        self.cerrar(20)
        x = unir(self.salida)
        self.assertEqual(len(x), 20 * FRECUENCIA)
        self.assertEqual(self.a.huecos, 0)
        self.assertTrue((x == 1000).all())

    def test_deriva_en_una_hora(self):
        for deriva in (200e-6, -200e-6):
            with self.subTest(deriva=deriva):
                self.a, self.salida = AlineadorPista(), []
                peor = 0.0
                paso = 0.02 / (1 + deriva)
                total = 0
                for i in range(round(3600 / paso)):
                    t = (i + 1) * paso
                    for b in self.a.agregar(np.full(BLOQUE, 500, np.int16), t):
                        total += len(b)
                    if t > 1:
                        peor = max(peor, abs(t - self.a.escritas / FRECUENCIA))
                # Sin corregir serían 720 ms.
                self.assertLess(peor, 0.030)
                self.assertEqual(self.a.huecos, 0)
                self.assertEqual(total, self.a.escritas)

    def test_jitter_no_provoca_correcciones(self):
        rng = np.random.default_rng(1)
        ruido = rng.exponential(0.005, 60 * 50)
        self.simular(0, 60, lambda i: float(ruido[i]))
        self.cerrar(60)
        self.assertEqual(self.a.huecos, 0)
        self.assertLess(abs(self.a.deriva_corregida), FRECUENCIA * 0.01)

    def test_pista_sin_datos_queda_en_silencio(self):
        self.cerrar(12.5)
        x = unir(self.salida)
        self.assertEqual(len(x), int(12.5 * FRECUENCIA))
        self.assertFalse(x.any())

    def test_grabacion_muy_corta(self):
        self.simular(0, 0.2)
        self.cerrar(0.2)
        self.assertAlmostEqual(duracion(self.salida), 0.2, delta=0.001)

    def test_hueco_al_final_sin_confirmar(self):
        self.simular(0, 5)
        self.simular(8, 8.1, valor=2000)
        self.cerrar(8.1)
        x = unir(self.salida)
        self.assertAlmostEqual(np.argmax(x == 2000) / FRECUENCIA, 8, delta=0.005)
        self.assertAlmostEqual(len(x) / FRECUENCIA, 8.1, delta=0.005)


class TestPista(unittest.TestCase):
    """Recorre cola -> hilo -> WAV con un dispositivo simulado."""

    class StreamFalso:
        def start_stream(self): pass
        def is_active(self): return True
        def close(self): pass

    def grabar(self, frecuencia, canales, segundos, carpeta):
        disp = {"name": "falso", "index": 0, "defaultSampleRate": float(frecuencia),
                "maxInputChannels": canales}
        p = Pista(None, disp, Path(carpeta) / "x.wav", "x")
        p._stream = self.StreamFalso()
        p.iniciar(0.0)
        n = frecuencia // 50
        for i in range(segundos * 50):
            t = np.arange(i * n, (i + 1) * n) / frecuencia
            s = (8000 * np.sin(2 * np.pi * 440 * t)).astype(np.int16)
            p._cola.put((np.repeat(s, canales).tobytes(), (i + 1) * 0.02, 0))
        p.detener(float(segundos))
        self.assertIsNone(p.estado.error)
        with wave.open(str(p.ruta)) as w:
            self.assertEqual((w.getnchannels(), w.getframerate()), (1, FRECUENCIA))
            return np.frombuffer(w.readframes(w.getnframes()), np.int16)

    def test_formatos(self):
        for frecuencia, canales in ((48000, 2), (44100, 2), (16000, 1), (48000, 8)):
            with self.subTest(frecuencia=frecuencia, canales=canales), \
                    tempfile.TemporaryDirectory() as d:
                x = self.grabar(frecuencia, canales, 4, d)
                self.assertAlmostEqual(len(x) / FRECUENCIA, 4, delta=0.03)
                medio = x[FRECUENCIA : 2 * FRECUENCIA].astype(float)
                tono = np.argmax(np.abs(np.fft.rfft(medio)))
                self.assertAlmostEqual(tono, 440, delta=1)
                self.assertAlmostEqual(np.abs(medio).max(), 8000, delta=100)

    def test_wav_valido_si_el_programa_se_interrumpe(self):
        with tempfile.TemporaryDirectory() as d:
            disp = {"name": "falso", "index": 0, "defaultSampleRate": 48000.0,
                    "maxInputChannels": 1}
            p = Pista(None, disp, Path(d) / "x.wav", "x")
            p._stream = self.StreamFalso()
            p.iniciar(0.0)
            for i in range(200):
                p._cola.put((np.ones(BLOQUE, np.int16).tobytes(), (i + 1) * 0.02, 0))
            espera = threading.Event()
            for _ in range(100):
                if p.estado.segundos >= 4:
                    break
                espera.wait(0.05)
            p._archivo.flush()
            # Se lee sin haber cerrado la grabación.
            with wave.open(str(p.ruta)) as w:
                self.assertAlmostEqual(w.getnframes() / FRECUENCIA, 4, delta=0.1)
            p.detener(4.0)


class TestUtilidades(unittest.TestCase):
    def test_a_mono(self):
        x = np.array([100, 300, -100, -300], np.int16).tobytes()
        self.assertEqual(a_mono(x, 2).tolist(), [200, -200])

    def test_nombre_seguro(self):
        self.assertEqual(nombre_seguro('Revisión: Q4 / "ventas"?'), "Revisión-Q4-ventas")
        self.assertEqual(nombre_seguro("../.."), "reunion")
        self.assertEqual(nombre_seguro("   "), "reunion")

    def test_carpetas_no_se_pisan(self):
        with tempfile.TemporaryDirectory() as d:
            m = datetime(2026, 11, 23, 9, 5)
            a = crear_carpeta(Path(d), "Diaria", m)
            b = crear_carpeta(Path(d), "Diaria", m)
            self.assertEqual(a.name, "2026-11-23_09-05_Diaria")
            self.assertEqual(b.name, "2026-11-23_09-05_Diaria_2")

    def test_config(self):
        with tempfile.TemporaryDirectory() as d:
            ruta = Path(d) / "c.json"
            guardar(Config(nombre_usuario="Ñandú"), ruta)
            self.assertEqual(cargar(ruta).nombre_usuario, "Ñandú")
            ruta.write_text("{mal", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                cargar(ruta)
            self.assertEqual(ruta.read_text(encoding="utf-8"), "{mal")


if __name__ == "__main__":
    unittest.main()
