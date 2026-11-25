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


def _ok(con, rutas, intento, _v, _i):
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


def _gpu_muere_cpu_ok(con, rutas, intento, v, i):
    if intento.dispositivo == "cuda":
        os._exit(7)
    _ok(con, rutas, intento, v, i)


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


class TestEjemplo(unittest.TestCase):
    def test_reconoce_el_eco(self):
        self.assertTrue(t.es_ejemplo("¿Hay dudas? No, ninguna.", "es"))
        self.assertTrue(t.es_ejemplo("bien empezamos primero los acuerdos", "es"))
        self.assertTrue(t.es_ejemplo("Any doubts? No, none.", "en"))
        self.assertTrue(t.es_ejemplo(t.EJEMPLOS["es"], "es"))

    def test_no_quita_lo_que_se_dijo(self):
        for texto in ("¿Hay dudas?", "No, ninguna.", "Bien, empezamos.", "Bien", "", "...",
                      "¿Hay dudas? No, gracias.", "Bien, empezamos. Primero, las tareas."):
            self.assertFalse(t.es_ejemplo(texto, "es"), texto)
        self.assertFalse(t.es_ejemplo("¿Hay dudas? No, ninguna.", "en"))

    def test_se_da_en_el_idioma_de_cada_tramo_y_se_filtra(self):
        vistos = []

        class Modelo:
            model = SimpleNamespace(device="cpu")

            def transcribe(self, _a, **k):
                vistos.append(k["hotwords"])
                textos = ["Any doubts? No, none.", "Real text."]
                segs = (SimpleNamespace(text=x, start=n, end=n + 1, words=[])
                        for n, x in enumerate(textos))
                return segs, SimpleNamespace(duration=9.0, duration_after_vad=9.0)

        r = t._transcribir_audio(Modelo(), None, "Kubernetes, Ana", 1, lambda _: None, "en")
        self.assertEqual([s.texto for s in r], ["Real text."])
        self.assertEqual(vistos, [t.EJEMPLOS["en"] + " Kubernetes, Ana"])


