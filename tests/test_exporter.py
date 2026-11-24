"""Pruebas de la exportación y del cambio de nombres."""

import io
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from docx import Document

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import exporter as e
import merger as m
from merger import Frase, Intervencion, Transcripcion


def transcripcion(**cambios):
    datos = dict(
        titulo="Reunión: añadir índices",
        inicio="2026-11-23T20:36:07.398-05:00",
        hablantes={"yo": "Yo", "h1": "Hablante 1", "h2": "Hablante 2"},
        intervenciones=[
            Intervencion("yo", 83.2, 86, [Frase(83.2, 86, "Empezamos, ¿no? Hablante 1 presenta.")]),
            Intervencion("h1", 90.5, 95, [Frase(90.5, 92, "Sí."), Frase(92, 95, "Voy.")]),
            Intervencion("h2", 3725, 3740, [Frase(3725, 3740, "palabra " * 40)]),
            Intervencion("h1", 3741, 3742, [Frase(3741, 3742, "Fin.")]),
        ],
    )
    return Transcripcion(**(datos | cambios))


def bloques(srt: bytes):
    """Lee el SRT como lo haría un reproductor: (inicio, fin, texto)."""
    salida = []
    for n, b in enumerate(srt.decode("utf-8").replace("\r\n", "\n").strip().split("\n\n"), 1):
        numero, tiempos, *texto = b.split("\n")
        assert numero == str(n), b
        x = re.fullmatch(r"(\d\d):(\d\d):(\d\d),(\d{3}) --> (\d\d):(\d\d):(\d\d),(\d{3})", tiempos)
        v = [int(g) for g in x.groups()]
        salida.append((v[0] * 3600 + v[1] * 60 + v[2] + v[3] / 1000,
                       v[4] * 3600 + v[5] * 60 + v[6] + v[7] / 1000, texto))
    return salida


class TestTxt(unittest.TestCase):
    def test_formato_pedido(self):
        texto = e.a_txt(transcripcion()).decode("utf-8")
        self.assertIn("\r\n[00:01:23] Yo: Empezamos, ¿no? Hablante 1 presenta.\r\n", texto)
        self.assertIn("\r\n[00:01:30] Hablante 1: Sí. Voy.\r\n", texto)
        self.assertIn("Fecha: 23 de noviembre de 2026, 20:36", texto)
        self.assertIn("Participantes: Yo, Hablante 1, Hablante 2\r\n", texto)

    def test_sin_voz_ni_fecha(self):
        texto = e.a_txt(transcripcion(intervenciones=[], inicio="")).decode("utf-8")
        self.assertIn("No se detectó voz", texto)
        self.assertNotIn("Fecha", texto)


class TestDocx(unittest.TestCase):
    def test_contenido(self):
        doc = Document(io.BytesIO(e.a_docx(transcripcion(avisos=["Ojo"]))))
        parrafos = [p.text for p in doc.paragraphs]
        self.assertEqual(parrafos[0], "Reunión: añadir índices")
        self.assertIn("[00:01:30] Hablante 1: Sí. Voy.", parrafos)
        self.assertIn("Aviso: Ojo", parrafos)
        self.assertEqual(doc.core_properties.author, "")
        self.assertEqual(doc.core_properties.created.hour, 1)   # 20:36 -05:00 en UTC

    def test_caracteres_que_word_no_admite(self):
        t = transcripcion(titulo="a\x00b\ud800")
        t.intervenciones[0].frases[0].texto = "uno\x0bdos & <tres>￾"
        doc = Document(io.BytesIO(e.a_docx(t)))
        self.assertIn("[00:01:23] Yo: uno dos & <tres>", [p.text for p in doc.paragraphs])
        e.a_txt(t), e.a_srt(t)

    def test_sin_voz(self):
        doc = Document(io.BytesIO(e.a_docx(transcripcion(intervenciones=[]))))
        self.assertIn("No se detectó voz en la grabación.", [p.text for p in doc.paragraphs])


