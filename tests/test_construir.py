"""Pruebas del cambio de versión del ejecutable, sin construirlo."""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import construir


class TestCambiar(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        self.destino, self.nueva = d / "Transcriptor", d / "nueva" / "Transcriptor"
        for nombre, valor in (("DESTINO", self.destino), ("NUEVA", self.nueva),
                              ("ANTERIOR", d / "anterior"), ("INTENTOS", 2),
                              ("ESPERA_S", 0.01)):
            p = mock.patch.object(construir, nombre, valor)
            p.start()
            self.addCleanup(p.stop)
        salida = contextlib.redirect_stdout(io.StringIO())
        self.texto = salida.__enter__()
        self.addCleanup(salida.__exit__, None, None, None)

    def version(self, carpeta, texto):
        carpeta.mkdir(parents=True)
        (carpeta / "Transcriptor.exe").write_text(texto)

    def leer(self):
        return (self.destino / "Transcriptor.exe").read_text()

    def test_primera_vez_y_reemplazo(self):
        for texto in ("v1", "v2"):
            self.version(self.nueva, texto)
            self.assertEqual(construir.cambiar(), 0)
            self.assertEqual(self.leer(), texto)
        self.assertEqual([p.name for p in self.destino.parent.iterdir()], ["Transcriptor"])

    def test_abierta_conserva_las_dos(self):
        self.version(self.destino, "v1")
        self.version(self.nueva, "v2")
        with open(self.destino / "Transcriptor.exe"):
            self.assertTrue(construir.en_uso(self.destino))
            self.assertEqual(construir.cambiar(), 1)
            self.assertEqual(self.leer(), "v1")
        self.assertIn("--cambiar", self.texto.getvalue())
        self.assertFalse(construir.en_uso(self.destino))
        self.assertEqual(construir.cambiar(), 0)
        self.assertEqual(self.leer(), "v2")

    def test_si_falla_a_medias_vuelve_la_anterior(self):
        self.version(self.destino, "v1")
        self.version(self.nueva, "v2")
        with open(self.nueva / "Transcriptor.exe"):
            self.assertEqual(construir.cambiar(), 1)
        self.assertEqual(self.leer(), "v1")

    def test_bloqueo_pasajero_se_reintenta(self):
        self.version(self.nueva, "v1")
        original, fallos = Path.rename, [PermissionError("antivirus")]

        def rename(ruta, destino):
            if fallos:
                raise fallos.pop()
            return original(ruta, destino)

        with mock.patch.object(Path, "rename", rename):
            self.assertEqual(construir.cambiar(), 0)
        self.assertEqual(self.leer(), "v1")

    def test_abierta_no_empieza_a_construir(self):
        self.version(self.destino, "v1")
        with (open(self.destino / "Transcriptor.exe"),
              mock.patch.object(construir.subprocess, "call") as llamada,
              mock.patch.object(sys, "argv", ["construir.py"])):
            self.assertEqual(construir.main(), 1)
        llamada.assert_not_called()

    def test_construccion_fallida_no_toca_la_actual(self):
        self.version(self.destino, "v1")
        with (mock.patch.object(construir.subprocess, "call", return_value=1),
              mock.patch.object(sys, "argv", ["construir.py"])):
            self.assertEqual(construir.main(), 1)
        self.assertEqual(self.leer(), "v1")

    def test_sin_version_nueva(self):
        self.version(self.destino, "v1")
        self.assertEqual(construir.cambiar(), 1)
        self.assertEqual(self.leer(), "v1")


if __name__ == "__main__":
    unittest.main()
