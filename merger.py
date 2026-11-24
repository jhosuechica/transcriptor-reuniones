"""Une las dos pistas y la diarización en una sola transcripción cronológica.

Pasos: asignar un hablante a cada palabra -> agrupar palabras en frases ->
ordenar las frases de ambas pistas -> juntar las seguidas del mismo hablante.
Trabajar por frases permite que una interrupción quede en su sitio.
"""

from __future__ import annotations

import bisect
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from diarizer import Turno
from transcriber import Palabra, Resultado

ID_USUARIO = "yo"
PAUSA_FRASE = 0.8           # s de silencio que cierran una frase
PAUSA_INTERVENCION = 2.0    # s entre frases que separan intervenciones
DURACION_MAX = 30.0         # s de una intervención antes de partirla
DISTANCIA_MAX = 1.0         # s a los que una palabra se asigna al turno más cercano
MARGEN_ECO = 1.0            # s alrededor de una frase donde se busca su eco
PALABRAS_MIN_ECO = 4
PARECIDO_ECO = 0.8
PARECIDO_VOZ = 0.7          # coincidencia para dar por igual a un hablante
LARGO_NOMBRE = 60
FIN_DE_FRASE = (".", "?", "!", "…")


class ErrorUnion(Exception):
    """Error al unir o guardar, con un mensaje apto para mostrar al usuario."""


@dataclass
class Frase:
    inicio: float
    fin: float
    texto: str


@dataclass
class Intervencion:
    hablante: str            # identificador interno, no el nombre visible
    inicio: float
    fin: float
    frases: list[Frase] = field(default_factory=list)

    @property
    def texto(self) -> str:
        return " ".join(f.texto for f in self.frases)


@dataclass
class Transcripcion:
    titulo: str
    inicio: str
    hablantes: dict[str, str]           # identificador -> nombre visible
    intervenciones: list[Intervencion]
    avisos: list[str] = field(default_factory=list)
    # Alguien corrigió el texto o los hablantes a mano.
    corregida: bool = False


# --------------------------------------------------------------------------
# Hablante de cada palabra
# --------------------------------------------------------------------------


def _palabras(r: Resultado) -> list[Palabra]:
    lista = []
    for s in r.segmentos:
        # Si faltan las palabras, el segmento entero cuenta como una.
        lista += s.palabras or [Palabra(s.inicio, s.fin, " " + s.texto, 1.0)]
    return lista


def asignar_hablantes(palabras: list[Palabra], turnos: list[Turno]) -> list[str | None]:
    """Devuelve el hablante de cada palabra según el turno que más la cubre."""
    turnos = sorted(turnos, key=lambda t: t.inicio)
    if not turnos:
        return [None] * len(palabras)
    inicios = [t.inicio for t in turnos]
    etiquetas: list[str | None] = []
    cobertura: list[float] = []
    for p in palabras:
        duracion = max(p.fin - p.inicio, 1e-3)
        suma: dict[str, float] = {}
        cercano, distancia = None, float("inf")
        # Los turnos no se solapan: basta mirar los próximos a la palabra.
        i = max(0, bisect.bisect_right(inicios, p.inicio) - 1)
        while i < len(turnos) and turnos[i].inicio <= p.fin + DISTANCIA_MAX:
            t = turnos[i]
            solape = min(p.fin, t.fin) - max(p.inicio, t.inicio)
            if solape > 0:
                suma[t.hablante] = suma.get(t.hablante, 0.0) + solape
            elif -solape < distancia:
                cercano, distancia = t.hablante, -solape
            i += 1
        if suma:
            h = max(suma, key=suma.get)
            etiquetas.append(h)
            cobertura.append(suma[h] / duracion)
        else:
            etiquetas.append(cercano if distancia <= DISTANCIA_MAX else None)
            cobertura.append(0.0)

    # Palabra suelta, mal cubierta y rodeada por otro hablante: es un error de
    # bordes. Si su turno la cubre bien se respeta (un "sí" de otra persona).
    for i in range(1, len(palabras) - 1):
        antes, despues = etiquetas[i - 1], etiquetas[i + 1]
        if (
            antes is not None and antes == despues != etiquetas[i]
            and cobertura[i] < 0.5
            and palabras[i].inicio - palabras[i - 1].fin < PAUSA_FRASE
            and palabras[i + 1].inicio - palabras[i].fin < PAUSA_FRASE
        ):
            etiquetas[i] = antes

    # Sin turno cercano: se hereda de la palabra anterior o, al principio, de
    # la primera que tenga hablante.
    actual = next((e for e in etiquetas if e is not None), None)
    for i, e in enumerate(etiquetas):
        actual = e if e is not None else actual
        etiquetas[i] = actual
    return etiquetas


