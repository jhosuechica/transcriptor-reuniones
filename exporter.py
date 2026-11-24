"""Exporta la transcripción a .txt, .docx y .srt.

Los archivos se generan siempre desde transcripcion.json. Si el usuario editó
uno a mano (por ejemplo, el .docx en Word), antes de reemplazarlo se guarda
una copia: se detecta comparando su huella con la de la última exportación.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import textwrap
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

import merger
from merger import Transcripcion, hms

REGISTRO = "exportados.json"
# Subtítulos: dos líneas de 42 caracteres como máximo, lo habitual en vídeo.
ANCHO_SRT = 42
LINEAS_SRT = 2
DURACION_MIN_SRT = 1.0      # s; menos no da tiempo a leerlo
GRIS = RGBColor(0x70, 0x70, 0x70)
MESES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
         "septiembre", "octubre", "noviembre", "diciembre")
# Caracteres que no admite el XML de Word (incluye saltos de línea).
NO_VALIDOS = re.compile(r"[\x00-\x1f\x7f\ud800-\udfff￾￿]")

log = logging.getLogger(__name__)


class ErrorExportacion(Exception):
    """Error al exportar, con un mensaje apto para mostrar al usuario."""


@dataclass
class Exportacion:
    archivos: list[Path] = field(default_factory=list)
    # Copias de los archivos que el usuario había editado.
    copias: list[Path] = field(default_factory=list)


# --------------------------------------------------------------------------
# Contenido
# --------------------------------------------------------------------------


def _limpio(texto: str) -> str:
    return re.sub(r"\s+", " ", NO_VALIDOS.sub(" ", texto)).strip()


def _fecha(iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


def _cabecera(t: Transcripcion) -> list[tuple[str, str]]:
    datos = []
    f = _fecha(t.inicio)
    if f:
        datos.append(("Fecha", f"{f.day} de {MESES[f.month - 1]} de {f.year}, {f:%H:%M}"))
    # Solo quien habla; sin repetir si dos hablantes se llaman igual.
    nombres = dict.fromkeys(_limpio(t.hablantes[i.hablante]) for i in t.intervenciones)
    if nombres:
        datos.append(("Participantes", ", ".join(nombres)))
    return datos


def a_txt(t: Transcripcion) -> bytes:
    lineas = [_limpio(t.titulo), *(f"{c}: {v}" for c, v in _cabecera(t)), ""]
    lineas += [f"AVISO: {_limpio(a)}" for a in t.avisos]
    if t.avisos:
        lineas.append("")
    if not t.intervenciones:
        lineas.append("No se detectó voz en la grabación.")
    for i in t.intervenciones:
        lineas.append(f"[{hms(i.inicio)}] {_limpio(t.hablantes[i.hablante])}: {_limpio(i.texto)}")
    return ("\r\n".join(lineas) + "\r\n").encode("utf-8")


def _numero_de_pagina(parrafo) -> None:
    # python-docx no ofrece campos: se crea el campo PAGE a mano.
    campo = OxmlElement("w:fldSimple")
    campo.set(qn("w:instr"), "PAGE")
    parrafo.alignment = WD_ALIGN_PARAGRAPH.CENTER
    parrafo._p.append(campo)


def a_docx(t: Transcripcion) -> bytes:
    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name, normal.font.size = "Calibri", Pt(11)
    # Idioma español, para que Word no marque todo como falta de ortografía.
    rpr = normal.element.get_or_add_rPr()
    idioma = rpr.find(qn("w:lang"))
    if idioma is None:
        idioma = OxmlElement("w:lang")
        rpr.append(idioma)
    idioma.set(qn("w:val"), "es-ES")

    titulo = _limpio(t.titulo)
    p = doc.core_properties
    p.title, p.author, p.comments, p.language = titulo, "", "", "es-ES"
    f = _fecha(t.inicio)
    # Word guarda las fechas en UTC.
    p.created = p.modified = (
        f.astimezone(timezone.utc) if f else datetime.now(timezone.utc)
    ).replace(tzinfo=None, microsecond=0)

    doc.add_heading(titulo, level=1)
    for campo, valor in _cabecera(t):
        parrafo = doc.add_paragraph()
        parrafo.paragraph_format.space_after = Pt(0)
        parrafo.add_run(f"{campo}: ").bold = True
        parrafo.add_run(valor)
    for aviso in t.avisos:
        doc.add_paragraph().add_run(f"Aviso: {_limpio(aviso)}").italic = True
    doc.add_heading("Transcripción", level=2)
    if not t.intervenciones:
        doc.add_paragraph("No se detectó voz en la grabación.")
    for i in t.intervenciones:
        parrafo = doc.add_paragraph()
        parrafo.paragraph_format.space_after = Pt(6)
        marca = parrafo.add_run(f"[{hms(i.inicio)}] ")
        marca.font.color.rgb, marca.font.size = GRIS, Pt(9)
        parrafo.add_run(f"{_limpio(t.hablantes[i.hablante])}: ").bold = True
        parrafo.add_run(_limpio(i.texto))
    _numero_de_pagina(doc.sections[0].footer.paragraphs[0])

    salida = io.BytesIO()
    doc.save(salida)
    return salida.getvalue()


def _hms_srt(segundos: float) -> str:
    ms = max(0, round(segundos * 1000))
    return f"{ms // 3_600_000:02}:{ms // 60_000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


def _repartir(texto: str) -> list[list[str]]:
    """Divide el texto en subtítulos de tamaño parecido, sin palabras sueltas."""
    lineas = textwrap.wrap(texto, ANCHO_SRT)
    minimo = -(-len(lineas) // LINEAS_SRT)
    if minimo > 1:
        # El ancho más pequeño que no aumenta el número de subtítulos.
        for ancho in range(len(texto) // minimo, ANCHO_SRT * LINEAS_SRT + 1):
            grupos = [textwrap.wrap(p, ANCHO_SRT) for p in textwrap.wrap(texto, ancho)]
            if len(grupos) <= minimo and all(len(g) <= LINEAS_SRT for g in grupos):
                return grupos
    return [lineas[k:k + LINEAS_SRT] for k in range(0, len(lineas), LINEAS_SRT)]


def a_srt(t: Transcripcion) -> bytes:
    trozos: list[list] = []     # [inicio, fin, líneas]
    for i in t.intervenciones:
        nombre = _limpio(t.hablantes[i.hablante])
        for frase in i.frases:
            # "-->" separa los tiempos en este formato: no puede ir en el texto.
            texto = _limpio(f"{nombre}: {frase.texto}").replace("-->", "→")
            grupos = _repartir(texto)
            # Una frase larga se reparte en varios subtítulos según su longitud.
            total = sum(len(linea) for g in grupos for linea in g) or 1
            actual = frase.inicio
            for g in grupos:
                fin = actual + (frase.fin - frase.inicio) * sum(map(len, g)) / total
                trozos.append([actual, fin, g])
                actual = fin
    trozos.sort(key=lambda x: (x[0], x[1]))
    for k, trozo in enumerate(trozos):
        # Los muy cortos se alargan, sin pisar el siguiente.
        limite = trozos[k + 1][0] if k + 1 < len(trozos) else float("inf")
        trozo[1] = max(trozo[1], min(trozo[0] + DURACION_MIN_SRT, limite))
    return "".join(
        f"{n}\r\n{_hms_srt(ini)} --> {_hms_srt(fin)}\r\n" + "\r\n".join(g) + "\r\n\r\n"
        for n, (ini, fin, g) in enumerate(trozos, 1)
    ).encode("utf-8")


FORMATOS: dict[str, Callable[[Transcripcion], bytes]] = {
    "txt": a_txt, "docx": a_docx, "srt": a_srt,
}


# --------------------------------------------------------------------------
# Escritura
# --------------------------------------------------------------------------


def _huella(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


def _leer_registro(carpeta: Path) -> dict[str, str]:
    try:
        d = json.loads((carpeta / REGISTRO).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        # Sin registro, todo archivo existente se trata como editado.
        return {}


def _nombre_copia(ruta: Path) -> Path:
    base = f"{ruta.stem} (editado {datetime.now():%Y-%m-%d %H-%M-%S})"
    copia, n = ruta.with_name(base + ruta.suffix), 2
    while copia.exists():
        copia, n = ruta.with_name(f"{base} {n}{ruta.suffix}"), n + 1
    return copia


def _reemplazar(ruta: Path, datos: bytes, huella: str | None) -> Path | None:
    """Escribe el archivo. Devuelve la copia del anterior si estaba editado."""
    temporal = ruta.with_name(ruta.name + ".tmp")
    copia = None
    try:
        temporal.write_bytes(datos)
        if ruta.exists():
            anterior = ruta.read_bytes()
            if anterior != datos and _huella(anterior) != huella:
                copia = _nombre_copia(ruta)
                copia.write_bytes(anterior)
        try:
            temporal.replace(ruta)
        except OSError:
            # El original sigue intacto: la copia sobra.
            if copia:
                copia.unlink(missing_ok=True)
            raise
    finally:
        temporal.unlink(missing_ok=True)
    return copia


def exportar(
    t: Transcripcion, carpeta: Path, formatos: tuple[str, ...] = tuple(FORMATOS)
) -> Exportacion:
    """Genera los archivos. Un fallo en uno no impide crear los demás."""
    registro = _leer_registro(carpeta)
    resultado, fallos = Exportacion(), []
    for formato in formatos:
        ruta = carpeta / f"transcripcion.{formato}"
        datos = FORMATOS[formato](t)
        try:
            copia = _reemplazar(ruta, datos, registro.get(ruta.name))
        except PermissionError:
            log.exception("Sin permiso para escribir %s", ruta)
            fallos.append(f"{ruta.name}: está abierto en otro programa o es de solo lectura")
            continue
        except OSError as e:
            log.exception("No se pudo escribir %s", ruta)
            fallos.append(f"{ruta.name}: {e}")
            continue
        registro[ruta.name] = _huella(datos)
        resultado.archivos.append(ruta)
        if copia:
            resultado.copias.append(copia)
    try:
        (carpeta / REGISTRO).write_text(json.dumps(registro, indent=1), encoding="utf-8")
    except OSError as e:
        # Lo peor que ocurre es una copia de más la próxima vez.
        log.warning("No se pudo guardar %s: %s", REGISTRO, e)
    if fallos:
        hechos = ", ".join(a.name for a in resultado.archivos) or "ninguno"
        copias = "".join(f"\nTu versión editada se guardó en {c.name}." for c in resultado.copias)
        raise ErrorExportacion(
            "No se pudo exportar " + "; ".join(fallos)
            + f".\nExportados: {hechos}.{copias}"
            + "\nCierra el archivo y vuelve a exportar; los datos no se han perdido."
        )
    return resultado


def renombrar(carpeta: Path, cambios: dict[str, str]) -> tuple[Exportacion, list[str]]:
    """Cambia los nombres, guarda la transcripción y regenera los archivos."""
    t = merger.cargar(carpeta)
    avisos = merger.renombrar(t, cambios)
    merger.guardar(t, carpeta)
    return exportar(t, carpeta), avisos
