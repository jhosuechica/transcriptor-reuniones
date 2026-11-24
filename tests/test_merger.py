"""Pruebas de la unión de pistas y de la diarización, sin modelos."""

import os
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import diarizer as d
import merger as m
import proceso
from diarizer import Turno
from transcriber import Palabra, Resultado, Segmento, cargar_resultado, guardar_resultado


def palabras(texto, inicio, paso=0.4):
    return [Palabra(round(inicio + i * paso, 3), round(inicio + (i + 1) * paso - 0.05, 3), " " + p, 0.9)
            for i, p in enumerate(texto.split())]


def resultado(*grupos, archivo="x.wav"):
    segs = [Segmento(g[0].inicio, g[-1].fin, "".join(p.texto for p in g).strip(), g) for g in grupos]
    return Resultado(archivo, 60.0, "small", "cpu", "int8", 1.0, segs)


def unir(mic, sis, turnos):
    return m.unir(mic, sis, turnos, "T", "2026-01-01", "Yo")


class TestAsignar(unittest.TestCase):
    def test_cambio_de_turno_a_mitad(self):
        p = palabras("uno dos tres cuatro", 0)
        h = m.asignar_hablantes(p, [Turno(0, 0.8, "A"), Turno(0.8, 2, "B")])
        self.assertEqual(h, ["A", "A", "B", "B"])

    def test_hueco_usa_el_turno_mas_cercano(self):
        p = [Palabra(5.0, 5.3, " eh", 0.9)]
        self.assertEqual(m.asignar_hablantes(p, [Turno(0, 4.8, "A"), Turno(5.9, 8, "B")]), ["A"])

    def test_lejos_de_todo_hereda(self):
        p = palabras("uno", 1) + palabras("dos", 20) + palabras("tres", 41)
        self.assertEqual(
            m.asignar_hablantes(p, [Turno(1, 2, "A"), Turno(40.9, 42, "B")]), ["A", "A", "B"])

    def test_error_de_borde_se_corrige(self):
        p = palabras("uno dos tres", 0)
        h = m.asignar_hablantes(
            p, [Turno(0, 0.38, "A"), Turno(0.65, 0.75, "B"), Turno(0.8, 2, "A")])
        self.assertEqual(h, ["A", "A", "A"])

    def test_si_de_otra_persona_se_respeta(self):
        p = palabras("uno si tres", 0)
        h = m.asignar_hablantes(
            p, [Turno(0, 0.38, "A"), Turno(0.4, 0.76, "B"), Turno(0.8, 2, "A")])
        self.assertEqual(h, ["A", "B", "A"])

    def test_sin_turnos(self):
        self.assertEqual(m.asignar_hablantes(palabras("a b", 0), []), [None, None])

    def test_muchos_turnos_coincide_con_busqueda_completa(self):
        rng = np.random.default_rng(3)
        turnos, t = [], 0.0
        for _ in range(300):
            t += rng.uniform(0, 0.4)
            f = t + rng.uniform(0.2, 3)
            turnos.append(Turno(t, f, "ABC"[rng.integers(3)]))
            t = f
        ps = [Palabra(x, x + 0.25, " x", 1) for x in np.arange(0, t, 5.3)]
        for p, h in zip(ps, m.asignar_hablantes(ps, turnos), strict=True):
            total = {}
            for x in turnos:
                s = min(p.fin, x.fin) - max(p.inicio, x.inicio)
                if s > 0:
                    total[x.hablante] = total.get(x.hablante, 0) + s
            if total:
                self.assertEqual(h, max(total, key=total.get))


