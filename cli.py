"""Comandos de consola equivalentes a la ventana.

    python cli.py dispositivos
    python cli.py grabar --titulo "Prueba" [--segundos 30]
    python cli.py verificar [carpeta]
    python cli.py descargar small | diarizacion
    python cli.py transcribir [carpeta o wav] [--modelo small] [--dispositivo cpu]
    python cli.py procesar [carpeta] [--hablantes 3]
    python cli.py renombrar [h1=Ana "Hablante 2=Luis"] [--carpeta ruta]
    python cli.py exportar [carpeta]
"""

from __future__ import annotations

import argparse
import getpass
import multiprocessing
import sys
import time
import wave
from pathlib import Path

import numpy as np
import pyaudiowpatch as pyaudio

import config
import diarizer
import exporter
import flujo
import merger
import transcriber
from recorder import (
    ESPACIO_PARADA,
    ErrorGrabacion,
    Grabadora,
    dbfs,
    elegir_loopback,
    elegir_microfono,
    espacio_libre,
    listar_dispositivos,
)

AVISO_CONSENTIMIENTO = """\
RECUERDA: avisa a los participantes de que vas a grabar y transcribir la
reunión, y pide su consentimiento antes de continuar."""


def cmd_dispositivos(cfg: config.Config, _args) -> int:
    pa = pyaudio.PyAudio()
    try:
        micros, loopbacks = listar_dispositivos(pa)
        mic, aviso_mic = elegir_microfono(pa, cfg.microfono)
        sis, aviso_sis = elegir_loopback(pa, cfg.salida)
    finally:
        pa.terminate()
    for titulo, lista, elegido in (
        ("Micrófonos", micros, mic),
        ("Salidas que se pueden grabar", loopbacks, sis),
    ):
        print(f"\n{titulo}:")
        for d in lista:
            marca = "*" if d["index"] == elegido["index"] else " "
            print(f"  {marca} {d['name']}  ({int(d['defaultSampleRate'])} Hz)")
    print("\n* = la que se usará. Se cambia en config.json.")
    for aviso in (aviso_mic, aviso_sis):
        if aviso:
            print("AVISO:", aviso)
    return 0


def _barra(nivel: float, ancho: int = 20) -> str:
    """Barra de nivel entre -60 y 0 dBFS."""
    llenos = round(max(0.0, min(1.0, (nivel + 60) / 60)) * ancho)
    return "#" * llenos + "-" * (ancho - llenos)


def cmd_grabar(cfg: config.Config, args) -> int:
    print(AVISO_CONSENTIMIENTO)
    if not args.si:
        input("\nPulsa Enter para empezar (Ctrl+C para cancelar)... ")
    g = Grabadora(cfg)
    carpeta = g.iniciar(args.titulo)
    print(f"\nGrabando en {carpeta}\nPulsa Ctrl+C para detener.\n")
    for aviso in g.avisos:
        print("AVISO:", aviso)
    mostrados: set[str] = set()
    vueltas = 0
    try:
        while args.segundos is None or g.transcurrido < args.segundos:
            t = int(g.transcurrido)
            print(
                f"\rREC {t // 60:02}:{t % 60:02}  "
                f"mic [{_barra(g.mic.estado.pico_dbfs)}]  "
                f"sistema [{_barra(g.sistema.estado.pico_dbfs)}]",
                end="", flush=True,
            )
            for p in g.problemas():
                if p not in mostrados:
                    mostrados.add(p)
                    print(f"\nERROR: {p}")
            # Cada 10 s, porque se espera 0,1 s en cada vuelta.
            vueltas += 1
            libre = espacio_libre(carpeta) if vueltas % 100 == 0 else None
            if libre is not None and libre < ESPACIO_PARADA:
                print("\nAVISO: el disco está casi lleno. Se detiene la grabación.")
                break
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        sesion = g.detener()
    print("\n\nGrabación terminada.")
    for nombre, p in sesion["pistas"].items():
        print(
            f"  {nombre:8} {p['duracion_s']:.2f} s | pico {p['pico_max_dbfs']} dBFS | "
            f"huecos {p['huecos_rellenados']} | deriva {p['deriva_corregida_ms']} ms"
        )
    for aviso in sesion["avisos"]:
        print("AVISO:", aviso)
    siguiente = config.comando(f'verificar "{carpeta}"')
    print(f"\nSiguiente paso: {siguiente}")
    return 0