class TestSrt(unittest.TestCase):
    def test_tiempos_y_texto(self):
        b = bloques(e.a_srt(transcripcion()))
        self.assertEqual(b[0][:2], (83.2, 86.0))
        self.assertEqual(b[1], (90.5, 92.0, ["Hablante 1: Sí."]))

    def test_frase_larga_se_reparte(self):
        b = [x for x in bloques(e.a_srt(transcripcion())) if 3725 <= x[0] < 3741]
        self.assertGreater(len(b), 1)
        self.assertEqual(b[0][0], 3725.0)
        # El último trozo es corto y se alarga hasta 1 s, sin llegar al siguiente.
        self.assertTrue(3740.0 <= b[-1][1] <= 3741.0)
        self.assertGreaterEqual(b[-1][1] - b[-1][0], e.DURACION_MIN_SRT - 0.001)
        for _, _, lineas in b:
            self.assertLessEqual(len(lineas), e.LINEAS_SRT)
            self.assertTrue(all(len(x) <= e.ANCHO_SRT for x in lineas))
        for x, y in zip(b, b[1:]):
            self.assertAlmostEqual(x[1], y[0], places=3)
        texto = " ".join(x for _, _, lineas in b for x in lineas)
        self.assertEqual(texto.count("palabra"), 40)

    def test_no_quedan_palabras_sueltas(self):
        texto = "Ana: vamos a revisar el avance de todo el proyecto. " + "una palabra más " * 25
        for n in range(20, 400):
            largo = texto[:n].strip()
            grupos = e._repartir(largo)
            self.assertEqual(" ".join(x for g in grupos for x in g), largo)
            self.assertTrue(all(len(g) <= e.LINEAS_SRT and max(map(len, g)) <= e.ANCHO_SRT
                                for g in grupos))
            tam = [sum(map(len, g)) for g in grupos]
            self.assertGreater(min(tam), max(tam) / 4, grupos)

    def test_corto_se_alarga_sin_pisar_el_siguiente(self):
        t = transcripcion(intervenciones=[
            Intervencion("h1", 1, 1.2, [Frase(1, 1.2, "Sí.")]),
            Intervencion("h2", 1.6, 1.7, [Frase(1.6, 1.7, "No.")]),
        ])
        self.assertEqual([x[:2] for x in bloques(e.a_srt(t))], [(1.0, 1.6), (1.6, 2.6)])

    def test_flecha_y_saltos_no_rompen_el_archivo(self):
        t = transcripcion()
        t.intervenciones[0].frases[0].texto = "a --> b\n\nc"
        self.assertEqual(bloques(e.a_srt(t))[0][2], ["Yo: a → b c"])

    def test_sin_voz(self):
        self.assertEqual(e.a_srt(transcripcion(intervenciones=[])), b"")