class TestUnir(unittest.TestCase):
    def test_orden_y_formato(self):
        t = unir(resultado(palabras("Hola, buenos días.", 83)),
                 resultado(palabras("Buenos días a todos.", 90), palabras("Empecemos ya.", 100)),
                 [Turno(89, 95, "SPEAKER_01"), Turno(99, 105, "SPEAKER_00")])
        self.assertEqual(m.a_texto(t).splitlines(), [
            "[00:01:23] Yo: Hola, buenos días.",
            "[00:01:30] Hablante 1: Buenos días a todos.",
            "[00:01:40] Hablante 2: Empecemos ya."])

    def test_interrupcion_queda_en_su_sitio(self):
        sis = resultado(palabras("Esto es lo primero. Esto es lo segundo.", 0, 1.0))
        mic = resultado(palabras("Perdona.", 3.5))
        t = unir(mic, sis, [Turno(0, 10, "A")])
        self.assertEqual([i.hablante for i in t.intervenciones], ["h1", "yo", "h1"])

    def test_mismo_hablante_se_junta_y_se_parte(self):
        t = unir(resultado(), resultado(palabras("Uno. Dos. Tres.", 0)), [Turno(0, 5, "A")])
        self.assertEqual(len(t.intervenciones), 1)
        largo = resultado(palabras("palabra. " * 200, 0))
        t = unir(resultado(), largo, [Turno(0, 100, "A")])
        self.assertTrue(all(i.fin - i.inicio <= m.DURACION_MAX for i in t.intervenciones))
        self.assertEqual(sum(len(i.texto.split()) for i in t.intervenciones), 200)

    def test_ninguna_palabra_se_pierde(self):
        rng = np.random.default_rng(5)
        a = [palabras(f"a{i} b{i} c{i}.", i * 4 + float(rng.uniform(0, 2))) for i in range(50)]
        b = [palabras(f"d{i} e{i}", i * 4 + float(rng.uniform(0, 2))) for i in range(50)]
        t = unir(resultado(*a), resultado(*b), [Turno(i * 4, i * 4 + 4, f"S{i % 3}") for i in range(50)])
        self.assertEqual(sum(len(i.texto.split()) for i in t.intervenciones), 250)
        inicios = [i.inicio for i in t.intervenciones]
        self.assertEqual(inicios, sorted(inicios))
        self.assertEqual(list(t.hablantes), ["yo", "h1", "h2", "h3"])

    def test_eco_se_elimina_pero_no_lo_propio(self):
        sis = resultado(palabras("vamos a revisar el informe de ventas", 10))
        mic = resultado(palabras("Vamos a revisar él informe", 10.1), palabras("me parece muy bien eso", 14),
                        palabras("vamos a revisar el informe", 40))
        t = unir(mic, sis, [Turno(9, 14, "A")])
        textos = [i.texto for i in t.intervenciones if i.hablante == "yo"]
        self.assertEqual(textos, ["me parece muy bien eso", "vamos a revisar el informe"])
        self.assertIn("Se quitaron 1", t.avisos[0])

    def test_pistas_vacias_y_sin_turnos(self):
        self.assertEqual(unir(resultado(), resultado(), []).intervenciones, [])
        t = unir(resultado(), resultado(palabras("hola a todos", 0)), [])
        self.assertEqual(t.hablantes["h1"], "Hablante 1")

    def test_guardar_y_cargar(self):
        t = unir(resultado(palabras("Ñandú.", 0)), resultado(palabras("Sí.", 3)), [Turno(2, 5, "A")])
        with tempfile.TemporaryDirectory() as c:
            m.guardar(t, Path(c))
            self.assertEqual(m.cargar(Path(c)), t)
            (Path(c) / "transcripcion.json").write_text("{", encoding="utf-8")
            with self.assertRaises(m.ErrorUnion):
                m.cargar(Path(c))
        with self.assertRaises(m.ErrorUnion):
            m.cargar(Path("no_existe"))

    def test_reutilizar_transcripcion(self):
        with tempfile.TemporaryDirectory() as c:
            wav = Path(c) / "a.wav"
            wav.touch()
            self.assertIsNone(cargar_resultado(wav))
            r = resultado(palabras("hola", 0))
            guardar_resultado(r, wav)
            self.assertEqual(cargar_resultado(wav), r)
            futuro = wav.stat().st_mtime + 100
            os.utime(wav, (futuro, futuro))
            self.assertIsNone(cargar_resultado(wav))


class TestDiarizar(unittest.TestCase):
    def wav(self, carpeta, amplitud):
        ruta = Path(carpeta) / "s.wav"
        with wave.open(str(ruta), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(np.full(32000, amplitud, np.int16).tobytes())
        return ruta

    def test_casos(self):
        with tempfile.TemporaryDirectory() as c:
            with mock.patch.object(d, "disponible", return_value=False):
                with self.assertRaisesRegex(d.ErrorDiarizacion, "Falta el modelo"):
                    d.diarizar(self.wav(c, 5000))
                # Con un hablante no se necesita el modelo.
                self.assertEqual(d.diarizar(self.wav(c, 5000), 1).turnos, [Turno(0, 2, "SPEAKER_00")])
            with self.assertRaisesRegex(d.ErrorDiarizacion, "entre 1 y"):
                d.diarizar(self.wav(c, 5000), 0)
            with mock.patch.object(d, "disponible", return_value=True):
                self.assertIn("silencio", d.diarizar(self.wav(c, 0)).aviso)
                with mock.patch.object(proceso, "ejecutar", side_effect=proceso.FalloProceso("x")), \
                        self.assertRaisesRegex(d.ErrorDiarizacion, "No se pudo separar"):
                    d.diarizar(self.wav(c, 5000))

    def test_huellas_sin_repetir(self):
        import torch

        llamadas = []

        class Original:
            min_num_samples = 7

            def __call__(self, ondas, masks=None):
                llamadas.append(len(ondas))
                if masks.dim() == 3:  # (1, hablantes, tramas)
                    return np.stack([[o.sum().item() + m.sum().item() for m in ms]
                                     for o, ms in zip(ondas, masks)])[..., None]
                return np.array([[o.sum().item() + m.sum().item()] for o, m in zip(ondas, masks)])

        rapido = d.HuellasSinRepetir(Original())
        self.assertEqual(rapido.min_num_samples, 7)
        masks = torch.arange(12.0).reshape(3, 4)
        iguales = torch.ones(1, 1, 5).repeat(3, 1, 1)
        np.testing.assert_allclose(rapido(iguales, masks=masks), Original()(iguales, masks=masks))
        self.assertEqual(llamadas, [1, 3])
        # Con fragmentos distintos se comporta como el original.
        distintas = torch.arange(15.0).reshape(3, 1, 5)
        np.testing.assert_allclose(rapido(distintas, masks=masks), Original()(distintas, masks=masks))

    def test_token_invalido_da_mensaje_claro(self):
        with mock.patch("huggingface_hub.snapshot_download", side_effect=OSError("sin red")), \
                self.assertRaisesRegex(d.ErrorDiarizacion, "sin red"):
            d.descargar("x")


if __name__ == "__main__":
    unittest.main()