def _leer_wav(ruta: Path) -> np.ndarray:
    try:
        with wave.open(str(ruta), "rb") as w:
            return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    except FileNotFoundError:
        raise ErrorGrabacion(f"No existe {ruta}") from None
    except (wave.Error, EOFError) as e:
        raise ErrorGrabacion(f"{ruta} no es un WAV válido: {e}") from e


def _ultima_carpeta(cfg: config.Config) -> Path:
    raiz = Path(cfg.carpeta_reuniones)
    sesiones = list(raiz.glob("*/sesion.json")) if raiz.exists() else []
    if not sesiones:
        raise ErrorGrabacion(f"No hay grabaciones en {raiz}")
    return max(sesiones, key=lambda p: p.stat().st_mtime).parent


def cmd_verificar(cfg: config.Config, args) -> int:
    carpeta = Path(args.carpeta) if args.carpeta else _ultima_carpeta(cfg)
    print(f"Carpeta: {carpeta}\n")
    pistas = {n: _leer_wav(carpeta / f"{n}.wav") for n in ("mic", "sistema")}
    correcto = True
    for nombre, x in pistas.items():
        if len(x) == 0:
            print(f"{nombre:8} VACÍA")
            correcto = False
            continue
        y = x.astype(np.float64)
        pico = dbfs(float(np.abs(y).max()))
        rms = dbfs(float(np.sqrt(np.mean(y**2))))
        saturadas = int(np.count_nonzero(np.abs(y) >= 32767))
        print(f"{nombre:8} {len(x) / config.FRECUENCIA:7.2f} s | pico {pico:6.1f} dBFS | "
              f"medio {rms:6.1f} dBFS | saturadas {saturadas}")
        if pico < -60:
            print("         -> en silencio")
            correcto = False
        elif pico < -30:
            print("         -> nivel bajo: sube el volumen del micrófono o de la reunión")
        if saturadas > 10:
            print("         -> distorsiona: baja el volumen")
    dif = abs(len(pistas["mic"]) - len(pistas["sistema"])) / config.FRECUENCIA * 1000
    print(f"\nDiferencia de duración: {dif:.1f} ms")
    if dif > 100:
        print("  -> demasiado grande")
        correcto = False

    # Estéreo para escuchar ambas pistas a la vez y juzgar la sincronía.
    n = min(len(x) for x in pistas.values())
    if n:
        ruta = carpeta / "comprobacion.wav"
        estereo = np.column_stack([pistas["mic"][:n], pistas["sistema"][:n]])
        with wave.open(str(ruta), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(config.FRECUENCIA)
            w.writeframes(estereo.tobytes())
        print(f"Para escuchar (izquierda = mic, derecha = sistema): {ruta}")
    print("\nRESULTADO:", "correcto" if correcto else "revisar los avisos")
    return 0 if correcto else 1


def cmd_descargar(_cfg: config.Config, args) -> int:
    if args.modelo == "diarizacion":
        # No se muestra al escribirlo ni se guarda en ningún archivo.
        token = getpass.getpass("Token de Hugging Face (no se verá al pegarlo): ").strip()
        if not token:
            raise diarizer.ErrorDiarizacion("No se escribió ningún token.")
        print("Descargando el modelo de diarización...")
        print("Guardado en", diarizer.descargar(token))
        return 0
    print(f"Descargando {args.modelo}...")
    print("Guardado en", transcriber.descargar_modelo(args.modelo))
    return 0


def cmd_transcribir(cfg: config.Config, args) -> int:
    ruta = Path(args.ruta) if args.ruta else _ultima_carpeta(cfg)
    rutas = [ruta / "mic.wav", ruta / "sistema.wav"] if ruta.is_dir() else [ruta]
    cfg.modelo = args.modelo or cfg.modelo
    cfg.dispositivo = args.dispositivo or cfg.dispositivo
    inicio = time.perf_counter()
    resultados = transcriber.transcribir(
        rutas, cfg,
        al_estado=_estado,
        al_progreso=_progreso,
    )
    total = time.perf_counter() - inicio
    print()
    for wav, r in zip(rutas, resultados, strict=True):
        destino = transcriber.guardar_resultado(r, wav)
        print(f"\n=== {r.archivo} ({r.duracion:.1f} s) ===")
        for s in r.segmentos:
            print(f"[{merger.hms(s.inicio)}] {s.texto}")
        if r.aviso:
            print("AVISO:", r.aviso)
        print(f"-- {r.segundos_proceso:.1f} s de proceso; guardado en {destino.name}")
    r = resultados[0]
    print(f"\nModelo {r.modelo}, {'GPU' if r.dispositivo == 'cuda' else 'CPU'} ({r.calculo}). "
          f"Tiempo total: {total:.1f} s")
    return 0


def _estado(texto: str) -> None:
    print(f"\n{texto}")


def _progreso(p: float) -> None:
    print(f"\r  {p:4.0%}", end="", flush=True)


def cmd_procesar(cfg: config.Config, args) -> int:
    carpeta = Path(args.carpeta) if args.carpeta else _ultima_carpeta(cfg)
    inicio = time.perf_counter()
    cfg.modelo = args.modelo or cfg.modelo
    p = flujo.procesar(
        carpeta, cfg, args.hablantes, bool(args.modelo) or args.retranscribir, _estado, _progreso,
    )
    t = p.transcripcion
    print(f"\n\n{merger.a_texto(t)}\n")
    for aviso in p.avisos:
        print("AVISO:", aviso)
    print(f"Hablantes: {', '.join(t.hablantes.values())}")
    print(f"Diarización: {p.segundos_diarizacion:.1f} s. "
          f"Total: {time.perf_counter() - inicio:.1f} s")
    _mostrar_exportacion(p.exportacion)
    if len(t.hablantes) > 1:
        print(f"\nPara poner nombres: {config.comando('renombrar')}")
    return 1 if p.incompleto else 0


def _mostrar_exportacion(e: exporter.Exportacion) -> None:
    for a in e.archivos:
        print("Exportado", a)
    for c in e.copias:
        print("AVISO: habías editado el archivo; tu versión está en", c.name)


def cmd_exportar(cfg: config.Config, args) -> int:
    carpeta = Path(args.carpeta) if args.carpeta else _ultima_carpeta(cfg)
    _mostrar_exportacion(exporter.exportar(merger.cargar(carpeta), carpeta))
    return 0


def _preguntar_nombres(t: merger.Transcripcion) -> dict[str, str]:
    """Muestra qué dijo cada hablante, para reconocerlo, y pide su nombre."""
    cambios = {}
    print("Escribe el nombre de cada uno. Enter lo deja igual; Ctrl+C cancela.")
    for ident, nombre in t.hablantes.items():
        frases = [f for i in t.intervenciones if i.hablante == ident for f in i.frases]
        if not frases:
            continue
        habla = sum(f.fin - f.inicio for f in frases)
        print(f"\n{nombre} ({ident}), habla {merger.hms(habla)}. Por ejemplo:")
        largas = sorted(frases, key=lambda f: -len(f.texto))[:2]
        for f in sorted(largas, key=lambda f: f.inicio):
            print(f"  [{merger.hms(f.inicio)}] {f.texto}")
        nuevo = input("  Nombre: ").strip()
        if nuevo:
            cambios[ident] = nuevo
    return cambios


def cmd_renombrar(cfg: config.Config, args) -> int:
    carpeta = Path(args.carpeta) if args.carpeta else _ultima_carpeta(cfg)
    t = merger.cargar(carpeta)
    cambios: dict[str, str] = {}
    for par in args.cambios:
        quien, igual, nombre = par.partition("=")
        if not igual:
            raise merger.ErrorUnion(f'"{par}" no es válido. Ejemplo: h1=Ana')
        ident = merger.resolver(t, quien)
        if ident in cambios:
            raise merger.ErrorUnion(f"{quien} aparece dos veces.")
        cambios[ident] = nombre
    if not args.cambios:
        cambios = _preguntar_nombres(t)
    if not cambios:
        print("\nNo se cambió ningún nombre.")
        return 0
    exportacion, avisos = exporter.renombrar(carpeta, cambios)
    print()
    for aviso in avisos:
        print("AVISO:", aviso)
    print("Hablantes:", ", ".join(merger.cargar(carpeta).hablantes.values()))
    _mostrar_exportacion(exportacion)
    return 0


def main() -> int:
    for salida in (sys.stdout, sys.stderr):
        salida.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Grabador y transcriptor de reuniones")
    sub = parser.add_subparsers(required=True)
    p = sub.add_parser("dispositivos", help="lista micrófonos y salidas")
    p.set_defaults(func=cmd_dispositivos)
    p = sub.add_parser("grabar", help="graba micrófono y sistema")
    p.add_argument("--titulo", default="prueba")
    p.add_argument("--segundos", type=float, help="detener automáticamente")
    p.add_argument("--si", action="store_true", help="no esperar confirmación")
    p.set_defaults(func=cmd_grabar)
    p = sub.add_parser("verificar", help="analiza una grabación")
    p.add_argument("carpeta", nargs="?", help="por defecto, la última")
    p.set_defaults(func=cmd_verificar)
    p = sub.add_parser("descargar", help="descarga un modelo")
    p.add_argument("modelo", choices=(*transcriber.MODELOS, "diarizacion"))
    p.set_defaults(func=cmd_descargar)
    p = sub.add_parser("transcribir", help="transcribe una reunión o un WAV")
    p.add_argument("ruta", nargs="?", help="carpeta o WAV; por defecto, la última grabación")
    p.add_argument("--modelo", choices=("auto", *transcriber.MODELOS))
    p.add_argument("--dispositivo", choices=("auto", "gpu", "cpu"))
    p.set_defaults(func=cmd_transcribir)
    p = sub.add_parser("procesar", help="transcribe, separa hablantes y une las pistas")
    p.add_argument("carpeta", nargs="?", help="por defecto, la última grabación")
    p.add_argument("--hablantes", type=int, help="personas en la pista del sistema, sin contarte")
    p.add_argument("--modelo", choices=("auto", *transcriber.MODELOS))
    p.add_argument("--retranscribir", action="store_true")
    p.set_defaults(func=cmd_procesar)
    p = sub.add_parser("renombrar", help="pone nombre a los hablantes")
    p.add_argument("cambios", nargs="*", help="h1=Ana; sin nada, los pregunta uno a uno")
    p.add_argument("--carpeta", help="por defecto, la última grabación")
    p.set_defaults(func=cmd_renombrar)
    p = sub.add_parser("exportar", help="vuelve a crear los .txt, .docx y .srt")
    p.add_argument("carpeta", nargs="?", help="por defecto, la última grabación")
    p.set_defaults(func=cmd_exportar)
    args = parser.parse_args()
    config.configurar_logging()
    try:
        return args.func(config.cargar(), args)
    except (*flujo.ERRORES, RuntimeError) as e:
        print(f"\nERROR: {e}\nDetalles en {config.RUTA_LOG}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nCancelado.")
        return 130


if __name__ == "__main__":
    multiprocessing.freeze_support()    # los procesos hijos del .exe empiezan aquí
    sys.exit(main())