class TestIdiomas(unittest.TestCase):
    @staticmethod
    def p(es):
        return {"es": es, "en": 1 - es}

    def test_decide_cada_tramo(self):
        tramos = [(0, 3), (3.5, 6), (6.5, 9)]
        self.assertEqual(
            t.decidir_idiomas(tramos, [self.p(.99), self.p(.02), self.p(.9)]), ["es", "en", "es"])

    def test_dudosos_y_no_analizados_siguen_al_vecino_mas_proximo(self):
        tramos = [(0, 3), (3.2, 5), (5.9, 6.3), (6.5, 9), (9.4, 9.8)]
        probs = [self.p(.99), self.p(.45), None, self.p(.01), None]
        self.assertEqual(t.decidir_idiomas(tramos, probs), ["es", "es", "en", "en", "en"])

    def test_si_ninguno_es_claro_gana_el_mas_probable(self):
        self.assertEqual(
            t.decidir_idiomas([(0, 3), (4, 6)], [self.p(.6), self.p(.4)]), ["es", "en"])
        self.assertEqual(
            t.decidir_idiomas([(0, 3), (3.1, 3.4)], [self.p(.6), None]), ["es", "es"])
        self.assertEqual(t.decidir_idiomas([], []), [])

    def detectar(self, valores, segundos=3.0):
        vistos = []
        modelo = SimpleNamespace(
            feature_extractor=lambda x: np.zeros((128, int(len(x) / 160)), np.float32),
            encode=lambda r: vistos.append(r.shape),
            model=SimpleNamespace(detect_language=lambda _: [[(f"<|{k}|>", v) for k, v in valores]]))
        return t._probabilidades(modelo, np.zeros(int(segundos * 16000)), ("es", "en")), vistos

    def test_solo_cuentan_los_idiomas_esperados(self):
        p, vistos = self.detectar([("pt", .6), ("es", .3), ("en", .1)])
        self.assertEqual({k: round(v, 3) for k, v in p.items()}, {"es": .75, "en": .25})
        self.assertEqual(vistos, [(128, 1000)])
        self.assertEqual(self.detectar([("es", 1.0), ("en", 0.0)], 13)[1], [(128, 1400)])
        self.assertEqual(self.detectar([("es", 1.0), ("en", 0.0)], 40)[1], [(128, 3000)])

    def test_valores_imposibles_pasan_al_siguiente_intento(self):
        for malos in ([("es", -1.7e38), ("en", 0.0)], [("es", float("nan")), ("en", 1.0)],
                      [("es", 0.0), ("en", 0.0)], [("es", 5.0), ("en", 0.0)]):
            with self.assertRaises(t.TextoCorrupto):
                self.detectar(malos)

    def test_no_se_analizan_los_trozos_cortos(self):
        f = 16000
        voz = [{"start": 0, "end": 3 * f}, {"start": 3 * f + 800, "end": 4 * f}]
        with (mock.patch("faster_whisper.vad.get_speech_timestamps",
                         side_effect=[voz, voz[1:], []]) as cortar,
              mock.patch.object(t, "_probabilidades", return_value=self.p(.1)) as p):
            audio = np.zeros(5 * f)
            self.assertEqual(t._separar_idiomas(None, audio, ("es", "en"), lambda _: None),
                             [(0.0, 4.0, "en")])
            self.assertEqual(p.call_count, 1)
            opciones = cortar.call_args.args[1]
            self.assertEqual((opciones.speech_pad_ms, opciones.min_silence_duration_ms), (300, 250))
            # Si solo hay trozos cortos, se analizan.
            self.assertEqual(
                t._separar_idiomas(None, audio, ("es", "en"), lambda _: None)[0][2], "en")
            self.assertEqual(t._separar_idiomas(None, audio, ("es", "en"), lambda _: None), [])

    def test_junta_sin_pasar_de_30_segundos(self):
        tramos = [(0, 10), (10.5, 20), (20.5, 29), (29.5, 35), (40, 41), (41.5, 43)]
        self.assertEqual(
            t.juntar_tramos(tramos, ["es", "es", "es", "es", "es", "en"]),
            [(0, 20, "es"), (20.5, 35, "es"), (40, 41, "es"), (41.5, 43, "en")])

    def transcribir(self, lote, tramos, idioma="auto", dispositivo="cuda", texto="Texto."):
        llamadas, avances = [], []

        class Modelo:
            model = SimpleNamespace(device=dispositivo)

            def transcribe(self, _audio, **k):
                llamadas.append(k)
                clips = k.get("clip_timestamps")
                if clips and not isinstance(clips[0], dict):
                    clips = [{"start": a, "end": b} for a, b in zip(clips[::2], clips[1::2])]
                segs = [SimpleNamespace(text=texto and f"{k['language']} {texto}", start=c["start"],
                                        end=c["end"], words=[]) for c in clips or [{"start": 0, "end": 30}]]
                return iter(segs), SimpleNamespace(duration=100.0, duration_after_vad=0.0)

        modelo = Modelo()
        with (mock.patch.object(t, "_separar_idiomas", return_value=tramos) as separar,
              mock.patch("faster_whisper.BatchedInferencePipeline",
                         lambda m: SimpleNamespace(transcribe=m.transcribe, model=m))):
            r = t._transcribir_audio(modelo, None, "", lote, avances.append, idioma)
        return r, llamadas, avances, separar

    TRAMOS = [(0.0, 20.0, "es"), (21.0, 60.0, "en"), (61.0, 99.0, "es")]

    def test_transcribe_cada_idioma_y_ordena(self):
        for lote in (8, 1):
            r, llamadas, avances, _ = self.transcribir(lote, self.TRAMOS)
            self.assertEqual([s.texto for s in r], ["es Texto.", "en Texto.", "es Texto."])
            self.assertEqual([k["language"] for k in llamadas], ["es", "en"])
            self.assertNotIn("vad_filter", llamadas[0])
            self.assertEqual(avances, sorted(avances))
            self.assertTrue(all(0.3 <= a <= 1.0 for a in avances), avances)
        self.assertEqual(llamadas[0]["clip_timestamps"], [0.0, 20.0, 61.0, 99.0])

    def test_un_solo_idioma_no_detecta_nada(self):
        for idioma in ("es", "en"):
            r, llamadas, _, separar = self.transcribir(8, self.TRAMOS, idioma)
            separar.assert_not_called()
            self.assertEqual([k["language"] for k in llamadas], [idioma])
            self.assertTrue(llamadas[0]["vad_filter"])

    def test_sin_voz(self):
        r, llamadas, _, _ = self.transcribir(8, [])
        self.assertEqual((r, llamadas), ([], []))

    def test_vacio_con_voz_tambien_es_sospechoso(self):
        with self.assertRaises(t.TextoCorrupto):
            self.transcribir(8, [(0.0, 20.0, "en")], texto="")

    def test_idioma_desconocido(self):
        with tempfile.TemporaryDirectory() as d:
            wav = Path(d) / "a.wav"
            wav.write_bytes(b"")
            with self.assertRaisesRegex(t.ErrorTranscripcion, "auto, es, en"):
                t.transcribir([wav], Config(idioma="fr"))

    def test_por_defecto_se_aceptan_los_dos(self):
        self.assertEqual(t.IDIOMAS[Config().idioma], ("es", "en"))


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

        def con_falso(*a, **k):
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