# --------------------------------------------------------------------------
# Frases e intervenciones
# --------------------------------------------------------------------------


def agrupar_frases(
    palabras: list[Palabra], hablantes: list[str]
) -> list[tuple[str, Frase]]:
    frases: list[tuple[str, Frase]] = []
    grupo: list[Palabra] = []
    quien = ""

    def cerrar():
        texto = re.sub(r"\s+", " ", "".join(p.texto for p in grupo)).strip()
        if texto:
            frases.append((quien, Frase(grupo[0].inicio, grupo[-1].fin, texto)))
        grupo.clear()

    for p, h in zip(palabras, hablantes, strict=True):
        if grupo and (
            h != quien
            or p.inicio - grupo[-1].fin > PAUSA_FRASE
            or grupo[-1].texto.rstrip().endswith(FIN_DE_FRASE)
            or p.fin - grupo[0].inicio > DURACION_MAX
        ):
            cerrar()
        quien = h
        grupo.append(p)
    if grupo:
        cerrar()
    return frases


def juntar(frases: list[tuple[str, Frase]]) -> list[Intervencion]:
    salida: list[Intervencion] = []
    for h, f in sorted(frases, key=lambda x: (x[1].inicio, x[1].fin)):
        u = salida[-1] if salida else None
        if (
            u is not None and u.hablante == h
            and f.inicio - u.fin <= PAUSA_INTERVENCION
            and f.fin - u.inicio <= DURACION_MAX
        ):
            u.frases.append(f)
            u.fin = max(u.fin, f.fin)
        else:
            salida.append(Intervencion(h, f.inicio, f.fin, [f]))
    return salida


# --------------------------------------------------------------------------
# Eco
# --------------------------------------------------------------------------


def _normalizar(texto: str) -> list[str]:
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFD", texto.lower()) if not unicodedata.combining(c)
    )
    return re.findall(r"\w+", sin_tildes)


def quitar_eco(
    mic: list[tuple[str, Frase]], sistema: list[Palabra]
) -> tuple[list[tuple[str, Frase]], int]:
    """Elimina del micrófono las frases que los altavoces repitieron."""
    limpias, quitadas = [], 0
    for h, f in mic:
        propias = _normalizar(f.texto)
        if len(propias) >= PALABRAS_MIN_ECO:
            cerca = _normalizar("".join(
                p.texto for p in sistema
                if p.fin >= f.inicio - MARGEN_ECO and p.inicio <= f.fin + MARGEN_ECO
            ))
            comunes = sum(
                b.size for b in SequenceMatcher(None, propias, cerca, False).get_matching_blocks()
            )
            if comunes / len(propias) >= PARECIDO_ECO:
                quitadas += 1
                continue
        limpias.append((h, f))
    return limpias, quitadas


# --------------------------------------------------------------------------
# Unión
# --------------------------------------------------------------------------


