"""Hook de Claude Code: escribir en Trello lo aprueba el usuario, no Claude.

    python trello_guard.py pre    PreToolUse: `ask` para escrituras y para el token
    python trello_guard.py post   PostToolUse: despues de `factoria new` en un repo
                                  que espeja a Trello, ordena preguntar

Lee el evento de stdin y escribe la decision en stdout, como pide Claude Code.
Sin salida, el hook no opina y la herramienta sigue su permiso normal.

No es un lock absoluto: Claude corre con el mismo usuario de Windows que el
token. Traba las rutas directas (el comando de factoria, la API, el archivo de
credenciales) y las obvias para esquivarlo (editar este hook o desregistrarlo).
"""
from __future__ import annotations

import json
import re
import shlex
import sys

# `factoria trello ...` con un flag que escribe en el tablero.
RE_ESCRITURA = re.compile(r"\bfactoria(?:\.py)?\b[^\n|;&]*\btrello\b[^\n|;&]*"
                          r"--(?:crear|publicar|mover|reemplazar)\b")
# Lo que da acceso al token por fuera de factoria.
RE_TOKEN = re.compile(r"api\.trello\.com|\.factoria-trello\.yml|TRELLO_(?:TOKEN|KEY)"
                      r"|\btrello_api\b|\bcredenciales_trello\b", re.I)
# Desarmar el lock: el script del hook, o los settings que lo registran.
RE_HOOK = re.compile(r"trello_guard\.py", re.I)
RE_SETTINGS = re.compile(r"[\\/]\.claude[^\\/]*[\\/]settings(?:\.local)?\.json$", re.I)
# Un archivo de codigo que nombra el token se puede ejecutar despues; un .md no.
EJECUTABLES = (".py", ".sh", ".ps1", ".bat", ".cmd", ".js", ".ts", ".mjs")

AVISO_NEW = "este repo espeja a Trello"
RE_SLUG_AVISO = re.compile(r"factoria trello ([a-z0-9][a-z0-9-]*)")
ACCIONES = {"--crear": "CREAR una tarjeta", "--publicar": "PUBLICAR la descripcion",
            "--mover": "MOVER la tarjeta de lista",
            "--reemplazar": "PISAR la descripcion que ya tenia"}


def _corto(texto: str, n: int = 400) -> str:
    texto = " ".join(texto.split())
    return texto if len(texto) <= n else texto[:n] + "…"


def _que_escribe(comando: str) -> str:
    """Lo que va a hacer `factoria trello`, con el texto exacto que se sube."""
    m = RE_ESCRITURA.search(comando)
    try:
        partes = shlex.split(comando[m.start():] if m else comando, posix=True)
    except ValueError:
        return "Comando: " + _corto(comando)
    slug = next((x for i, x in enumerate(partes)
                 if i and partes[i - 1] == "trello" and not x.startswith("-")), "?")
    acciones = [v for k, v in ACCIONES.items() if k in partes]

    def valor(flag: str) -> str:
        return partes[partes.index(flag) + 1] if flag in partes[:-1] else ""

    lineas = [f"Trello, ticket {slug}: " + " y ".join(acciones) + "."]
    if titulo := valor("--titulo"):
        lineas.append(f"Titulo: {titulo}")
    if texto := valor("--texto"):
        lineas.append(f"Descripcion: {_corto(texto, 700)}")
    elif "--crear" in partes:
        lineas.append(f"Titulo: {titulo or slug} (sin descripcion)")
    return " | ".join(lineas)


def decidir_pre(evento: dict) -> str:
    """El motivo para pedir aprobacion, diciendo QUE se va a hacer, o "" si no
    hace falta."""
    herramienta = evento.get("tool_name") or ""
    ti = evento.get("tool_input") or {}
    comando = str(ti.get("command") or "")
    ruta = str(ti.get("file_path") or ti.get("path") or "")
    contenido = "\n".join(str(ti.get(k) or "") for k in ("content", "new_string"))
    if comando and RE_ESCRITURA.search(comando):
        return _que_escribe(comando)
    if comando and RE_TOKEN.search(comando):
        return ("Usa el token o la API de Trello por fuera de factoria. Comando: "
                + _corto(comando))
    if ruta and RE_TOKEN.search(ruta):
        return f"{herramienta} del archivo con el token de Trello: {ruta}"
    if (herramienta in ("Edit", "Write") and ruta.lower().endswith(EJECUTABLES)
            and RE_TOKEN.search(contenido)):
        return (f"Escribe codigo que usa el token o la API de Trello en {ruta}: "
                + _corto(contenido, 300))
    if RE_HOOK.search(ruta) or (herramienta in ("Edit", "Write")
                                and RE_SETTINGS.search(ruta)):
        return (f"{herramienta} de {ruta}, que es el hook que traba Trello o los "
                "settings que lo registran.")
    if comando and RE_HOOK.search(comando):
        return ("El comando toca el script del hook que traba Trello: "
                + _corto(comando))
    return ""


def _cadenas(x) -> list[str]:
    """Los strings de `tool_response`, sin depender de su forma."""
    if isinstance(x, str):
        return [x]
    if isinstance(x, dict):
        return [c for v in x.values() for c in _cadenas(v)]
    if isinstance(x, list):
        return [c for v in x for c in _cadenas(v)]
    return []


def decidir_post(evento: dict) -> str:
    """La orden para Claude despues de un `factoria new`, o ""."""
    comando = str((evento.get("tool_input") or {}).get("command") or "")
    if not re.search(r"\bfactoria(?:\.py)?\s+new\b", comando):
        return ""
    # rich parte las lineas al ancho de la consola: se compara sin saltos.
    salida = re.sub(r"\s+", " ", " ".join(_cadenas(evento.get("tool_response"))))
    if AVISO_NEW not in salida:
        return ""
    m = RE_SLUG_AVISO.search(salida)
    slug = m.group(1) if m else "<slug>"
    return (f"OBLIGATORIO, antes de cualquier otra cosa: el ticket {slug} es de un repo "
            "que espeja a Trello. Preguntale al usuario si quiere crear su tarjeta "
            "(con AskUserQuestion). Si dice que si, segui la skill `trello`: redacta "
            f"la descripcion con `factoria trello {slug}`, mostrasela y espera su OK "
            "explicito. Crear o publicar en Trello pide ademas su aprobacion manual.")


def main(modo: str) -> int:
    evento = json.load(sys.stdin)
    if modo == "pre":
        if motivo := decidir_pre(evento):
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "ask",
                "permissionDecisionReason": f"trello_guard — {motivo}"}}))
    elif modo == "post":
        if orden := decidir_post(evento):
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PostToolUse", "additionalContext": orden}}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
