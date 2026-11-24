"""Pruebas sin modelos ni GPU. Los trabajadores falsos simulan los fallos."""

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

import proceso
import transcriber as t
from config import Config

GPU = t.Intento("cuda", "large-v3", "float16")
CPU = t.Intento("cpu", "small", "int8")


def _ok(con, rutas, intento, _v):
    con.send(("progreso", 1.0))
    for r in rutas:
        con.send(("resultado", t.Resultado(Path(r).name, 1.0, intento.modelo,
                                           intento.dispositivo, intento.calculo, 0.1, [])))
    con.send(("fin", None))


def _muere(*_):
    os._exit(7)


def _sin_memoria(con, *_):
    con.send(("error", "RuntimeError: CUDA failed with error out of memory"))


def _colgado(*_):
    time.sleep(60)


def _gpu_muere_cpu_ok(con, rutas, intento, v):
    if intento.dispositivo == "cuda":
        os._exit(7)
    _ok(con, rutas, intento, v)


def _mal_wav(con, *_):
    con.send(("error_usuario", "no es un WAV válido"))


def ejecutar(trabajador, intento=CPU, **kw):
    return t._ejecutar([Path("a.wav")], intento, "", lambda _: None, lambda _: None,
                       kw.pop("cancelar", None), trabajador=trabajador, **kw)


class TestPlan(unittest.TestCase):
    TODOS = list(t.MODELOS)

    def test_gpu_automatica(self):
        p = t.planificar("auto", "auto", True, self.TODOS)
        self.assertEqual([(i.dispositivo, i.modelo, i.calculo) for i in p], [
            ("cuda", "large-v3", "int8_float16"), ("cuda", "large-v3", "int8_float16"),
            ("cpu", "small", "int8")])
        self.assertEqual([i.lote for i in p], [t.LOTE_GPU, 1, 1])

    def test_sin_gpu_usa_modelo_pequeno(self):
        self.assertEqual(t.planificar("auto", "auto", False, self.TODOS), [CPU])

    def test_respeta_lo_elegido(self):
        p = t.planificar("medium", "cpu", True, self.TODOS)
        self.assertEqual(p, [t.Intento("cpu", "medium", "int8")])
        p = t.planificar("small", "gpu", True, self.TODOS)
        self.assertTrue(all(i.dispositivo == "cuda" and i.modelo == "small" for i in p))

    def test_errores_claros(self):
        for args in (("auto", "auto", True, []), ("medium", "auto", True, ["small"]),
                     ("otro", "auto", True, ["small"]), ("auto", "gpu", False, ["small"]),
                     ("auto", "tpu", True, ["small"])):
            with self.subTest(args=args), self.assertRaises(t.ErrorTranscripcion):
                t.planificar(*args)


class _ModeloFalso:
    def __init__(self, textos, dispositivo="cuda", voz=30.0):
        self.textos, self.voz = textos, voz
        self.model = SimpleNamespace(device=dispositivo)

    def transcribe(self, _audio, **_):
        segs = (SimpleNamespace(text=x, start=i, end=i + 1, words=[])
                for i, x in enumerate(self.textos))
        return segs, SimpleNamespace(duration=30.0, duration_after_vad=self.voz)


class TestTextoCorrupto(unittest.TestCase):
    def test_detecta(self):
        for malo in ("!" * 40, "Do" + "!" * 30, "Hasta" + "?" * 12, "a" * 30):
            self.assertTrue(t.es_corrupto(malo), malo)

    def test_no_molesta_al_texto_normal(self):
        for bueno in ("¡Hola! ¿Qué tal?", "¡¡¡Gol!!!", "Nooooooo", "hola hola hola hola hola hola",
                      "Espera...................... ya", "1000000000000", ""):
            self.assertFalse(t.es_corrupto(bueno), bueno)

    def transcribir(self, *a, **kw):
        return t._transcribir_audio(_ModeloFalso(*a, **kw), None, "", 1, lambda _: None)

    def test_interrumpe_el_intento(self):
        with self.assertRaises(t.TextoCorrupto):
            self.transcribir(["Todo bien.", "!" * 50])
        self.assertEqual(len(self.transcribir(["Todo bien.", "Sigue bien."])), 2)

    def test_vacio_con_voz(self):
        with self.assertRaises(t.TextoCorrupto):
            self.transcribir([])
        # En CPU, o casi sin voz, un resultado vacío es válido.
        self.assertEqual(self.transcribir([], "cpu"), [])
        self.assertEqual(self.transcribir([], voz=1.0), [])


class TestProceso(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(ejecutar(_ok)[0].archivo, "a.wav")

    def test_muerte_del_proceso(self):
        with self.assertRaisesRegex(proceso.FalloProceso, "inesperada"):
            ejecutar(_muere)

    def test_sin_memoria(self):
        with self.assertRaises(proceso.FalloProceso) as e:
            ejecutar(_sin_memoria)
        self.assertEqual(proceso.explicar(str(e.exception)), "no hay memoria suficiente")

    def test_gpu_bloqueada(self):
        with self.assertRaisesRegex(proceso.FalloProceso, "dejó de responder"):
            ejecutar(_colgado, GPU, limite_gpu=1)

    def test_cancelar(self):
        e = threading.Event()
        threading.Timer(0.5, e.set).start()
        inicio = time.monotonic()
        with self.assertRaises(proceso.Cancelado):
            ejecutar(_colgado, cancelar=e)
        self.assertLess(time.monotonic() - inicio, 15)

    def test_error_de_usuario_no_se_reintenta(self):
        with self.assertRaises(t.ErrorTranscripcion):
            ejecutar(_mal_wav)

    def test_respaldo_en_cpu(self):
        real, estados = t._ejecutar, []

        def con_falso(*a):
            return real(*a, trabajador=_gpu_muere_cpu_ok)

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(t, "hay_gpu", return_value=True), \
                mock.patch.object(t, "modelos_disponibles", return_value=["large-v3", "small"]), \
                mock.patch.object(t, "_ejecutar", con_falso):
            wav = Path(d) / "a.wav"
            wav.touch()
            r = t.transcribir([wav], Config(), al_estado=estados.append)
        self.assertEqual((r[0].dispositivo, r[0].modelo), ("cpu", "small"))
        self.assertEqual(sum("Falló" in e for e in estados), 2)

    def test_archivo_inexistente(self):
        with self.assertRaisesRegex(t.ErrorTranscripcion, "No existe"):
            t.transcribir([Path("no_esta.wav")], Config())


class TestAudio(unittest.TestCase):
    def test_lee_y_remuestrea(self):
        with tempfile.TemporaryDirectory() as d:
            ruta = Path(d) / "x.wav"
            s = (16000 * np.sin(2 * np.pi * 440 * np.arange(48000) / 48000)).astype(np.int16)
            with wave.open(str(ruta), "wb") as w:
                w.setnchannels(2)
                w.setsampwidth(2)
                w.setframerate(48000)
                w.writeframes(np.column_stack([s, s]).tobytes())
            y = t.leer_wav(ruta)
            self.assertEqual((y.dtype, len(y)), (np.float32, 16000))
            self.assertAlmostEqual(float(np.abs(y).max()), 16000 / 32768, delta=0.02)

    def test_wav_invalido(self):
        with tempfile.TemporaryDirectory() as d:
            ruta = Path(d) / "x.wav"
            ruta.write_bytes(b"no es un wav")
            with self.assertRaises(t.ErrorTranscripcion):
                t.leer_wav(ruta)


if __name__ == "__main__":
    unittest.main()