def unir(
    mic: Resultado,
    sistema: Resultado,
    turnos: list[Turno],
    titulo: str,
    inicio: str,
    nombre_usuario: str,
) -> Transcripcion:
    avisos: list[str] = []
    p_sis = _palabras(sistema)
    h_sis = [h or "SPEAKER_00" for h in asignar_hablantes(p_sis, turnos)]
    f_sis = agrupar_frases(p_sis, h_sis)

    p_mic = _palabras(mic)
    f_mic = agrupar_frases(p_mic, [ID_USUARIO] * len(p_mic))
    f_mic, ecos = quitar_eco(f_mic, p_sis)
    if ecos:
        avisos.append(
            f"Se quitaron {ecos} frases del micrófono que repetían el audio del sistema. "
            "Usa auriculares para evitarlo."
        )

    # Los hablantes se numeran por orden de aparición.
    ids: dict[str, str] = {}
    for h, _ in f_sis:
        ids.setdefault(h, f"h{len(ids) + 1}")
    hablantes = {ID_USUARIO: nombre_usuario}
    hablantes.update({i: f"Hablante {i[1:]}" for i in ids.values()})

    intervenciones = juntar(f_mic + [(ids[h], f) for h, f in f_sis])
    for r in (mic, sistema):
        if r.aviso:
            avisos.append(f"{r.archivo}: {r.aviso}")
    return Transcripcion(titulo, inicio, hablantes, intervenciones, avisos)


# --------------------------------------------------------------------------
# Nombres de los hablantes
# --------------------------------------------------------------------------


def _nombre_valido(nombre: str) -> str:
    nombre = re.sub(r"\s+", " ", nombre).strip()
    if not nombre or not nombre.isprintable():
        raise ErrorUnion("El nombre está vacío o no es válido.")
    if len(nombre) > LARGO_NOMBRE:
        raise ErrorUnion(f"El nombre supera los {LARGO_NOMBRE} caracteres: {nombre}")
    return nombre


def renombrar(t: Transcripcion, cambios: dict[str, str]) -> list[str]:
    """Cambia los nombres visibles (identificador -> nombre). Devuelve avisos."""
    nuevos = dict(t.hablantes)
    for ident, nombre in cambios.items():
        if ident not in nuevos:
            raise ErrorUnion(f"No existe el hablante {ident}.")
        nuevos[ident] = _nombre_valido(nombre)
    t.hablantes = nuevos
    # Se permite: sirve cuando la diarización partió a una persona en dos.
    nombres = list(nuevos.values())
    repetidos = dict.fromkeys(n for n in nombres if nombres.count(n) > 1)
    return [f"Hay varios hablantes llamados {n}: se verán como la misma persona."
            for n in repetidos]


def resolver(t: Transcripcion, quien: str) -> str:
    """Devuelve el identificador a partir de él mismo o del nombre visible."""
    buscado = quien.strip().casefold()
    if buscado in t.hablantes:
        # "yo" es también un identificador: otro hablante podría llamarse así.
        return buscado
    ids = [i for i, n in t.hablantes.items() if n.casefold() == buscado]
    if len(ids) == 1:
        return ids[0]
    if ids:
        raise ErrorUnion(f"Hay varios hablantes llamados {quien}. Usa su código: {', '.join(ids)}.")
    disponibles = ", ".join(f"{i} ({n})" for i, n in t.hablantes.items())
    raise ErrorUnion(f"No existe el hablante {quien}. Hay: {disponibles}.")


def heredar_nombres(nueva: Transcripcion, anterior: Transcripcion) -> list[str]:
    """Al repetir el proceso, recupera los nombres que había puesto el usuario.

    Los identificadores pueden cambiar entre pasadas, así que se comparan los
    tiempos. Solo se hereda si las dos voces coinciden casi por completo.
    """
    if anterior.hablantes.get(ID_USUARIO, "Yo") != "Yo":
        nueva.hablantes[ID_USUARIO] = anterior.hablantes[ID_USUARIO]

    def tramos(t: Transcripcion) -> dict[str, list[Frase]]:
        d: dict[str, list[Frase]] = {}
        for i in t.intervenciones:
            d.setdefault(i.hablante, []).extend(i.frases)
        return d

    def segundos(frases: list[Frase]) -> float:
        return sum(f.fin - f.inicio for f in frases)

    antes, ahora = tramos(anterior), tramos(nueva)
    perdidos = []
    for viejo, nombre in anterior.hablantes.items():
        if viejo == ID_USUARIO or re.fullmatch(r"Hablante \d+", nombre):
            continue
        a = antes.get(viejo, [])
        for ident, b in ahora.items():
            comun = sum(
                max(0.0, min(x.fin, y.fin) - max(x.inicio, y.inicio)) for x in a for y in b
            )
            if ident != ID_USUARIO and comun > PARECIDO_VOZ * max(segundos(a), segundos(b)):
                nueva.hablantes[ident] = nombre
                break
        else:
            perdidos.append(nombre)
    if not perdidos:
        return []
    return ["La separación de voces cambió y no se pudieron conservar estos nombres: "
            + ", ".join(perdidos) + ". Vuelve a ponerlos."]