class TestExportar(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_crea_los_tres_y_repetir_no_deja_copias(self):
        for _ in range(2):
            r = e.exportar(transcripcion(), self.dir)
            self.assertEqual([a.name for a in r.archivos],
                             ["transcripcion.txt", "transcripcion.docx", "transcripcion.srt"])
            self.assertEqual(r.copias, [])
        self.assertEqual(len(list(self.dir.iterdir())), 4)

    def test_lo_editado_a_mano_se_conserva(self):
        e.exportar(transcripcion(), self.dir)
        ruta = self.dir / "transcripcion.txt"
        ruta.write_text("mis correcciones", encoding="utf-8")
        r = e.exportar(transcripcion(titulo="Otro"), self.dir)
        self.assertEqual([c.read_text(encoding="utf-8") for c in r.copias], ["mis correcciones"])
        self.assertIn("Otro", ruta.read_text(encoding="utf-8"))
        # La copia no se vuelve a duplicar.
        self.assertEqual(e.exportar(transcripcion(), self.dir).copias, [])

    def test_archivo_previo_sin_registro_se_conserva(self):
        (self.dir / "transcripcion.srt").write_text("ajeno")
        self.assertEqual(len(e.exportar(transcripcion(), self.dir).copias), 1)

    def test_archivo_abierto_no_impide_los_demas(self):
        e.exportar(transcripcion(), self.dir)
        (self.dir / "transcripcion.docx").write_bytes(b"editado")
        original = Path.replace

        def replace(ruta, destino):
            if destino.suffix == ".docx":
                raise PermissionError("en uso")
            return original(ruta, destino)

        with mock.patch.object(Path, "replace", replace), self.assertLogs("exporter"):
            with self.assertRaisesRegex(e.ErrorExportacion, "transcripcion.docx.*abierto"):
                e.exportar(transcripcion(titulo="Nuevo"), self.dir)
        self.assertIn("Nuevo", (self.dir / "transcripcion.txt").read_text(encoding="utf-8"))
        self.assertTrue((self.dir / "transcripcion.srt").exists())
        self.assertEqual((self.dir / "transcripcion.docx").read_bytes(), b"editado")
        # Sin restos: ni temporales ni copias de algo que no se reemplazó.
        self.assertEqual([p for p in self.dir.iterdir() if ".tmp" in p.name or "editado" in p.name], [])
        # Al cerrarlo, la versión editada sí se guarda.
        self.assertEqual(len(e.exportar(transcripcion(), self.dir).copias), 1)

    def test_bloqueo_real_de_windows(self):
        e.exportar(transcripcion(), self.dir)
        # open() de Python no comparte el borrado: bloquea igual que Word.
        with open(self.dir / "transcripcion.docx", "rb"), self.assertLogs("exporter"):
            with self.assertRaises(e.ErrorExportacion):
                e.exportar(transcripcion(titulo="X"), self.dir)
        e.exportar(transcripcion(titulo="X"), self.dir)

    def test_renombrar_cambia_todos_los_archivos(self):
        m.guardar(transcripcion(), self.dir)
        e.renombrar(self.dir, {"h1": "Ana Muñoz"})
        txt = (self.dir / "transcripcion.txt").read_text(encoding="utf-8")
        srt = (self.dir / "transcripcion.srt").read_text(encoding="utf-8")
        docx = "\n".join(p.text for p in Document(str(self.dir / "transcripcion.docx")).paragraphs)
        for texto in (txt, srt, docx):
            self.assertIn("Ana Muñoz: ", texto)
            self.assertNotIn("Hablante 1:", texto)
        # Lo que alguien dijo no se toca, aunque mencione la etiqueta.
        self.assertIn("Yo: Empezamos, ¿no? Hablante 1 presenta.", txt)
        self.assertEqual(m.cargar(self.dir).hablantes["h1"], "Ana Muñoz")


class TestNombres(unittest.TestCase):
    def test_intercambio_y_espacios(self):
        t = transcripcion()
        m.renombrar(t, {"h1": "Hablante 2", "h2": "  Ana   Paz "})
        self.assertEqual(t.hablantes, {"yo": "Yo", "h1": "Hablante 2", "h2": "Ana Paz"})

    def test_repetidos_avisan(self):
        t = transcripcion()
        self.assertEqual(len(m.renombrar(t, {"h1": "Ana", "h2": "Ana"})), 1)
        self.assertIn("Participantes: Yo, Ana\r\n", e.a_txt(t).decode())

    def test_invalido_no_cambia_nada(self):
        for cambios in ({"h1": "Ana", "h2": "  "}, {"h1": "Ana", "h9": "X"}, {"h1": "x" * 61},
                        {"h1": "a\x00b"}):
            t = transcripcion()
            with self.assertRaises(m.ErrorUnion):
                m.renombrar(t, cambios)
            self.assertEqual(t.hablantes["h1"], "Hablante 1")

    def test_resolver(self):
        t = transcripcion()
        self.assertEqual([m.resolver(t, x) for x in ("H1", "hablante 2", "yo")], ["h1", "h2", "yo"])
        m.renombrar(t, {"h1": "Ana", "h2": "Ana"})
        for x in ("ana", "nadie"):
            with self.assertRaises(m.ErrorUnion):
                m.resolver(t, x)

    def test_se_heredan_aunque_cambie_el_orden(self):
        vieja = transcripcion(hablantes={"yo": "Nora", "h1": "Ana", "h2": "Luis"})
        nueva = transcripcion()
        for i in nueva.intervenciones:
            i.hablante = {"h1": "h2", "h2": "h1", "yo": "yo"}[i.hablante]
        self.assertEqual(m.heredar_nombres(nueva, vieja), [])
        self.assertEqual(nueva.hablantes, {"yo": "Nora", "h1": "Luis", "h2": "Ana"})

    def test_voces_mezcladas_no_se_heredan(self):
        vieja = transcripcion(hablantes={"yo": "Yo", "h1": "Ana", "h2": "Hablante 2"})
        nueva = transcripcion(hablantes={"yo": "Yo", "h1": "Hablante 1"})
        for i in nueva.intervenciones:
            i.hablante = "h1" if i.hablante == "h2" else i.hablante
        self.assertIn("Ana", m.heredar_nombres(nueva, vieja)[0])
        self.assertEqual(nueva.hablantes["h1"], "Hablante 1")

    def test_sin_nombres_propios_no_hay_avisos(self):
        nueva = transcripcion(intervenciones=[])
        self.assertEqual(m.heredar_nombres(nueva, transcripcion()), [])


if __name__ == "__main__":
    unittest.main()