# --------------------------------------------------------------------------
# Correcciones a mano
# --------------------------------------------------------------------------


def frases(t: Transcripcion) -> list[tuple[str, Frase]]:
    """Todas las frases en orden, con su hablante."""
    return [(i.hablante, f) for i in t.intervenciones for f in i.frases]


def agregar_hablante(t: Transcripcion, nombre: str) -> str:
    usados = [int(i[1:]) for i in t.hablantes if re.fullmatch(r"h\d+", i)]
    ident = f"h{max(usados, default=0) + 1}"
    t.hablantes[ident] = _nombre_valido(nombre)
    return ident


def corregir(
    t: Transcripcion, n: int, texto: str | None = None, hablante: str | None = None,
    toda: bool = False,
) -> None:
    """Cambia el texto o el hablante de la frase n, o el de toda su intervención."""
    lista = frases(t)
    quien, frase = lista[n]
    if texto is not None:
        limpio = re.sub(r"\s+", " ", texto).strip()
        if not limpio:
            raise ErrorUnion("El texto está vacío. Para quitar la frase, usa Borrar.")
        frase.texto = limpio
    if hablante is not None and hablante not in t.hablantes:
        raise ErrorUnion(f"No existe el hablante {hablante}.")
    if hablante is not None and hablante != quien:
        grupo = [frase]
        if toda:
            grupo = next(i.frases for i in t.intervenciones
                         if any(f is frase for f in i.frases))
        ids = {id(f) for f in grupo}
        lista = [(hablante if id(f) in ids else h, f) for h, f in lista]
    t.intervenciones = juntar(lista)
    t.corregida = True


def borrar(t: Transcripcion, n: int) -> None:
    lista = frases(t)
    del lista[n]
    t.intervenciones = juntar(lista)
    t.corregida = True


def hms(segundos: float) -> str:
    s = int(segundos)
    return f"{s // 3600:02}:{s % 3600 // 60:02}:{s % 60:02}"


def a_texto(t: Transcripcion) -> str:
    return "\n".join(
        f"[{hms(i.inicio)}] {t.hablantes[i.hablante]}: {i.texto}" for i in t.intervenciones
    )


def guardar(t: Transcripcion, carpeta: Path) -> Path:
    destino = carpeta / "transcripcion.json"
    temporal = destino.with_suffix(".tmp")
    try:
        # Se escribe aparte y se reemplaza: un corte no deja el archivo a medias.
        temporal.write_text(json.dumps(asdict(t), indent=1, ensure_ascii=False), encoding="utf-8")
        temporal.replace(destino)
    except OSError as e:
        raise ErrorUnion(f"No se pudo guardar {destino}: {e}") from e
    return destino


def cargar(carpeta: Path) -> Transcripcion:
    ruta = carpeta / "transcripcion.json"
    try:
        d = json.loads(ruta.read_text(encoding="utf-8"))
        return Transcripcion(
            d["titulo"], d["inicio"], d["hablantes"],
            [Intervencion(i["hablante"], i["inicio"], i["fin"], [Frase(**f) for f in i["frases"]])
             for i in d["intervenciones"]],
            d["avisos"], bool(d.get("corregida", False)),
        )
    except FileNotFoundError:
        raise ErrorUnion(f"No existe {ruta}. Procesa antes la reunión.") from None
    except (ValueError, KeyError, TypeError) as e:
        raise ErrorUnion(f"{ruta} está dañado: {e}") from e
