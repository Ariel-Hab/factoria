#!/usr/bin/env python
"""factoria - gestor de tickets, sesiones y documentacion.

`board` (solo lectura) releva el estado real del ecosistema a partir de los
artefactos que ya existen (branches, worktrees, docs de trabajo, sesiones,
contratos) y reporta las cotas violadas.

`sesiones` / `resume` / `cortar` (paso 3) indexan las sesiones de los dos
config dirs y reanudan la correcta, con su cuenta y su cwd. El indice es
derivado: se reconstruye leyendo los .jsonl en cada corrida, asi que no hay
estado que se pueda desincronizar.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import click
import yaml
from rich.console import Console
from rich.table import Table

# --------------------------------------------------------------------------
# Layout (ver plan §4). Todo por path absoluto: no depende del cwd.
# --------------------------------------------------------------------------
ARIEL = Path(r"C:\ariel")
DATOS = ARIEL / ".factoria"
# DemandSync (UTN, Equipo 207) es hermano de dfv/ e integhra/, no esta anidado en
# ninguna de las dos -- mismo motivo que DATOS, va explicito (2026-09-10).
DEMANDSYNC = ARIEL / "demandsync"
CONTRATOS = ARIEL / "dfv" / ".contracts"
# Derivado: lo reescribe cada sesion fresca. Ignorado en el repo de datos.
CONTEXTOS = DATOS / "contexto"
RAICES = [ARIEL / "dfv", ARIEL / "integhra"]
# gstack es un template de terceros; skills/~ es un directorio espurio.
# `factoria` NO se ignora: la herramienta se trackea con la herramienta, y
# estaba excluida solo porque cuando se escribio esto todavia no tenia .git.
IGNORAR_REPOS = {"gstack", "skills"}

CUENTAS = {
    "dfv": Path.home() / ".claude-dfv",
    "personal": Path.home() / ".claude-personal",
}

# Cotas del estandar de documentacion (plan §3).
COTA_ESTADO_ACTUAL = 40
COTA_CONTRATO = 250
COTA_DOC_TRABAJO = 200
# Umbral inicial para recomendar cortar sesion. Se calibra midiendo (plan §7.5).
UMBRAL_SESION_MB = 1.0
# Calibrado el 2026-09-10 con los 50.401 turnos con `usage` de las 342 sesiones
# en disco, en tokens equivalentes de input fresco (lectura de cache 0,1x,
# escritura 1,25x, input 1x):
#
#   arrancar de cero (primer turno, mediana de 252)   41.400
#   continuar, 0 - 0,25 MB                            11.500    0,28x
#   continuar, 0,5 - 1 MB                             20.300    0,49x
#   continuar, 1 - 2 MB                               29.500    0,71x
#   continuar, mas de 8 MB                            29.500    0,71x
#
# El costo se SATURA en 1 MB. Arriba de ahi el tamano del .jsonl no predice
# nada, porque la compactacion vacia el prefijo: en la sesion de este ticket el
# cache read sube a 450k tokens y se derrumba a 30k, siete veces en 8,9 MB. El
# .jsonl es el registro acumulado de lo que paso, no lo que hay en contexto.
#
# Dos correcciones a la regla original, que suponia costo creciente:
#   1. continuar un turno NUNCA cuesta mas que reconstruir (peor caso 0,71x),
#      asi que cortar no ahorra en el turno siguiente;
#   2. cortar se paga recien despues de ~3 turnos.
# O sea: cortar si vas a seguir trabajando, no para hacer una pregunta. Y un
# .jsonl de 8 MB no es mas urgente que uno de 1,5 MB.
TURNOS_PARA_QUE_CONVENGA = 3
# Una sesion que no se toca en mas de esto ya no se va a reanudar: no es hallazgo.
DIAS_SESION_VIVA = 7

# Los titulos de sesion vienen del .jsonl con acentos y la consola de Windows no
# siempre esta en UTF-8: sin esto salen como '?'.
# stderr tambien: los mensajes de error de click salen por ahi y no por rich.
for _flujo in (sys.stdout, sys.stderr):
    if hasattr(_flujo, "reconfigure"):
        _flujo.reconfigure(encoding="utf-8", errors="replace")

console = Console()

# --------------------------------------------------------------------------
# git
# --------------------------------------------------------------------------


def git(repo: Path, *args: str) -> str:
    """Corre git en `repo` y devuelve stdout. Silencioso ante fallo."""
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def descubrir_repos() -> list[Path]:
    repos = []
    for raiz in RAICES:
        if not raiz.is_dir():
            continue
        for d in sorted(raiz.iterdir()):
            if d.is_dir() and (d / ".git").exists() and d.name not in IGNORAR_REPOS:
                repos.append(d)
    # El repo de datos va explicito: no esta bajo ninguna raiz, pero es un repo
    # con remoto y trabajo real -- los 13 contratos y los tickets viven ahi. Sin
    # esto, podar un contrato no puede ser un ticket, porque `commit` y `check`
    # no tienen donde correr.
    if (DATOS / ".git").exists():
        repos.append(DATOS)
    # DemandSync (UTN, Equipo 207): mismo motivo que DATOS. No esta bajo ninguna
    # RAIZ, es un repo real con remoto y trabajo real.
    if (DEMANDSYNC / ".git").exists():
        repos.append(DEMANDSYNC)
    return repos


def base_de(repo: Path) -> str:
    """Rama base real del repo: la que apunta origin/HEAD, o main/master."""
    ref = git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if ref and "/" in ref:
        return ref.split("/", 1)[1]
    ramas = set(git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines())
    for cand in ("master", "main"):
        if cand in ramas:
            return cand
    return ""


# --------------------------------------------------------------------------
# Branches
# --------------------------------------------------------------------------


@dataclass
class Rama:
    repo: str
    nombre: str
    dias: int
    pusheada: bool
    # Rama de integracion intermedia que ya la contiene, si hay alguna. En
    # defeve, 34 de las 55 "sin mergear a master" ya estan en desarrollo-ari:
    # no son 55 puntas sueltas, son 34 esperando un solo merge mas 21 sueltas.
    integrada_en: str = ""


# Ramas de integracion intermedias: en defeve las features salen de
# `desarrollo-ari` y se integran ahi antes de llegar a master.
BASES_CANDIDATAS = ("desarrollo-ari", "develop", "dev")


def _es_ancestro(repo: Path, rama: str, posible_padre: str) -> bool:
    r = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", rama, posible_padre],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode == 0


def ramas_sin_mergear(repo: Path, base: str) -> list[Rama]:
    if not base:
        return []
    sin_merge = {
        b.strip().lstrip("* ").strip()
        for b in git(repo, "branch", "--no-merged", base, "--format=%(refname:short)").splitlines()
        if b.strip()
    }
    if not sin_merge:
        return []

    # Las refs remotas locales suelen estar podadas (defeve tiene 12 para 105
    # ramas), asi que el upstream configurado es la senal confiable. Se usan
    # las dos: upstream O ref remota presente.
    remotas = {
        r.split("/", 1)[1]
        for r in git(repo, "for-each-ref", "--format=%(refname:short)", "refs/remotes").splitlines()
        if "/" in r
    }
    ramas_locales = set(
        git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines()
    )
    ahora = time.time()
    out = []
    for linea in git(
        repo,
        "for-each-ref",
        "--format=%(refname:short)%09%(committerdate:unix)%09%(upstream)",
        "refs/heads",
    ).splitlines():
        partes = linea.split("\t")
        nombre = partes[0]
        if nombre not in sin_merge:
            continue
        ts = partes[1] if len(partes) > 1 else ""
        upstream = partes[2] if len(partes) > 2 else ""
        dias = int((ahora - int(ts)) // 86400) if ts.isdigit() else 0
        integrada = ""
        for cand in BASES_CANDIDATAS:
            if cand != nombre and cand in ramas_locales and _es_ancestro(repo, nombre, cand):
                integrada = cand
                break
        out.append(Rama(repo.name, nombre, dias, bool(upstream) or nombre in remotas,
                        integrada))
    return sorted(out, key=lambda r: -r.dias)


# --------------------------------------------------------------------------
# Worktrees
# --------------------------------------------------------------------------


@dataclass
class Worktree:
    repo: str
    path: str
    rama: str | None  # None = detached
    principal: bool

    @property
    def hoja_dir(self) -> str:
        return Path(self.path).name

    @property
    def hoja_rama(self) -> str:
        return self.rama.rsplit("/", 1)[-1] if self.rama else ""

    def problema(self, base: str) -> str | None:
        """Un worktree 'miente' cuando su directorio no dice donde esta parado.

        Abreviar la rama en el nombre del directorio es legitimo, incluso
        salteando palabras del medio (`split-liquidacion-service` para
        `chore/split-liquidacion-proveedor-service`). Por eso la comparacion es
        por tokens: si los del directorio son un subconjunto de los de la rama,
        esta bien. Solo se marca cuando aparecen tokens que la rama no tiene.
        """
        if self.principal:
            return None
        if self.rama is None:
            return "detached HEAD"
        if self.rama == base:
            return f"parado en la base ({base})"
        tok_dir = set(re.split(r"[-_/.]+", self.hoja_dir.lower())) - {""}
        tok_rama = set(re.split(r"[-_/.]+", self.rama.lower())) - {""}
        if not tok_dir <= tok_rama:
            return f"dir dice '{self.hoja_dir}' pero esta en '{self.rama}'"
        return None


def worktrees_de(repo: Path) -> list[Worktree]:
    salida = git(repo, "worktree", "list", "--porcelain")
    if not salida:
        return []
    out: list[Worktree] = []
    path = rama = None
    detached = False
    for linea in salida.splitlines() + [""]:
        if linea.startswith("worktree "):
            path = linea[len("worktree "):]
            rama, detached = None, False
        elif linea.startswith("branch "):
            rama = linea[len("branch "):].removeprefix("refs/heads/")
        elif linea == "detached":
            detached = True
        elif not linea and path:
            out.append(Worktree(repo.name, path, None if detached else rama, principal=not out))
            path = rama = None
            detached = False
    return out


# --------------------------------------------------------------------------
# Docs de trabajo y estado inferido
# --------------------------------------------------------------------------

# Prioridad importa: "IMPLEMENTADO Y VERIFICADO" tiene que caer en verificado.
PATRONES_ESTADO: list[tuple[str, str]] = [
    ("cerrado", r"mergead|cerrad"),
    ("verificado", r"verificad|\blisto\b"),
    # "HECHO en dev, sin commitear" es literalmente como lo escribe hoy.
    ("implementado", r"implementad|\bhech[ao]s?\b"),
    ("en_curso", r"en curso|en progreso"),
    ("propuesto", r"propuest|pendiente de confirmaci|nada ejecutado|esperando|^\W*plan\b"),
]
RE_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.S)
RE_ESTADO_PROSA = re.compile(r"^\s*\*{0,2}Estado\*{0,2}\s*:\s*(.+)$", re.I | re.M)
RE_PENDIENTE = re.compile(r"^\s*[-*]\s*\[ \]", re.M)
RE_HECHO = re.compile(r"^\s*[-*]\s*\[[xX]\]", re.M)


def clasificar(texto: str) -> str:
    bajo = texto.lower()
    for estado, patron in PATRONES_ESTADO:
        if re.search(patron, bajo, re.M):
            return estado
    return "sin_estado"


@dataclass
class Doc:
    repo: str
    path: Path
    estado: str
    explicito: bool
    pendientes: int
    hechos: int
    lineas: int

    @property
    def slug(self) -> str:
        return self.path.stem if self.path.stem != "README" else self.path.parent.name


def leer_doc(repo_nombre: str, p: Path) -> Doc | None:
    try:
        texto = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    estado, explicito = "sin_estado", False
    fm = RE_FRONTMATTER.match(texto)
    if fm and re.search(r"^\s*estado\s*:", fm.group(1), re.I | re.M):
        crudo = re.search(r"^\s*estado\s*:\s*(.+)$", fm.group(1), re.I | re.M).group(1)
        estado, explicito = clasificar(crudo), True
    else:
        # El campo **Estado:** en prosa: solo el encabezado, no todo el archivo.
        m = RE_ESTADO_PROSA.search("\n".join(texto.splitlines()[:30]))
        if m:
            estado, explicito = clasificar(m.group(1)), True

    return Doc(
        repo=repo_nombre,
        path=p,
        estado=estado,
        explicito=explicito,
        pendientes=len(RE_PENDIENTE.findall(texto)),
        hechos=len(RE_HECHO.findall(texto)),
        lineas=len(texto.splitlines()),
    )


def docs_de(repo: Path) -> list[Doc]:
    """Cubre las dos convenciones que existen: .ai/tasks/ y .ia/{fixes,chores,proyectos}/."""
    candidatos: list[Path] = []
    candidatos += sorted((repo / ".ai" / "tasks").glob("*.md"))
    for sub in ("fixes", "chores", "features"):
        candidatos += sorted((repo / ".ia" / sub).glob("*.md"))
    candidatos += sorted((repo / ".ia" / "proyectos").glob("*/README.md"))
    docs = [d for p in candidatos if (d := leer_doc(repo.name, p))]
    return docs


# --------------------------------------------------------------------------
# Sesiones
# --------------------------------------------------------------------------


@dataclass
class Sesion:
    cuenta: str
    proyecto: str
    session_id: str
    mb: float
    dias: int
    cwd: str = ""
    rama: str = ""
    rama_inicial: str = ""
    titulo: str = ""
    mtime: float = 0.0

    @property
    def repo(self) -> str:
        return Path(self.cwd).name if self.cwd else self.proyecto

    @property
    def cambio_de_rama(self) -> bool:
        return bool(self.rama_inicial and self.rama != self.rama_inicial)

    @property
    def config_dir(self) -> Path:
        return CUENTAS[self.cuenta]

    @property
    def cwd_existe(self) -> bool:
        return bool(self.cwd) and Path(self.cwd).is_dir()


# Cuanto se lee de cada .jsonl. La cabeza trae cwd y rama inicial; la cola, la
# rama final y el ultimo `aiTitle` (se regenera varias veces por sesion, asi que
# el que vale es el ultimo). Leer los 338 archivos enteros seria ~350 MB; asi
# son ~56 MB y 0.45 s, que no justifica cachear.
CABEZA_LINEAS = 60
COLA_BYTES = 65_536


def _meta_sesion(jsonl: Path, tam: int) -> dict[str, str]:
    m = {"cwd": "", "rama_inicial": "", "rama": "", "titulo": ""}
    try:
        with jsonl.open("rb") as fh:
            cabeza = [fh.readline() for _ in range(CABEZA_LINEAS)]
            if tam > COLA_BYTES:
                fh.seek(tam - COLA_BYTES)
                fh.readline()  # descartar la linea partida por el seek
            else:
                fh.seek(0)
            cola = fh.readlines()
    except OSError:
        return m
    # Cabeza primero y cola despues, a proposito: los campos "ultimos" (rama,
    # titulo) se sobreescriben en orden cronologico.
    for bloque, es_cabeza in ((cabeza, True), (cola, False)):
        for cruda in bloque:
            if not cruda.strip():
                continue
            try:
                d = json.loads(cruda.decode("utf-8", "replace"))
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            if d.get("cwd") and not m["cwd"]:
                m["cwd"] = d["cwd"]
            if d.get("gitBranch"):
                if es_cabeza and not m["rama_inicial"]:
                    m["rama_inicial"] = d["gitBranch"]
                m["rama"] = d["gitBranch"]
            if d.get("aiTitle"):
                m["titulo"] = d["aiTitle"]
    if not m["rama_inicial"]:
        m["rama_inicial"] = m["rama"]
    return m


def inventario_sesiones() -> list[Sesion]:
    ahora = time.time()
    out: list[Sesion] = []
    for cuenta, raiz in CUENTAS.items():
        proyectos = raiz / "projects"
        if not proyectos.is_dir():
            continue
        # rglob: 64 de las 338 sesiones viven mas abajo que un nivel.
        for jsonl in proyectos.rglob("*.jsonl"):
            try:
                st = jsonl.stat()
            except OSError:
                continue
            m = _meta_sesion(jsonl, st.st_size)
            out.append(Sesion(
                cuenta=cuenta,
                # El slug del proyecto es el primer componente bajo projects/,
                # no el directorio inmediato (hay sesiones anidadas).
                #
                # El slug SI se puede derivar del cwd (normalizando mayusculas y
                # `_`->`-`): falla en 1 de 334, una sesion lanzada en un
                # subdirectorio del repo. El cwd se guarda igual porque `resume`
                # lo necesita para lanzar claude en el directorio correcto, y
                # porque 4 sesiones no lo tienen registrado: esas no se pueden
                # reanudar y hay que decirlo, no fallar raro.
                proyecto=jsonl.relative_to(proyectos).parts[0],
                session_id=jsonl.stem,
                mb=st.st_size / 1_048_576,
                dias=int((ahora - st.st_mtime) // 86400),
                mtime=st.st_mtime,
                **m,
            ))
    return sorted(out, key=lambda s: -s.mb)


def buscar_sesiones(consulta: str, cuenta: str | None = None,
                    dias: int | None = None) -> list[Sesion]:
    """Sesiones que matchean rama, repo, slug o titulo. Mas reciente primero."""
    q = consulta.lower().strip()
    out = []
    for s in inventario_sesiones():
        if cuenta and s.cuenta != cuenta:
            continue
        if dias is not None and s.dias > dias:
            continue
        heno = " ".join((s.rama, s.rama_inicial, s.repo, s.proyecto, s.titulo)).lower()
        if q in heno:
            out.append(s)
    return sorted(out, key=lambda s: -s.mtime)


# --------------------------------------------------------------------------
# Cotas
# --------------------------------------------------------------------------

RE_SEC_ESTADO = re.compile(r"^#{1,6}\s*(?:7\.?\s*)?.*Estado de la Misi", re.I)
RE_HEADING = re.compile(r"^(#{1,6})\s")


def cota_estado_actual(repo: Path) -> tuple[int, int] | None:
    """Mide la seccion 'Estado de la Mision' de CLAUDE.md. Devuelve (linea, largo).

    La seccion termina en el siguiente heading de nivel IGUAL O MENOR, no en
    cualquier heading: los `###` de adentro son sus subsecciones. Medirla mal
    era lo que hacia que una seccion de 1352 lineas se reportara como 5.
    """
    p = repo / "CLAUDE.md"
    if not p.is_file():
        return None
    lineas = p.read_text(encoding="utf-8", errors="replace").splitlines()
    inicio = next((i for i, l in enumerate(lineas) if RE_SEC_ESTADO.match(l)), None)
    if inicio is None:
        return None
    nivel = len(RE_HEADING.match(lineas[inicio]).group(1))
    fin = next(
        (
            j
            for j in range(inicio + 1, len(lineas))
            if (m := RE_HEADING.match(lineas[j])) and len(m.group(1)) <= nivel
        ),
        len(lineas),
    )
    return inicio + 1, fin - inicio


def cotas_contratos() -> list[tuple[str, int]]:
    if not CONTRATOS.is_dir():
        return []
    out = []
    for p in sorted(CONTRATOS.glob("*.md")):
        if p.name == "README.md":
            continue
        n = len(p.read_text(encoding="utf-8", errors="replace").splitlines())
        out.append((p.name, n))
    return sorted(out, key=lambda t: -t[1])


# --------------------------------------------------------------------------
# Relevamiento completo
# --------------------------------------------------------------------------


@dataclass
class Relevamiento:
    repos: list[Path] = field(default_factory=list)
    bases: dict[str, str] = field(default_factory=dict)
    ramas: list[Rama] = field(default_factory=list)
    worktrees: list[Worktree] = field(default_factory=list)
    docs: list[Doc] = field(default_factory=list)
    sesiones: list[Sesion] = field(default_factory=list)
    contratos: list[tuple[str, int]] = field(default_factory=list)
    estado_actual: dict[str, tuple[int, int]] = field(default_factory=dict)
    tickets: list["Ticket"] = field(default_factory=list)


def relevar() -> Relevamiento:
    rel = Relevamiento(repos=descubrir_repos())
    for repo in rel.repos:
        base = base_de(repo)
        rel.bases[repo.name] = base
        rel.ramas += ramas_sin_mergear(repo, base)
        rel.worktrees += worktrees_de(repo)
        rel.docs += docs_de(repo)
        if (ca := cota_estado_actual(repo)):
            rel.estado_actual[repo.name] = ca
    rel.sesiones = inventario_sesiones()
    rel.contratos = cotas_contratos()
    rel.tickets = tickets_todos()
    return rel


def _tiene_items(cuerpo: str, encabezado: str) -> bool:
    """Si una seccion tiene algun bullet con texto real. Los comentarios HTML de
    la plantilla y los guiones sueltos no cuentan."""
    lineas = cuerpo.splitlines()
    try:
        i = next(n for n, l in enumerate(lineas) if l.strip() == encabezado)
    except StopIteration:
        return False
    dentro = False
    for l in lineas[i + 1:]:
        if l.startswith("## "):
            break
        t = l.strip()
        if t.startswith("<!--"):
            dentro = True
        if dentro:
            if "-->" in t:
                dentro = False
            continue
        if t.startswith(("-", "*")) and len(t.lstrip("-*[ ]x").strip()) > 2:
            return True
    return False


def hallazgos(rel: Relevamiento) -> list[str]:
    """Todo lo que viola una cota o es internamente inconsistente."""
    h: list[str] = []

    sin_push = [r for r in rel.ramas if not r.pusheada]
    por_repo: dict[str, list[Rama]] = {}
    for r in rel.ramas:
        por_repo.setdefault(r.repo, []).append(r)
    for repo, ramas in sorted(por_repo.items(), key=lambda t: -len(t[1])):
        vieja = max(ramas, key=lambda r: r.dias)
        npush = sum(1 for r in ramas if not r.pusheada)
        h.append(
            f"[yellow]{repo}[/]: {len(ramas)} ramas sin mergear a "
            f"'{rel.bases.get(repo, '?')}', la mas vieja de {vieja.dias}d "
            f"({vieja.nombre}); [red]{npush} nunca pusheadas[/]"
        )
        # No son N puntas sueltas si la mayoria ya paso por la rama intermedia:
        # esas esperan UN merge, no N. La distincion cambia por donde empezar.
        integradas = [r for r in ramas if r.integrada_en]
        if integradas:
            porintermedia: dict[str, int] = {}
            for r in integradas:
                porintermedia[r.integrada_en] = porintermedia.get(r.integrada_en, 0) + 1
            detalle = ", ".join(f"{n} ya en '{k}'" for k, n in porintermedia.items())
            h.append(
                f"  [dim]de esas, {detalle}: esperan un solo merge de la intermedia, "
                f"no {len(ramas)}. Sueltas de verdad: "
                f"{len(ramas) - len(integradas)}[/]"
            )

    for wt in rel.worktrees:
        if (p := wt.problema(rel.bases.get(wt.repo, ""))):
            h.append(f"[red]worktree miente[/] {wt.repo}: {Path(wt.path).name} -> {p}")

    # `setup-matt-pocock-skills` deja este archivo versionado en la raiz del
    # repo, y con el entran las skills que guardan el estado del trabajo en un
    # tracker por repo -- la invariante inversa a la de factoria, donde los .md
    # son canonicos. Se descarto, asi que si aparece es que alguien corrio el
    # setup: la decision se sostiene porque se mide, no porque este escrita.
    intrusos = [r.name for r in rel.repos if (r / "docs" / "agents").is_dir()]
    if intrusos:
        h.append(
            f"[yellow]tracker ajeno[/] docs/agents/ versionado en "
            f"{', '.join(intrusos)}: lo escribe `setup-matt-pocock-skills`, que "
            "esta descartado (factoria skills). Borralo o el `code-review` va a "
            "leer un tracker que no es el tuyo"
        )

    for repo, (linea, largo) in sorted(rel.estado_actual.items(), key=lambda t: -t[1][1]):
        if largo > COTA_ESTADO_ACTUAL:
            h.append(
                f"[red]cota[/] {repo}\\CLAUDE.md 'Estado de la Mision' (linea {linea}): "
                f"{largo} lineas, cota {COTA_ESTADO_ACTUAL} "
                f"({largo // COTA_ESTADO_ACTUAL}x)"
            )

    excedidos = [(n, c) for n, c in rel.contratos if c > COTA_CONTRATO]
    if excedidos:
        peores = ", ".join(f"{n} ({c})" for n, c in excedidos[:3])
        h.append(
            f"[red]cota[/] {len(excedidos)} de {len(rel.contratos)} contratos pasan "
            f"{COTA_CONTRATO} lineas; se leen enteros al abrir sesion. Peores: {peores}"
        )

    # Agregado: 22 lineas de "cota excedida" son ruido, no hallazgo.
    gordos = sorted(
        (d for d in rel.docs if d.lineas > COTA_DOC_TRABAJO), key=lambda d: -d.lineas
    )
    if gordos:
        peores = ", ".join(f"{d.slug} ({d.lineas})" for d in gordos[:3])
        h.append(
            f"[yellow]cota[/] {len(gordos)} de {len(rel.docs)} docs de trabajo pasan "
            f"{COTA_DOC_TRABAJO} lineas. Peores: {peores}"
        )

    # Solo las sesiones que realmente podrias reanudar: las viejas no importan.
    caras = [s for s in rel.sesiones if s.mb >= UMBRAL_SESION_MB and s.dias <= DIAS_SESION_VIVA]
    if caras:
        h.append(
            f"[yellow]sesiones[/] {len(caras)} activas (<={DIAS_SESION_VIVA}d) por encima de "
            f"{UMBRAL_SESION_MB:.1f} MB, donde el costo por turno ya toco el techo "
            "(0,7x de reconstruirlas, medido): "
            + ", ".join(f"{s.rama or s.proyecto} ({s.mb:.1f}MB)" for s in caras[:3])
            + (f" y {len(caras) - 3} mas" if len(caras) > 3 else "")
            + f". El tamano de aca en mas no cambia el costo, asi que no hay una "
              f"mas urgente que otra: cortar la que vayas a seguir mas de "
              f"{TURNOS_PARA_QUE_CONVENGA} turnos  ->  factoria cortar <rama> --fork"
        )

    # El agujero de trazabilidad: una tarea con N conversaciones y ningun indice.
    # 'HEAD' es detached, no una rama: agruparlo juntaria tareas sin relacion.
    grupos: dict[tuple[str, str], list[Sesion]] = {}
    for s in rel.sesiones:
        if s.dias <= DIAS_SESION_VIVA and s.rama and s.rama != "HEAD":
            grupos.setdefault((s.repo, s.rama), []).append(s)
    multi = sorted(((k, v) for k, v in grupos.items() if len(v) > 1), key=lambda t: -len(t[1]))
    if multi:
        h.append(
            f"[yellow]trazabilidad[/] {len(multi)} ramas tienen mas de una sesion viva "
            f"({sum(len(v) for _, v in multi)} sesiones en total): sin indice, la proxima "
            "vez abris una nueva. Peores: "
            + ", ".join(f"{rm} ({len(v)})" for (_, rm), v in multi[:3])
            + "  ->  factoria sesiones --paralelas"
        )

    # --- Tickets: el estandar §3 medido desde afuera, que es el unico modo en
    # que una cota sobrevive. Las de los .md se auto-vigilaban y decayeron.
    gordos = [t for t in rel.tickets if t.lineas > COTA_TICKET]
    if gordos:
        h.append(
            f"[yellow]cota[/] {len(gordos)} tickets pasan {COTA_TICKET} lineas: "
            + ", ".join(f"{t.slug} ({t.lineas})" for t in gordos[:3])
            + ". El handoff de fase va a .factoria/handoffs/, no adentro del ticket"
        )
    # El espejo es opcional, pero su ausencia tiene que ser visible: un ticket
    # sin issue no esta en el tablero y no hay ningun otro sintoma.
    sin_espejo = [t for t in rel.tickets if t.abierto and not t.issue]
    if sin_espejo:
        h.append(
            f"[yellow]espejo[/] {len(sin_espejo)} tickets abiertos sin issue: "
            + ", ".join(t.slug for t in sin_espejo[:3])
            + ". No estan en el tablero  ->  factoria espejo --todos"
        )
    sin_ses = [(t, e) for t in rel.tickets if t.abierto
               for e in t.repos if not e.session_id]
    if sin_ses:
        h.append(
            f"[yellow]sesion[/] {len(sin_ses)} entradas de ticket sin session_id: "
            + ", ".join(f"{t.slug}/{e.repo}" for t, e in sin_ses[:3])
            + ". Sin eso `resume` no las encuentra"
        )
    # Un ticket abierto cuyo .jsonl no esta en disco: el id quedo reservado y
    # nunca se abrio, o se borro la sesion. Los dos casos rompen `resume`.
    huerfanas = [
        (t, e) for t in rel.tickets if t.abierto
        for e in t.repos if e.session_id and not jsonl_de(e.session_id, e.cuenta)
    ]
    if huerfanas:
        h.append(
            f"[yellow]sesion[/] {len(huerfanas)} sesiones de ticket reservadas pero sin "
            ".jsonl en disco (nunca abiertas, o borradas): "
            + ", ".join(f"{t.slug}/{e.repo}" for t, e in huerfanas[:3])
        )
    # Hotspots: un archivo que tocan muchas ramas sin mergear es conflicto
    # garantizado el dia que se integren. Sale del grafo si esta construido; no
    # se reconstruye aca porque cuesta 10 s.
    g = cargar_json(path_grafo())
    if isinstance(g, dict) and g.get("aristas"):
        cuenta: dict[str, int] = {}
        for ar in g["aristas"]:
            if ar["t"] == "toca" and ar["d"].startswith("archivo:"):
                cuenta[ar["d"]] = cuenta.get(ar["d"], 0) + 1
        top = sorted(cuenta.items(), key=lambda kv: -kv[1])[:3]
        if top and top[0][1] >= 5:
            h.append(
                f"[yellow]conflicto[/] {sum(1 for v in cuenta.values() if v >= 5)} archivos "
                "los tocan 5+ trabajos sin mergear: "
                + ", ".join(f"{Path(n.split('/', 1)[-1]).name} ({v})" for n, v in top)
                + "  ->  factoria grafo --que-toca <archivo>"
            )

    # Solo los abiertos: ver _estado_spec, `close` cambia la huella a proposito.
    derivados = [t for t in rel.tickets
                 if t.abierto and t.spec_congelado
                 and huella_spec(t) != t.spec_congelado]
    if derivados:
        h.append(
            f"[red]spec-drift[/] {len(derivados)} tickets cambiaron criterios o fuera de "
            "alcance despues de aprobarse: " + ", ".join(t.slug for t in derivados[:3])
            + ". No es un error en si; es que lo que se aprobo ya no es lo que dice"
        )
    sin_espejo = [t for t in rel.tickets if t.abierto and not t.issue]
    if sin_espejo and (DATOS / "github.yml").is_file():
        h.append(
            f"[yellow]espejo[/] {len(sin_espejo)} tickets abiertos sin issue: "
            + ", ".join(t.slug for t in sin_espejo[:3])
            + "  ->  factoria espejo --todos"
        )
    en_plan = [t for t in rel.tickets if t.abierto and t.fase == "plan"
               and "## Supuestos abiertos" in t.cuerpo
               and not _tiene_items(t.cuerpo, "## Supuestos abiertos")]
    if en_plan:
        h.append(
            f"[yellow]spec[/] {len(en_plan)} tickets en fase plan con 'Supuestos abiertos' "
            "vacio: " + ", ".join(t.slug for t in en_plan[:3])
            + ". Esa seccion vacia no significa que no haya supuestos, significa que no "
            "se escribieron"
        )

    sin_estado = [d for d in rel.docs if not d.explicito]
    if sin_estado:
        h.append(
            f"[yellow]estado[/] {len(sin_estado)} de {len(rel.docs)} docs de trabajo "
            f"sin campo Estado explicito (inferido de la prosa)"
        )

    # Tiene el campo pero su valor no dice un estado: el problema de los 9
    # vocabularios, hecho accionable.
    ilegibles = [d for d in rel.docs if d.explicito and d.estado == "sin_estado"]
    if ilegibles:
        h.append(
            f"[yellow]estado[/] {len(ilegibles)} docs tienen campo Estado con un valor "
            f"que no nombra un estado: " + ", ".join(d.slug for d in ilegibles[:3])
        )

    if sin_push:
        h.append(
            f"[red]riesgo[/] {len(sin_push)} ramas solo existen en este disco: "
            "sin push no hay backup ni revision posible"
        )
    return h


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


@click.group()
def cli() -> None:
    """factoria - tickets, sesiones y documentacion."""


@cli.command()
@click.option("--docs/--no-docs", default=True, help="Tabla de docs de trabajo.")
@click.option("--ramas/--no-ramas", default=True, help="Tabla de ramas sin mergear.")
@click.option("--sesiones/--no-sesiones", default=True, help="Resumen de sesiones.")
@click.option("--limite", default=12, show_default=True, help="Filas por tabla.")
@click.option("--json", "como_json", is_flag=True, help="Volcado crudo, sin formato.")
def board(docs: bool, ramas: bool, sesiones: bool, limite: int, como_json: bool) -> None:
    """Releva el estado real del ecosistema. Solo lectura: no escribe nada."""
    rel = relevar()

    if como_json:
        click.echo(json.dumps({
            "repos": [r.name for r in rel.repos],
            "bases": rel.bases,
            "ramas_sin_mergear": [vars(r) for r in rel.ramas],
            "worktrees": [
                {**{k: v for k, v in vars(w).items()},
                 "problema": w.problema(rel.bases.get(w.repo, ""))}
                for w in rel.worktrees
            ],
            "docs": [{**vars(d), "path": str(d.path), "slug": d.slug} for d in rel.docs],
            "sesiones": [vars(s) for s in rel.sesiones],
            "contratos": rel.contratos,
            "estado_actual": rel.estado_actual,
        }, indent=2, ensure_ascii=False, default=str))
        return

    abiertos = [t for t in rel.tickets if t.abierto]
    console.print()
    console.print(
        f"[bold]factoria board[/]  |  {len(rel.repos)} repos  |  "
        f"{len(abiertos)} tickets  |  {len(rel.docs)} docs  |  "
        f"{len(rel.ramas)} ramas sin mergear  |  {len(rel.sesiones)} sesiones"
    )

    if abiertos:
        t = Table(title="Tickets en vuelo", title_justify="left", header_style="bold")
        t.add_column("slug", overflow="fold")
        t.add_column("fase")
        t.add_column("repos")
        t.add_column("ses")
        t.add_column("lin", justify="right")
        for tk in sorted(abiertos, key=lambda x: (FASES.index(x.fase) if x.fase in FASES else 9,
                                                  x.slug))[:limite]:
            con_ses = sum(1 for e in tk.repos if e.session_id)
            t.add_row(tk.slug, tk.fase,
                      ", ".join(f"{e.repo}({e.cuenta})" for e in tk.repos) or "[red]-[/]",
                      f"{con_ses}/{len(tk.repos)}", str(tk.lineas))
        console.print(t)

    # El indice se regenera aca y no en cada `new`/`fase`: es derivado.
    regenerar_indice()

    if docs and rel.docs:
        t = Table(title="Docs de trabajo", title_justify="left", header_style="bold")
        t.add_column("repo")
        # fold en vez de elipsis: el caracter de recorte de rich sale como '?'
        # en esta consola, y los slugs largos son justo lo que hay que leer.
        t.add_column("slug", overflow="fold")
        t.add_column("estado")
        t.add_column("src")
        for c in ("pend", "hecho", "lin"):
            t.add_column(c, justify="right")
        for d in sorted(rel.docs, key=lambda d: (-d.pendientes, d.repo))[:limite]:
            t.add_row(
                d.repo, d.slug, d.estado, "exp" if d.explicito else "inf",
                str(d.pendientes), str(d.hechos), str(d.lineas),
            )
        console.print(t)
        if len(rel.docs) > limite:
            console.print(f"  [dim]... y {len(rel.docs) - limite} docs mas[/]")
        tot_p = sum(d.pendientes for d in rel.docs)
        tot_h = sum(d.hechos for d in rel.docs)
        console.print(f"  [dim]checkboxes: {tot_p} pendientes | {tot_h} hechos[/]")

    if ramas and rel.ramas:
        t = Table(title="Ramas sin mergear", title_justify="left", header_style="bold")
        for c in ("repo", "rama", "dias", "push"):
            t.add_column(c, justify="right" if c == "dias" else "left")
        for r in rel.ramas[:limite]:
            t.add_row(r.repo, r.nombre, str(r.dias), "si" if r.pusheada else "[red]NO[/]")
        console.print(t)
        if len(rel.ramas) > limite:
            console.print(f"  [dim]... y {len(rel.ramas) - limite} ramas mas[/]")

    if sesiones and rel.sesiones:
        t = Table(title="Sesiones mas grandes", title_justify="left", header_style="bold")
        for c in ("cuenta", "proyecto", "MB", "dias"):
            t.add_column(c, justify="right" if c in ("MB", "dias") else "left")
        for s in rel.sesiones[:limite]:
            t.add_row(s.cuenta, s.proyecto, f"{s.mb:.1f}", str(s.dias))
        console.print(t)
        for cuenta in CUENTAS:
            de_cuenta = [s for s in rel.sesiones if s.cuenta == cuenta]
            if de_cuenta:
                console.print(
                    f"  [dim]{cuenta}: {len(de_cuenta)} sesiones, "
                    f"{sum(s.mb for s in de_cuenta):.0f} MB en total[/]"
                )

    hs = hallazgos(rel)
    console.print()
    console.print("[bold]HALLAZGOS[/]" + ("" if hs else " - ninguno"))
    for linea in hs:
        console.print(f"  - {linea}")
    console.print()


# --------------------------------------------------------------------------
# Sesiones: inventario, resume, cortar (paso 3)
# --------------------------------------------------------------------------

def _render_sesiones(ss: list[Sesion], encabezado: str = "") -> None:
    """Lista, no tabla: siete columnas no entran en una consola de 80 y rich
    apila cada celda hasta volver la salida ilegible. Dos lineas por sesion:
    identidad arriba, ubicacion abajo."""
    if encabezado:
        console.print(f"[bold]{encabezado}[/]")
    for i, s in enumerate(ss, 1):
        marca = "[yellow]*[/]" if s.mb >= UMBRAL_SESION_MB else " "
        console.print(
            f"{marca}[bold]{i:>3}[/]  [dim]{s.mb:>5.1f}MB {s.dias:>3}d[/]  "
            f"{s.cuenta}/{s.repo}  {s.titulo or '[dim](sin titulo)[/]'}"
        )
        # Una sesion que empezo en otra rama es justo la que no conviene reanudar
        # a ciegas: lo que buscás puede estar en la rama que dejo atras.
        extra = f"  [dim](era {s.rama_inicial})[/]" if s.cambio_de_rama else ""
        console.print(f"      [dim]{s.rama or '(sin rama)'}[/]{extra}")


def _lanzar_en(cuenta: str, cwd: str, args: list[str], forzar: bool, imprimir: bool,
               nota: str = "") -> bool:
    """Arranca claude con el CLAUDE_CONFIG_DIR de la cuenta y el cwd dados.

    Devuelve si de verdad lanzo algo: con `--imprimir` o adentro de otra sesion
    no lanza, y quien registro un uuid antes de llamar necesita saberlo para no
    dejar el ticket apuntando a una sesion que nunca se abrio.
    """
    config_dir = CUENTAS[cuenta]
    # El repo de datos esta afuera de TODOS los repos de codigo, asi que sin
    # esto lo primero que hace cualquier sesion -- leer su ticket, su doc de
    # trabajo, o el pack que le inyecta `resume --nueva` -- se para en un pedido
    # de permiso por path fuera del proyecto. Se vio en la prueba del ultimo
    # criterio: la sesion nueva arranco, se titulo, y no hizo nada mas.
    # Habilita ese directorio y nada mas: no es un --dangerously-skip.
    args = ["--add-dir", str(DATOS), *args]
    # list2cmdline y no un join: uno de los args puede ser el prompt inicial,
    # que lleva espacios y comillas.
    receta = (f"set CLAUDE_CONFIG_DIR={config_dir}\n"
              f"cd /d {cwd}\n"
              f"claude {subprocess.list2cmdline(args)}")
    if imprimir:
        console.print(receta)
        return False
    if not cwd or not Path(cwd).is_dir():
        raise click.ClickException(
            f"el cwd registrado no existe: {cwd or '(vacio)'}\n"
            "Si el worktree se borro, recrealo; o usa --imprimir para ver el comando."
        )
    if os.environ.get("CLAUDECODE") and not forzar:
        console.print(
            "[yellow]Estas adentro de una sesion de Claude.[/] Anidar otra encima consume "
            "tokens de las dos y el output se mezcla. Corre esto en una terminal aparte:\n"
        )
        console.print(receta)
        console.print("\n[dim](o --forzar si de verdad querias anidarla)[/]")
        return False
    binario = shutil.which("claude")
    if not binario:
        raise click.ClickException("no encuentro `claude` en el PATH.")
    entorno = os.environ.copy()
    entorno["CLAUDE_CONFIG_DIR"] = str(config_dir)
    console.print(f"[dim]{cuenta} | {cwd}{' | ' + nota if nota else ''}[/]")
    subprocess.run([binario, *args], cwd=cwd, env=entorno)
    return True


def _elegir_una(ss: list[Sesion], consulta: str, elegir: int | None,
                accion: str) -> Sesion:
    if not ss:
        raise click.ClickException(
            f"ninguna sesion matchea '{consulta}'.\n"
            "Probá `factoria sesiones` para ver el inventario, o acotá menos la consulta."
        )
    if elegir is not None:
        if not 1 <= elegir <= len(ss):
            raise click.ClickException(f"--elegir {elegir} fuera de rango (1..{len(ss)}).")
        return ss[elegir - 1]
    if len(ss) == 1:
        return ss[0]
    TOPE = 12
    _render_sesiones(
        ss[:TOPE],
        f"{len(ss)} sesiones matchean '{consulta}'"
        + (f" -- las {TOPE} mas recientes" if len(ss) > TOPE else ""),
    )
    console.print(
        f"\nElegí una: [bold]factoria {accion} {consulta} --elegir N[/]  "
        "(la #1 es la mas reciente)"
    )
    if len(ss) > TOPE:
        console.print("[dim]O acotá la busqueda: --cuenta dfv|personal, --dias N, o una "
                      "consulta mas especifica (la rama discrimina mejor que el repo).[/]")
    raise SystemExit(1)


@cli.command()
@click.option("--repo", help="Filtrar por repo o slug de proyecto.")
@click.option("--rama", help="Filtrar por rama.")
@click.option("--cuenta", type=click.Choice(list(CUENTAS)), help="Filtrar por cuenta.")
@click.option("--dias", default=DIAS_SESION_VIVA, show_default=True,
              help="Solo sesiones tocadas hace <= N dias. 0 = todas.")
@click.option("--limite", default=20, show_default=True, help="Filas.")
@click.option("--paralelas", is_flag=True,
              help="Solo ramas con MAS DE UNA sesion: el agujero de trazabilidad.")
@click.option("--json", "como_json", is_flag=True, help="Volcado crudo.")
def sesiones(repo: str | None, rama: str | None, cuenta: str | None, dias: int,
             limite: int, paralelas: bool, como_json: bool) -> None:
    """Inventario de sesiones con su repo, rama, titulo y tamano. Solo lectura."""
    ss = inventario_sesiones()
    total = len(ss)
    if cuenta:
        ss = [s for s in ss if s.cuenta == cuenta]
    if repo:
        q = repo.lower()
        ss = [s for s in ss if q in s.repo.lower() or q in s.proyecto.lower()]
    if rama:
        q = rama.lower()
        ss = [s for s in ss if q in s.rama.lower() or q in s.rama_inicial.lower()]
    if dias:
        ss = [s for s in ss if s.dias <= dias]

    if como_json:
        click.echo(json.dumps([{**vars(s), "repo": s.repo} for s in ss],
                              indent=2, ensure_ascii=False))
        return

    if paralelas:
        grupos: dict[tuple[str, str], list[Sesion]] = {}
        sin_rama = 0
        for s in ss:
            # 'HEAD' es detached, no una rama: agrupar por eso juntaria sesiones
            # de tareas sin ninguna relacion. Se cuentan aparte.
            if not s.rama or s.rama == "HEAD":
                sin_rama += 1
                continue
            grupos.setdefault((s.repo, s.rama), []).append(s)
        multi = sorted(((k, v) for k, v in grupos.items() if len(v) > 1),
                       key=lambda t: -len(t[1]))
        console.print()
        console.print(f"[bold]Ramas con mas de una sesion[/]  |  <= {dias}d  |  "
                      f"{len(multi)} ramas, {sum(len(v) for _, v in multi)} sesiones")
        t = Table(title_justify="left", header_style="bold")
        t.add_column("repo")
        t.add_column("rama", overflow="fold")
        t.add_column("ses", justify="right")
        t.add_column("cuentas")
        t.add_column("MB", justify="right")
        for (rp, rm), v in multi[:limite]:
            cs = sorted({x.cuenta for x in v})
            t.add_row(rp, rm, str(len(v)), "+".join(cs), f"{sum(x.mb for x in v):.1f}")
        console.print(t)
        console.print("  [dim]cada fila es una tarea a la que le vas a abrir una sesion "
                      "mas por no saber cual reanudar[/]")
        if sin_rama:
            console.print(f"  [dim]{sin_rama} sesiones sin rama o en detached HEAD, "
                          "no agrupables[/]")
        console.print()
        return

    console.print()
    console.print(f"[bold]Sesiones[/]  |  {len(ss)} de {total}"
                  f"  |  {sum(s.mb for s in ss):.0f} MB")
    _render_sesiones(sorted(ss, key=lambda s: -s.mtime)[:limite])
    if len(ss) > limite:
        console.print(f"  [dim]... y {len(ss) - limite} mas[/]")
    console.print()


@dataclass
class Destino:
    """A donde apunta un `resume`/`cortar`, venga de un ticket o de una busqueda."""
    cuenta: str
    cwd: str
    session_id: str
    nota: str = ""
    existe: bool = True   # el .jsonl ya esta en disco
    mb: float = 0.0
    origen: str = "sesion"
    # Cuando el destino salio de un ticket, quien lo resolvio ya lo tiene: sin
    # esto `cortar` tenia que volver a parsear `origen` para encontrarlo.
    ticket: Ticket | None = None
    entrada: RepoTicket | None = None


def jsonl_de(session_id: str, cuenta: str) -> Path | None:
    if not session_id or cuenta not in CUENTAS:
        return None
    base = CUENTAS[cuenta] / "projects"
    if not base.is_dir():
        return None
    return next(base.rglob(f"{session_id}.jsonl"), None)


def sesion_actual() -> tuple[str, str] | None:
    """El uuid y la cuenta de la sesion de Claude Code desde la que se corre.

    Claude Code exporta las dos cosas al shell de sus herramientas
    (`CLAUDE_CODE_SESSION_ID`, `CLAUDE_CONFIG_DIR`), asi que "la sesion soy yo"
    es el unico candidato a adoptar que no hay que adivinar. Devuelve None
    cuando el comando se corre desde una terminal comun.
    """
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if not sid:
        return None
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    cta = next((c for c, raiz in CUENTAS.items()
                if cfg and Path(cfg).name.lower() == raiz.name.lower()), None)
    # Sin CLAUDE_CONFIG_DIR queda buscar el .jsonl: el uuid es unico entre las
    # dos cuentas (0 en comun sobre 337), asi que encontrarlo la identifica.
    cta = cta or next((c for c in CUENTAS if jsonl_de(sid, c)), None)
    return (sid, cta) if cta else None


def sesiones_que_nombran(aguja: str) -> set[str]:
    """Ids de las sesiones cuyo transcript contiene `aguja`.

    Es la evidencia que convierte a `adoptar` en deteccion en vez de adivinanza:
    si el slug esta en el .jsonl, en esa sesion se corrio un comando de factoria
    con el slug o se abrio el ticket. Barrer los 337 archivos enteros son 359 MB
    y 0.7 s, mas barato que adoptar la sesion equivocada.
    """
    if not aguja:
        return set()
    blob = aguja.encode("utf-8")
    out: set[str] = set()
    for raiz in CUENTAS.values():
        base = raiz / "projects"
        if not base.is_dir():
            continue
        for jsonl in base.rglob("*.jsonl"):
            try:
                with jsonl.open("rb") as fh:
                    previo = b""
                    while trozo := fh.read(1 << 20):
                        if blob in previo + trozo:
                            out.add(jsonl.stem)
                            break
                        # El solape evita perder un match partido por el corte.
                        previo = trozo[-len(blob):]
            except OSError:
                continue
    return out


def resolver_destino(consulta: str, repo: str | None, cuenta: str | None,
                     elegir: int | None, dias: int, accion: str) -> Destino:
    """El ticket manda: es la clave primaria de una tarea. Solo si el slug no
    matchea ningun ticket se cae a la busqueda por texto sobre las sesiones."""
    t = next((x for x in tickets_todos()
              if x.slug == consulta or consulta.lower() in x.slug.lower()), None)
    if t:
        entradas = t.repos
        if repo:
            entradas = [e for e in entradas if repo.lower() in e.repo.lower()]
        if cuenta:
            entradas = [e for e in entradas if e.cuenta == cuenta]
        if not entradas:
            raise click.ClickException(
                f"el ticket '{t.slug}' no tiene entrada para esos filtros. Repos: "
                + ", ".join(f"{e.repo}({e.cuenta})" for e in t.repos)
                + (f"\nLos transcripts de las dos cuentas son disjuntos, asi que en "
                   f"{cuenta} no hay conversacion que continuar. Para trabajarlo ahi:"
                   f"\n  factoria resume {t.slug} --nueva --cuenta {cuenta}"
                   if cuenta else "")
            )
        if len(entradas) > 1:
            raise click.ClickException(
                f"'{t.slug}' tiene {len(entradas)} repos. Elegí uno con --repo: "
                + ", ".join(f"{e.repo}({e.cuenta})" for e in entradas)
            )
        e = entradas[0]
        j = jsonl_de(e.session_id, e.cuenta)
        return Destino(
            cuenta=e.cuenta, cwd=e.cwd, session_id=e.session_id,
            nota=f"{e.repo} | {e.rama or '(sin rama)'} | fase {t.fase}",
            existe=j is not None,
            mb=(j.stat().st_size / 1_048_576) if j else 0.0,
            origen=f"ticket {t.slug}",
            ticket=t, entrada=e,
        )
    ss = buscar_sesiones(consulta, cuenta, dias or None)
    if repo:
        ss = [s for s in ss if repo.lower() in s.repo.lower()]
    s = _elegir_una(ss, consulta, elegir, accion)
    return Destino(cuenta=s.cuenta, cwd=s.cwd, session_id=s.session_id,
                   nota=s.rama or "(sin rama)", existe=True, mb=s.mb)


def _arrancar_fresca(t: Ticket, e: RepoTicket, cuenta: str,
                     imprimir: bool, forzar: bool) -> None:
    """Sesion nueva para un ticket que ya existe: la registra y le pasa el pack.

    Es lo que faltaba para que "seguir este ticket en la otra cuenta" sea un
    comando y no un ritual de tres pasos. Dos cosas que no puede saltear:
    registrar el uuid ANTES de lanzar, porque si no la sesion nace huerfana y
    `resume` vuelve a abrir cualquier cosa; y pasarle el pack de contexto,
    porque una sesion fresca no sabe nada del ticket.

    Si al final no se lanzo nada, el registro se revierte: dejar escrito un uuid
    que nadie va a abrir es exactamente el fantasma que `adoptar` viene a
    arreglar.
    """
    nuevo = str(uuid.uuid4())
    previa, cuenta_previa = e.session_id, e.cuenta
    CONTEXTOS.mkdir(parents=True, exist_ok=True)
    pack = CONTEXTOS / f"{t.slug}.md"
    pack.write_text(pack_de_contexto(t.slug), encoding="utf-8")
    prompt = (f"Retomas el ticket {t.slug}. Antes que nada lee {pack}: es el pack "
              "de contexto que armo factoria, con el ticket, el estado de su doc "
              "de trabajo y sus vecinos. Despues deci en que estado esta y cual "
              "es el proximo paso, sin tocar nada todavia.")
    args = ["--session-id", nuevo, prompt]
    nota = f"{e.repo} | {e.rama or '(sin rama)'} | fase {t.fase}"
    console.print(f"[bold]{t.slug}/{e.repo}[/]  sesion nueva {nuevo} [dim]({cuenta})[/]")
    console.print(f"  [dim]pack: {pack}[/]")
    if previa:
        console.print(
            f"  [dim]la anterior ({previa}, cuenta {cuenta_previa}) queda intacta "
            f"pero sin ticket que la apunte. `factoria adoptar {t.slug} --cuenta "
            f"{cuenta_previa}` la vuelve a encontrar: nombra el slug.[/]")
    if imprimir:
        _lanzar_en(cuenta, e.cwd, args, forzar, True, nota)
        console.print("  [dim]--imprimir: el ticket quedo intacto.[/]")
        return
    e.session_id, e.cuenta = nuevo, cuenta
    escribir_ticket(t)
    try:
        lanzo = _lanzar_en(cuenta, e.cwd, args, forzar, False, nota)
    except Exception:
        e.session_id, e.cuenta = previa, cuenta_previa
        escribir_ticket(t)
        raise
    if not lanzo:
        e.session_id, e.cuenta = previa, cuenta_previa
        escribir_ticket(t)
        console.print("  [dim]no se lanzo nada, asi que el ticket volvio a apuntar "
                      f"a {previa or '(ninguna)'}.[/]")


@cli.command()
@click.argument("consulta")
@click.option("--repo", help="Cuando el ticket tiene sesiones en varios repos.")
@click.option("--cuenta", type=click.Choice(list(CUENTAS)), help="Acotar a una cuenta.")
@click.option("--elegir", type=int, help="Indice de la lista cuando matchean varias.")
@click.option("--dias", default=0, help="Solo sesiones de hace <= N dias. 0 = todas.")
@click.option("--nueva", is_flag=True,
              help="No continuar: abrir una sesion NUEVA con el pack de contexto "
                   "del ticket y registrarla. Con --cuenta, en esa cuenta.")
@click.option("--imprimir", is_flag=True, help="Mostrar el comando sin ejecutarlo.")
@click.option("--forzar", is_flag=True, help="Permitir anidar dentro de otra sesion.")
def resume(consulta: str, repo: str | None, cuenta: str | None, elegir: int | None,
           dias: int, nueva: bool, imprimir: bool, forzar: bool) -> None:
    """Reanuda la sesion de una tarea: continua la conversacion, no abre otra.

    `--nueva` es el caso en que no hay conversacion que continuar: el ticket
    vive en la otra cuenta (los transcripts son disjuntos), o la sesion se puso
    cara. Abre una sesion nueva en la cuenta que se pida, le pasa el pack de
    `contexto` como primer prompt y la deja registrada en el ticket.
    """
    if nueva:
        tk = buscar_ticket(consulta)
        _arrancar_fresca(tk, _entrada_unica(tk, repo), cuenta or _entrada_unica(
            tk, repo).cuenta, imprimir, forzar)
        return
    d = resolver_destino(consulta, repo, cuenta, elegir, dias, "resume")
    if not d.existe:
        # Ticket recien creado con --no-lanzar: el id esta reservado pero el
        # .jsonl todavia no existe, asi que `-r` no lo encontraria.
        console.print(f"[dim]{d.origen}: primera apertura, sesion {d.session_id}[/]")
        _lanzar_en(d.cuenta, d.cwd, ["--session-id", d.session_id], forzar, imprimir, d.nota)
        return
    if d.mb >= UMBRAL_SESION_MB:
        console.print(
            f"[yellow]Ojo:[/] esta sesion pesa {d.mb:.1f} MB, sobre el umbral de "
            f"{UMBRAL_SESION_MB}: su costo por turno ya esta en el techo, 0,7x de "
            "reconstruirla. Arriba de 1 MB el tamano no cambia nada -- la "
            "compactacion vacia el prefijo, y el .jsonl solo acumula el registro.\n"
            f"[dim]Cortar se paga despues de ~{TURNOS_PARA_QUE_CONVENGA} turnos, o sea "
            "conviene si vas a seguir trabajando y no para una pregunta:\n"
            f"  factoria cortar {consulta} --fork   (ramifica, conserva el contexto)\n"
            f"  factoria resume {consulta} --nueva  (limpia, con el pack del ticket)[/]\n"
        )
    _lanzar_en(d.cuenta, d.cwd, ["-r", d.session_id], forzar, imprimir, d.nota)


@cli.command()
@click.argument("consulta")
@click.option("--fork", is_flag=True,
              help="Ramificar desde la sesion actual, preservandola intacta.")
@click.option("--repo", help="Cuando el ticket tiene sesiones en varios repos.")
@click.option("--cuenta", type=click.Choice(list(CUENTAS)))
@click.option("--elegir", type=int)
@click.option("--imprimir", is_flag=True)
@click.option("--forzar", is_flag=True)
def cortar(consulta: str, fork: bool, repo: str | None, cuenta: str | None,
           elegir: int | None, imprimir: bool, forzar: bool) -> None:
    """Corta una sesion cara. Con --fork ramifica; sin el, arranca limpia."""
    d = resolver_destino(consulta, repo, cuenta, elegir, 0, "cortar")
    if fork:
        if not d.existe:
            raise click.ClickException(
                f"la sesion {d.session_id} todavia no existe en disco: no hay de donde "
                f"ramificar. Abrila primero con `factoria resume {consulta}`."
            )
        _lanzar_en(d.cuenta, d.cwd, ["-r", d.session_id, "--fork-session"],
                   forzar, imprimir, d.nota)
        return
    if d.ticket and d.entrada:
        _arrancar_fresca(d.ticket, d.entrada, d.cuenta, imprimir, forzar)
        return
    # Sin ticket detras no hay pack que armar ni donde registrar el uuid: el
    # destino salio de la busqueda por texto sobre las sesiones.
    console.print(
        f"[yellow]Sesion nueva y limpia[/] en {d.cwd} ({d.cuenta}) | {d.nota}\n"
        f"[dim]La anterior queda intacta. Sin ticket detras arranca solo con el "
        "CLAUDE.md del repo: si querias el pack, corrilo por slug de ticket.[/]\n"
    )
    _lanzar_en(d.cuenta, d.cwd, [], forzar, imprimir, d.nota)


# --------------------------------------------------------------------------
# Tickets y docs de trabajo (paso 4). El estandar de documentacion del plan §3
# no se declara en un .md: se materializa en las plantillas y lo mide `board`.
# Toda cota que se auto-vigilaba decayo.
# --------------------------------------------------------------------------

FASES = ("plan", "dev", "test", "cerrado")
COTA_TICKET = 120
RE_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
RE_FRONT = re.compile(r"\A---\s*\n(.*?)\n---[ \t]*\n?", re.S)


@dataclass
class RepoTicket:
    repo: str
    cuenta: str
    rama: str = ""
    cwd: str = ""
    session_id: str = ""
    doc: str = ""


@dataclass
class Ticket:
    slug: str
    fase: str = "plan"
    abierto: bool = True
    spec_congelado: str = ""
    issue: int = 0
    issue_url: str = ""
    proyecto_item: str = ""
    repos: list[RepoTicket] = field(default_factory=list)
    cuerpo: str = ""
    path: Path | None = None

    def entrada(self, repo: str) -> RepoTicket | None:
        r = repo.lower()
        return next((e for e in self.repos if e.repo.lower() == r), None)

    def texto(self) -> str:
        fm = {
            "slug": self.slug,
            "fase": self.fase,
            "abierto": self.abierto,
            "spec_congelado": self.spec_congelado,
            "issue": self.issue,
            "issue_url": self.issue_url,
            "proyecto_item": self.proyecto_item,
            "repos": [dict(vars(e)) for e in self.repos],
        }
        y = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True,
                           default_flow_style=False).rstrip()
        return f"---\n{y}\n---\n\n{self.cuerpo.lstrip()}"

    @property
    def lineas(self) -> int:
        return len(self.texto().splitlines())


def leer_ticket(p: Path) -> Ticket | None:
    try:
        txt = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = RE_FRONT.match(txt)
    if not m:
        return None
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(fm, dict):
        return None
    repos = [
        RepoTicket(
            repo=str(e.get("repo", "")), cuenta=str(e.get("cuenta", "")),
            rama=str(e.get("rama") or ""), cwd=str(e.get("cwd") or ""),
            session_id=str(e.get("session_id") or ""), doc=str(e.get("doc") or ""),
        )
        for e in (fm.get("repos") or [])
        if isinstance(e, dict) and e.get("repo")
    ]
    return Ticket(
        slug=str(fm.get("slug") or p.stem),
        fase=str(fm.get("fase") or "plan"),
        abierto=bool(fm.get("abierto", True)),
        spec_congelado=str(fm.get("spec_congelado") or ""),
        issue=int(fm.get("issue") or 0),
        issue_url=str(fm.get("issue_url") or ""),
        proyecto_item=str(fm.get("proyecto_item") or ""),
        repos=repos,
        cuerpo=txt[m.end():],
        path=p,
    )


def escribir_ticket(t: Ticket) -> None:
    assert t.path is not None
    t.path.parent.mkdir(parents=True, exist_ok=True)
    t.path.write_text(t.texto(), encoding="utf-8", newline="\n")


def dir_tickets() -> Path:
    return DATOS / "tickets"


def tickets_todos() -> list[Ticket]:
    d = dir_tickets()
    if not d.is_dir():
        return []
    return [t for p in sorted(d.glob("*.md")) if (t := leer_ticket(p))]


def buscar_ticket(slug: str) -> Ticket:
    p = dir_tickets() / f"{slug}.md"
    t = leer_ticket(p) if p.is_file() else None
    if t:
        return t
    # Coincidencia parcial: los slugs son largos y tipearlos completos es friccion.
    cands = [x for x in tickets_todos() if slug.lower() in x.slug.lower()]
    if len(cands) == 1:
        return cands[0]
    if not cands:
        raise click.ClickException(
            f"no hay ticket que matchee '{slug}'. `factoria tickets` lista los que hay."
        )
    raise click.ClickException(
        "ambiguo, matchean: " + ", ".join(c.slug for c in cands[:8])
    )


# --------------------------------------------------------------------------
# Perfiles por repo
# --------------------------------------------------------------------------

# `docs: ai-tasks` = el repo versiona sus docs y van adentro (Cotizaciones).
# `docs: central`  = el repo los ignora, asi que van al repo de datos, donde si
#                    tienen historial y respaldo (defeve ignora `.ia/` entero).
PERFIL_DEFECTO = {"cuenta": "dfv", "docs": "central"}


def normalizar_slug(bruto: str) -> str:
    """`rediseño web` -> `rediseno-web`.

    El slug termina siendo nombre de archivo, componente de rama, label de
    GitHub y clave del grafo. Rechazar la ñ seria correcto y molesto: se
    normaliza y se avisa. NFKD descompone la ñ en n + tilde combinante, y
    despues se descartan las marcas.
    """
    s = unicodedata.normalize("NFKD", bruto)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()


def repo_del_cwd() -> Path | None:
    """El repo descubierto que contiene el directorio actual, si hay uno."""
    try:
        aqui = Path.cwd().resolve()
    except OSError:
        return None
    for r in descubrir_repos():
        try:
            aqui.relative_to(r.resolve())
        except (ValueError, OSError):
            continue
        return r
    return None


def ruta_repo(nombre: str) -> Path | None:
    n = nombre.lower()
    repos = descubrir_repos()
    for r in repos:
        if r.name.lower() == n:
            return r
    cands = [r for r in repos if n in r.name.lower()]
    return cands[0] if len(cands) == 1 else None


def perfil(repo: str) -> dict:
    p = DATOS / "profiles" / f"{repo}.yml"
    if p.is_file():
        try:
            d = yaml.safe_load(p.read_text(encoding="utf-8", errors="replace"))
            if isinstance(d, dict):
                return {**PERFIL_DEFECTO, **d}
        except (OSError, yaml.YAMLError):
            pass
    return dict(PERFIL_DEFECTO)


def cuenta_dominante() -> dict[str, str]:
    """Con que cuenta trabajas cada repo, segun el historial real de sesiones.

    Poner `dfv` por defecto en todos seria adivinar, y adivina mal:
    dfv-automatizacion tiene 12 sesiones en `personal` contra 8 en `dfv`.
    """
    conteo: dict[str, dict[str, int]] = {}
    for s in inventario_sesiones():
        if s.repo:
            conteo.setdefault(s.repo, {}).setdefault(s.cuenta, 0)
            conteo[s.repo][s.cuenta] += 1
    return {repo: max(c, key=c.get) for repo, c in conteo.items() if c}


def sembrar_perfiles(sobrescribir: bool = False) -> list[str]:
    """Genera un perfil por repo descubierto, detectando la convencion de docs."""
    d = DATOS / "profiles"
    d.mkdir(parents=True, exist_ok=True)
    dominante = cuenta_dominante()
    escritos = []
    for r in descubrir_repos():
        p = d / f"{r.name}.yml"
        if p.is_file() and not sobrescribir:
            continue
        pf = {
            "cuenta": dominante.get(r.name, "dfv"),
            "docs": "ai-tasks" if (r / ".ai" / "tasks").is_dir() else "central",
            "base": base_de(r) or "main",
            # Guardas heredadas de los hooks Stop que este comando reemplaza.
            "ramas_prohibidas": [
                b for b in ("master", "main", *BASES_CANDIDATAS)
                if b in ("master", "main") or existe_rama(r, b)
            ],
            "proteger_commit": (
                ["api-grails/grails-app/conf/DataSource.groovy"]
                if (r / "api-grails" / "grails-app" / "conf" / "DataSource.groovy").exists()
                else []
            ),
            "excluir_commit": ["**/__pycache__", "**/*.pyc", "**/*.pyo"],
            # Ritual de §10.1/§10.3, solo donde ya lo usas de verdad.
            "worktree": (r.parent / "wt").is_dir(),
            "worktree_raiz": str(r.parent / "wt"),
            "copiar_al_worktree": [
                p for p in ("CLAUDE.md", ".claude/settings.local.json",
                            "api-grails/grails-app/conf/DataSource.groovy",
                            "intranet/src/environments/environment.ts")
                if (r / p).is_file()
            ],
            "skip_worktree": [
                p for p in ("api-grails/grails-app/conf/DataSource.groovy",)
                if (r / p).is_file()
            ],
            "junctions": [
                p for p in ("api-grails/target", "intranet/node_modules", "node_modules")
                if (r / p).is_dir()
            ],
        }
        p.write_text(
            "# Perfil de repo para factoria. `docs: ai-tasks` mete el doc de trabajo\n"
            "# adentro del repo (.ai/tasks/); `central` lo manda a .factoria/docs/<repo>/\n"
            "# porque el repo lo ignoraria.\n"
            "#\n"
            "# `cuenta` sale del historial real de sesiones de este repo, no de un\n"
            "# default. Si no habia sesiones, quedo en dfv: corregilo a mano.\n"
            + yaml.safe_dump(pf, sort_keys=False, allow_unicode=True),
            encoding="utf-8", newline="\n",
        )
        escritos.append(r.name)
    return escritos


def ruta_doc(repo: str, slug: str) -> Path:
    if perfil(repo).get("docs") == "ai-tasks":
        rp = ruta_repo(repo)
        if rp:
            return rp / ".ai" / "tasks" / f"{slug}.md"
    return DATOS / "docs" / repo / f"{slug}.md"


# --------------------------------------------------------------------------
# Plantillas: el estandar §3, hecho artefacto
# --------------------------------------------------------------------------

CUERPO_TICKET = """# {slug}

## Pedido crudo

{pedido}

## Criterios de aceptación

<!-- Uno por linea, cada uno respondible con si/no y nombrando SU evidencia.
     "funciona bien" no es un criterio; "el listado ordena por fecha desc y el
     test X lo cubre" si. Los escribe la sesion con vos, no vos solo. -->

- [ ]

## Fuera de alcance

<!-- Lo que se decidio NO hacer. Existe para que nadie lo agregue de onda
     despues, y para que `aprobar` pueda congelarlo. -->

-

## Entregables de código

<!-- Que se va a tocar, por unidad con NOMBRE en el sistema: un servicio, un
     endpoint, una tabla, una pantalla, un job, un comando. Verbo adelante
     --nuevo, modifica, borra-- porque crear y tocar algo preexistente no
     cuestan lo mismo de revisar. Sin firmas ni snippets: si necesitas codigo
     para explicarlo, va al doc de trabajo. Si no sabes como nombrarlo no es
     un entregable, es implementacion. Mas de 7 y el ticket son dos tickets.
     Si la unidad declara permisos, el ROL o tipo de usuario permitido va en la
     misma linea: "solo rol admin", "cualquier usuario autenticado", "publico".
     Un permiso sin rol es un permiso que nadie puede revisar.
     En tickets multi-repo, prefijo [repo] en cada linea. -->

-

## Supuestos abiertos

<!-- Cada cosa que el modelo tuvo que adivinar, escrita COMO adivinanza y antes
     de que exista codigo. Es el unico lugar donde una mala interpretacion se
     puede atajar barata. Si esta seccion queda vacia en fase plan, no se
     entendio el pedido: se entendio lo que se quiso entender. -->

-
"""

CUERPO_DOC = """---
ticket: {slug}
repo: {repo}
estado: propuesto
---

# {slug} ({repo})

## Estado actual

<!-- Presente, sin fechas, maximo {cota_estado} lineas. Una fecha aca es un
     error detectable con grep: significa que se filtro un delta. Lo que
     cambio va al historial. -->

Nada ejecutado todavia.

## Archivos tocados

<!-- Separados a proposito: tocar un archivo preexistente tiene un costo de
     revision distinto que crear uno nuevo. -->

**Nuevos:**

**Preexistentes:**

## Decisiones

<!-- Stubs de una linea. `close` los extrae. -->

-
"""


# Las tres que `aprobar` exige no vacias. `Supuestos abiertos` NO esta: ahi
# avisa y sigue, porque un ticket sin supuestos es sospechoso pero no invalido.
EXIGIDAS_PARA_APROBAR = ("## Criterios de aceptación", "## Fuera de alcance",
                         "## Entregables de código")


def crear_doc(repo: str, slug: str) -> Path:
    p = ruta_doc(repo, slug)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            CUERPO_DOC.format(slug=slug, repo=repo, cota_estado=COTA_ESTADO_ACTUAL),
            encoding="utf-8", newline="\n",
        )
    return p


def regenerar_indice() -> Path:
    """INDICE.md se CONSULTA, no se lee al abrir sesion: una linea por ticket."""
    ts = tickets_todos()
    lineas = [
        "# Indice de tickets",
        "",
        "Generado por `factoria board`. No se edita a mano, y no se carga al abrir",
        "una sesion: para eso esta `factoria contexto <slug>`.",
        "",
        "| slug | fase | repos | sesiones | issue |",
        "|---|---|---|---|---|",
    ]
    for t in sorted(ts, key=lambda x: (not x.abierto, x.slug)):
        repos = ", ".join(e.repo for e in t.repos) or "-"
        ses = sum(1 for e in t.repos if e.session_id)
        iss = f"[#{t.issue}]({t.issue_url})" if t.issue_url else "-"
        lineas.append(
            f"| [{t.slug}](tickets/{t.slug}.md) | {t.fase} | {repos} | {ses} | {iss} |"
        )
    lineas.append("")
    p = DATOS / "INDICE.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lineas), encoding="utf-8", newline="\n")
    return p


# --------------------------------------------------------------------------
# Comandos de tickets
# --------------------------------------------------------------------------


# feature/ (51 ramas) y fix/ (41) son tu convencion real en defeve; chore/ (7)
# la sigue. El default es feature porque es el caso mayoritario.
TIPOS_RAMA = ("feature", "fix", "chore")


def existe_rama(rp: Path, nombre: str) -> bool:
    return bool(git(rp, "rev-parse", "--verify", "--quiet", f"refs/heads/{nombre}"))


def _git_o_falla(rp: Path, *args: str) -> None:
    r = subprocess.run(["git", "-C", str(rp), *args], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise click.ClickException(
            f"git {' '.join(args)} falló:\n{(r.stderr or r.stdout or '').strip()[:400]}"
        )


def _resolver_rama(rp: Path, base: str, actual: str, pedida: str | None,
                   slug: str, tipo: str, aqui: bool) -> str:
    """La rama del ticket. Por defecto SIEMPRE una nueva, derivada del slug.

    Usar la rama actual era el pozo: si el repo quedo parado en la feature de
    otra tarea, el ticket nuevo la adoptaba y los commits de la sesion caian en
    la rama equivocada. Con --aqui se puede pedir explicitamente lo contrario.
    """
    if aqui:
        console.print(f"[dim]--aqui: se registra la rama actual, {actual or '(ninguna)'}[/]")
        return actual
    destino = pedida or f"{tipo}/{slug}"
    if actual == destino:
        return actual
    if sucio := git(rp, "status", "--porcelain"):
        raise click.ClickException(
            f"{rp.name} tiene cambios sin commitear: el checkout a '{destino}' los "
            f"arrastraria ahi.\nCommiteálos, guardálos con `git stash`, o usá --aqui "
            f"para quedarte en {actual}.\n\n" + sucio[:400]
        )
    if existe_rama(rp, destino):
        _git_o_falla(rp, "checkout", destino)
        console.print(f"[dim]checkout de la rama existente {destino}[/]")
    else:
        if not base:
            raise click.ClickException(
                f"no puedo determinar la base de {rp.name} para crear '{destino}'. "
                "Seteá `base:` en su perfil."
            )
        _git_o_falla(rp, "checkout", "-b", destino, base)
        console.print(f"[dim]rama nueva {destino} desde {base}[/]")
    return git(rp, "rev-parse", "--abbrev-ref", "HEAD") or destino


@cli.command()
@click.argument("slug")
@click.option("--repo", help="Repo donde arranca. Por defecto, el del directorio actual.")
@click.option("--cuenta", type=click.Choice(list(CUENTAS)),
              help="Override del perfil del repo.")
@click.option("--pedido", help="El pedido crudo, literal. Sin esto se abre el editor.")
@click.option("--rama", "rama_pedida",
              help="Nombre completo de la rama. Por defecto <tipo>/<slug>.")
@click.option("--tipo", type=click.Choice(TIPOS_RAMA), default="feature",
              show_default=True, help="Prefijo de la rama.")
@click.option("--aqui", is_flag=True,
              help="No crear rama: registrar la actual. Escape hatch explicito.")
@click.option("--no-lanzar", is_flag=True, help="Crear el ticket sin abrir la sesion.")
@click.option("--forzar", is_flag=True, help="Permitir anidar dentro de otra sesion.")
def new(slug: str, repo: str | None, cuenta: str | None, pedido: str | None,
        rama_pedida: str | None, tipo: str, aqui: bool, no_lanzar: bool,
        forzar: bool) -> None:
    """Crea un ticket en fase plan y abre su sesion, con id conocido de antemano."""
    crudo, slug = slug, normalizar_slug(slug)
    if not slug:
        raise click.ClickException(f"'{crudo}' no deja nada usable como slug.")
    if slug != crudo:
        console.print(f"[dim]slug normalizado: '{crudo}' -> '{slug}'[/]")
    if not RE_SLUG.match(slug):
        raise click.ClickException(f"slug invalido despues de normalizar: '{slug}'.")
    destino = dir_tickets() / f"{slug}.md"
    if destino.exists():
        raise click.ClickException(
            f"ya existe {destino}.\nSi querias sumarle un repo, eso es `factoria open` "
            f"(paso 9); si querias reanudarlo, `factoria resume {slug}`."
        )
    rp = ruta_repo(repo) if repo else repo_del_cwd()
    if not rp:
        detalle = (f"no encuentro el repo '{repo}'" if repo else
                   "no estas dentro de un repo conocido, asi que hace falta --repo")
        raise click.ClickException(
            detalle + ". Hay: " + ", ".join(r.name for r in descubrir_repos())
        )
    pf = perfil(rp.name)
    cta = cuenta or pf.get("cuenta") or "dfv"
    if cta not in CUENTAS:
        raise click.ClickException(f"cuenta '{cta}' desconocida (perfil de {rp.name}).")

    if not pedido:
        pedido = (click.edit(
            "\n\n<!-- El pedido como te llego: verbal, WhatsApp, lo que sea. LITERAL,\n"
            "     sin interpretar ni ordenar. Esta seccion no se edita nunca mas:\n"
            "     es contra lo que se compara si despues hubo malentendido. -->\n"
        ) or "")
        pedido = "\n".join(l for l in pedido.splitlines()
                           if not l.strip().startswith(("<!--", "-->"))
                           and "El pedido como te llego" not in l
                           and "sin interpretar" not in l
                           and "es contra lo que se compara" not in l).strip()
    if not pedido.strip():
        raise click.ClickException("el pedido crudo quedo vacio: sin eso el ticket no sirve.")

    sid = str(uuid.uuid4())
    base = perfil(rp.name).get("base") or base_de(rp) or ""
    actual = git(rp, "rev-parse", "--abbrev-ref", "HEAD") or ""
    rama = _resolver_rama(rp, base, actual, rama_pedida, slug, tipo, aqui)
    doc = crear_doc(rp.name, slug)
    t = Ticket(
        slug=slug, fase="plan", abierto=True, spec_congelado="",
        repos=[RepoTicket(repo=rp.name, cuenta=cta, rama=rama, cwd=str(rp),
                          session_id=sid, doc=str(doc))],
        cuerpo=CUERPO_TICKET.format(slug=slug, pedido=pedido.strip()),
        path=destino,
    )
    escribir_ticket(t)
    regenerar_indice()

    console.print()
    console.print(f"[bold]{slug}[/]  fase plan  |  {rp.name}  |  cuenta {cta}")
    espejar_si_se_puede(t)
    console.print(f"  ticket   {destino}")
    console.print(f"  doc      {doc}")
    console.print(f"  sesion   {sid}")
    console.print(f"  rama     {rama or '(sin rama)'}")
    console.print()
    console.print("[dim]En la sesion: escribí los criterios de aceptación, los entregables "
                  "de código y, sobre todo, "
                  "los supuestos abiertos.\nEsa seccion vacía en fase plan significa que "
                  "no se entendió el pedido.[/]")
    console.print()
    if no_lanzar:
        console.print(f"[dim]Para abrirla: factoria resume {slug}[/]")
        return
    _lanzar_en(cta, str(rp), ["--session-id", sid], forzar, False, rama)


# --- Skills de mattpocock: cuales se usan, y por que las otras no ------------
#
# El plugin trae 43 skills. Se adoptan las que aportan juicio sobre contenido
# (lo que factoria deliberadamente NO hace) y que no necesitan un tracker.
#
# Quedan afuera `to-spec`, `to-tickets`, `triage` y `wayfinder`: guardan el
# estado del trabajo EN el tracker. factoria ya tiene el suyo y su invariante
# es la inversa -- los .md son canonicos, la API es espejo. Adoptarlas moveria
# la verdad a la red, y `contexto` y `grafo.json` existen para arrancar una
# sesion sin ella.
#
# Y no se corre `setup-matt-pocock-skills`, que es lo que las habilitaria:
# escribe `docs/agents/issue-tracker.md` versionado en cada repo. En defeve no
# se crean directorios versionados -- es la misma razon por la que `.ia/` esta
# gitignored y por la que este perfil tiene `docs: central`.
MP = "/mattpocock-skills"

SKILLS_POR_FASE: dict[str, tuple[tuple[str, str], ...]] = {
    "plan": (
        ("grilling", "llena '## Supuestos abiertos' interrogandote: el unico "
                     "dispositivo anti-malinterpretacion que tiene el ticket"),
        ("prototype", "contesta una duda de diseño con codigo tirable, antes "
                      "de que el criterio de aceptacion la congele"),
        ("to-questionnaire", "cuando la duda la tiene que contestar otro y no vos"),
        ("codebase-design", "vocabulario de modulos profundos; no escribe archivos"),
    ),
    "dev": (
        ("tdd", "los tests que deja son los `verify:` que le faltan al perfil "
                "para que `check` sea un DoD y no un linter de cotas"),
        ("diagnosing-bugs", "para los tickets `fix/`"),
        ("implement", "ejecuta el ticket ya aprobado, sin volver a discutirlo"),
    ),
    "test": (
        ("code-review", "dos ejes, Standards y Spec. Pasale el ticket como spec "
                        "y no necesita tracker: es la opcion 2 de su busqueda"),
        ("resolving-merge-conflicts", "si el compare que imprime `close` sale con conflictos"),
    ),
    "cerrado": (
        ("retro", "que de esta sesion deberia ser un check de `factoria check` "
                  "en vez de una instruccion que hay que recordar"),
        ("writing-for-agents", "para editar las skills propias y los CLAUDE.md"),
    ),
}

# Sirven en cualquier fase, asi que no se repiten en cada cambio de fase.
SKILLS_TRANSVERSALES: tuple[tuple[str, str], ...] = (
    ("handoff", "compacta la conversacion. Es el de `productivity`, homonimo "
                "del propio: aquel arma contratos cross-repo, este comprime "
                "una sesion. Conviven porque va con prefijo"),
    ("wizard", "genera un wizard de bash para los pasos que solo podes hacer "
               "vos: `gh auth`, paneles de terceros, migraciones de una vez"),
    ("git-guardrails-claude-code", "bloquea push/reset --hard/branch -D por "
                                   "hook. No choca con `commit`: es la capa de abajo"),
)

SKILLS_DESCARTADAS: tuple[tuple[str, str], ...] = (
    ("to-spec", "publica el spec al tracker; aca el spec es el ticket .md"),
    ("to-tickets", "publica los slices al tracker. Su idea de aristas de "
                   "bloqueo ya vive en `bloquea` del grafo"),
    ("triage", "maquina de estados propia sobre issues; se pisa con `fase`"),
    ("wayfinder", "el mapa ES un issue del tracker, con hijos y queries"),
    ("setup-matt-pocock-skills", "escribe docs/agents/ versionado en el repo"),
    ("setup-pre-commit", "Husky + lint-staged, y el Node de aca es v12.22. "
                         "`check --instalar-pre-push` ya ocupa ese lugar"),
)


def _repos_centrales(t: Ticket) -> list[str]:
    """Repos del ticket donde el doc de trabajo no se versiona.

    Ahi ninguna skill puede dejar `CONTEXT.md` ni `docs/adr/` en la raiz: van
    a .factoria/docs/<repo>/. Es la misma razon por la que existe `docs:
    central`, y la unica objecion real que tienen las skills que si se adoptan.
    """
    return [e.repo for e in t.repos if perfil(e.repo).get("docs") == "central"]


def _render_skills(items: tuple[tuple[str, str], ...]) -> None:
    for nombre, para_que in items:
        console.print(f"  [bold]{MP}:{nombre}[/]\n    [dim]{para_que}.[/]")


@cli.command("skills")
@click.argument("fase_arg", required=False, type=click.Choice(FASES))
def skills_cmd(fase_arg: str | None) -> None:
    """Que skill de mattpocock usar en cada fase, y cual queda afuera y por que."""
    for f in ([fase_arg] if fase_arg else list(SKILLS_POR_FASE)):
        console.print(f"\n[bold]fase {f}[/]")
        _render_skills(SKILLS_POR_FASE.get(f, ()))
    if fase_arg:
        return
    console.print("\n[bold]en cualquier fase[/]")
    _render_skills(SKILLS_TRANSVERSALES)
    console.print("\n[bold]afuera[/] [dim](guardan estado en un tracker por repo, "
                  "o escriben directorios versionados)[/]")
    for nombre, motivo in SKILLS_DESCARTADAS:
        console.print(f"  [dim]{nombre} — {motivo}.[/]")


@cli.command("fase")
@click.argument("slug")
@click.argument("nueva", type=click.Choice(FASES))
def fase_cmd(slug: str, nueva: str) -> None:
    """Cambia la fase de un ticket. NO corta la sesion: eso es `cortar`."""
    t = buscar_ticket(slug)
    previa = t.fase
    if previa == nueva:
        console.print(f"[dim]{t.slug} ya esta en fase {nueva}.[/]")
        return
    t.fase = nueva
    if nueva == "cerrado":
        t.abierto = False
    escribir_ticket(t)
    regenerar_indice()
    console.print(f"[bold]{t.slug}[/]  {previa} -> {nueva}")
    # Sin condicionar a `proyecto_item`: guardarlo para los ya espejados dejaba
    # un ticket nunca espejado invisible para siempre, y en silencio.
    espejar_si_se_puede(t)
    console.print(
        "[dim]La sesion sigue viva: cambiar de fase no la corta, porque reconstruir "
        "contexto cuesta mas que seguir.\n"
        f"Cortá cuando pese, no cuando cambie la fase: factoria sesiones --repo "
        f"{t.repos[0].repo if t.repos else ''}[/]"
    )
    # El handoff de fase y el trabajo de cada fase los hace una skill: es juicio
    # sobre contenido, no algo determinista. factoria solo dice cual y donde.
    destino = DATOS / "handoffs" / f"{t.slug}-{previa}.md"
    if not destino.is_file():
        console.print(
            f"\n[bold]Handoff de {previa}:[/] corré [bold]{MP}:handoff[/] "
            f"y guardá la salida en\n  {destino}\n"
            "[dim]Va afuera del ticket para no romperle la cota de 120 lineas.[/]"
        )
    if (sugeridas := SKILLS_POR_FASE.get(nueva)):
        console.print(f"\n[bold]Para la fase {nueva}:[/]")
        _render_skills(sugeridas)
    if nueva == "test" and t.repos:
        # Su eje Spec busca el issue de origen via tracker primero, pero acepta
        # un path como opcion 2. Pasandole el ticket no hace falta ningun tracker.
        # El punto fijo es el ancestro real, no el default del repo: 30 de 57
        # ramas de defeve salen de `desarrollo-ari` y diffear contra master les
        # atribuye los 241 archivos de la intermedia.
        e = t.repos[0]
        rp = Path(e.cwd)
        base = (base_efectiva(rp, e.rama, perfil(e.repo).get("base", "main"))
                if e.rama and rp.is_dir() else perfil(e.repo).get("base", "main"))
        console.print(f"  [dim]{MP}:code-review {base} {t.path}[/]")
    if nueva == "plan" and (cs := _repos_centrales(t)):
        console.print(
            f"\n[dim]En {', '.join(cs)} el doc de trabajo no se versiona: si una skill "
            f"quiere dejar CONTEXT.md o docs/adr/, mandalo a {DATOS / 'docs'} y no a la "
            "raiz del repo.[/]")


@cli.command()
@click.option("--todos", is_flag=True, help="Incluir los cerrados.")
@click.option("--json", "como_json", is_flag=True, help="Volcado crudo.")
def tickets(todos: bool, como_json: bool) -> None:
    """Lista los tickets con su fase, repos y sesiones asociadas."""
    ts = tickets_todos()
    if not todos:
        ts = [t for t in ts if t.abierto]
    if como_json:
        click.echo(json.dumps(
            [{"slug": t.slug, "fase": t.fase, "abierto": t.abierto,
              "spec_congelado": t.spec_congelado, "lineas": t.lineas,
              "repos": [dict(vars(e)) for e in t.repos]} for t in ts],
            indent=2, ensure_ascii=False))
        return
    if not ts:
        console.print()
        console.print("[dim]No hay tickets todavia. `factoria new <slug> --repo R`[/]")
        console.print()
        return
    console.print()
    console.print(f"[bold]Tickets[/]  |  {len(ts)}"
                  + ("" if todos else f" abiertos de {len(tickets_todos())}"))
    t_ = Table(title_justify="left", header_style="bold")
    t_.add_column("slug", overflow="fold")
    t_.add_column("fase")
    t_.add_column("repo")
    t_.add_column("cuenta")
    t_.add_column("rama", max_width=26, overflow="fold")
    t_.add_column("ses")
    t_.add_column("issue")
    t_.add_column("lin", justify="right")
    for t in ts:
        if not t.repos:
            t_.add_row(t.slug, t.fase, "[red]-[/]", "-", "-", "-",
                       f"#{t.issue}" if t.issue else "-", str(t.lineas))
        for i, e in enumerate(t.repos):
            t_.add_row(t.slug if i == 0 else "", t.fase if i == 0 else "",
                       e.repo, e.cuenta, e.rama or "-",
                       "si" if e.session_id else "[red]NO[/]",
                       (f"#{t.issue}" if t.issue else "[dim]-[/]") if i == 0 else "",
                       str(t.lineas) if i == 0 else "")
    console.print(t_)
    console.print()


# --------------------------------------------------------------------------
# Espejo a GitHub Issues + Project v2 (paso 5). Una sola via: los .md son
# canonicos. Si esto falla, el ticket sigue existiendo igual.
# --------------------------------------------------------------------------

GH_FALLBACK = Path(r"C:\Program Files\GitHub CLI\gh.exe")


def bin_gh() -> str | None:
    return shutil.which("gh") or (str(GH_FALLBACK) if GH_FALLBACK.is_file() else None)


def gh(*args: str) -> tuple[int, str, str]:
    b = bin_gh()
    if not b:
        return 127, "", "no encuentro `gh` en el PATH"
    r = subprocess.run([b, *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def config_github() -> dict:
    p = DATOS / "github.yml"
    if not p.is_file():
        return {}
    try:
        d = yaml.safe_load(p.read_text(encoding="utf-8", errors="replace"))
        return d if isinstance(d, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def _seccion(cuerpo: str, encabezado: str) -> str:
    """El texto de una seccion, sin los comentarios HTML de la plantilla."""
    lineas, out, dentro, com = cuerpo.splitlines(), [], False, False
    for l in lineas:
        if l.startswith("## "):
            dentro = l.strip() == encabezado
            continue
        if not dentro:
            continue
        if "<!--" in l:
            com = True
        if com:
            if "-->" in l:
                com = False
            continue
        out.append(l)
    return "\n".join(out).strip()


def cuerpo_issue(t: Ticket) -> str:
    """El issue es un espejo legible, no la fuente de verdad. Eso se dice arriba."""
    partes = [
        "> Espejo de un ticket de factoria. **La fuente de verdad es el archivo**, "
        f"no este issue: `.factoria/tickets/{t.slug}.md`.",
        "",
        f"**fase:** `{t.fase}`",
        "",
        "**repos**",
        "",
    ]
    for e in t.repos:
        partes.append(f"- `{e.repo}` ({e.cuenta}) — rama `{e.rama or '?'}`")
    for enc in ("## Pedido crudo", "## Criterios de aceptación",
                "## Fuera de alcance", "## Entregables de código",
                "## Supuestos abiertos"):
        cuerpo = _seccion(t.cuerpo, enc)
        partes += ["", enc.replace("## ", "### "), "", cuerpo or "_(vacío)_"]
    return "\n".join(partes)


def _estado_spec(t: Ticket) -> str:
    """Que dice el tablero sobre el spec: es el gate que existe de verdad."""
    if not t.spec_congelado:
        return "sin aprobar"
    # Un ticket cerrado daria deriva SIEMPRE: `close` archiva el cuerpo en
    # historial/ y deja un puntero, asi que la huella cambia por diseño. La
    # deriva solo dice algo mientras el trabajo esta en vuelo; despues es ruido
    # que acusa al propio `close`.
    if not t.abierto:
        return "aprobado"
    return "aprobado" if huella_spec(t) == t.spec_congelado else "deriva"


def _set_single_select(item: str, proyecto: str, campo: dict, valor: str,
                       etiqueta: str) -> str | None:
    """Mueve un single-select del Project. None si el campo no esta configurado.

    Cada campo se reporta por separado en vez de abortar: el espejo es opcional,
    y que falle mover `spec` no puede tapar que `fase` si se movio.
    """
    if not campo or not (opcion := (campo.get("opciones") or {}).get(valor)):
        return None
    rc, _, err = gh("project", "item-edit", "--id", item, "--project-id", proyecto,
                    "--field-id", campo["id"], "--single-select-option-id", str(opcion))
    return (f"{etiqueta} = {valor}" if rc == 0
            else f"[yellow]no pude mover {etiqueta}: {err[:120]}[/]")


def _labels_del_ticket(t: Ticket, cfg: dict) -> list[str]:
    """Los repos van como label y no como campo del Project: un ticket cruza
    repos por naturaleza y un single-select no puede tener N valores."""
    lb = cfg.get("labels") or {}
    pr, pc = lb.get("prefijo_repo", "repo:"), lb.get("prefijo_cuenta", "cuenta:")
    nombres = {f"{pr}{e.repo}" for e in t.repos}
    nombres |= {f"{pc}{e.cuenta}" for e in t.repos if e.cuenta}
    return sorted(nombres)


def espejar_si_se_puede(t: Ticket) -> None:
    """Espeja y persiste los ids, sin abortar el comando que llamo.

    El espejo es opcional por diseño: los .md son canonicos, asi que sin red o
    sin `gh` el ticket ya quedo bien y esto solo se reporta. Lo que NO puede
    pasar es perder un id: `espejar` puede crear el issue y despues fallar al
    agregarlo al tablero, y si ese numero no se guarda el proximo intento crea
    un issue duplicado. Por eso se escribe el ticket en las dos ramas.
    """
    try:
        hechos = espejar(t, config_github())
    except click.ClickException as exc:
        escribir_ticket(t)
        console.print(f"  [yellow]espejo pendiente:[/] {exc.message}")
        return
    escribir_ticket(t)
    for h in hechos:
        console.print(f"  {h}")


def espejar(t: Ticket, cfg: dict) -> list[str]:
    """Sincroniza ticket -> issue -> item del Project. Devuelve que hizo."""
    faltan = [k for k in ("repo", "proyecto", "campo_fase") if k not in cfg]
    if faltan:
        raise click.ClickException(
            f"falta {', '.join(faltan)} en {DATOS / 'github.yml'}. "
            "Corré `factoria espejo --probar` para ver el diagnostico."
        )
    repo = cfg["repo"]
    pr, campo = cfg["proyecto"], cfg["campo_fase"]
    hechos = []

    etiquetas = _labels_del_ticket(t, cfg)
    arg_labels = ["--label", ",".join(etiquetas)] if etiquetas else []
    if not t.issue:
        rc, out, err = gh("issue", "create", "--repo", repo,
                          "--title", t.slug, "--body", cuerpo_issue(t), *arg_labels)
        if rc != 0:
            raise click.ClickException(f"gh issue create falló:\n{err[:400]}")
        t.issue_url = out.splitlines()[-1].strip()
        t.issue = int(t.issue_url.rstrip("/").rsplit("/", 1)[-1] or 0)
        hechos.append(f"issue #{t.issue} creado"
                      + (f" ({', '.join(etiquetas)})" if etiquetas else ""))
    else:
        # `--add-label` y no `--label`: sumar, no reemplazar, para no borrar una
        # label puesta a mano en el issue.
        add = ["--add-label", ",".join(etiquetas)] if etiquetas else []
        rc, _, err = gh("issue", "edit", str(t.issue), "--repo", repo,
                        "--body", cuerpo_issue(t), *add)
        hechos.append(f"issue #{t.issue} actualizado" if rc == 0
                      else f"[yellow]no pude actualizar el issue: {err[:120]}[/]")

    if not t.proyecto_item:
        rc, out, err = gh("project", "item-add", str(pr["numero"]),
                          "--owner", pr["owner"], "--url", t.issue_url,
                          "--format", "json")
        if rc != 0:
            raise click.ClickException(f"gh project item-add falló:\n{err[:400]}")
        try:
            t.proyecto_item = json.loads(out)["id"]
        except (ValueError, KeyError):
            raise click.ClickException(f"no pude leer el id del item:\n{out[:200]}")
        hechos.append("agregado al tablero")

    for campo_cfg, valor, etiqueta in (
        (campo, t.fase, "fase"),
        (cfg.get("campo_spec"), _estado_spec(t), "spec"),
        (cfg.get("campo_cuenta"), t.repos[0].cuenta if t.repos else "", "cuenta"),
    ):
        if (linea := _set_single_select(t.proyecto_item, pr["id"], campo_cfg,
                                        valor, etiqueta)):
            hechos.append(linea)

    if (cr := cfg.get("campo_rama")) and (ramas := [e.rama for e in t.repos if e.rama]):
        rc, _, err = gh("project", "item-edit", "--id", t.proyecto_item,
                        "--project-id", pr["id"], "--field-id", cr["id"],
                        "--text", ", ".join(ramas))
        hechos.append(f"rama = {', '.join(ramas)}" if rc == 0
                      else f"[yellow]no pude escribir la rama: {err[:120]}[/]")

    if not t.abierto:
        rc, _, _ = gh("issue", "close", str(t.issue), "--repo", repo)
        if rc == 0:
            hechos.append("issue cerrado")
    return hechos


@cli.command("espejo")
@click.argument("slug", required=False)
@click.option("--probar", is_flag=True, help="Diagnostico: no escribe nada.")
@click.option("--todos", is_flag=True, help="Espejar todos los tickets abiertos.")
def espejo_cmd(slug: str | None, probar: bool, todos: bool) -> None:
    """Espeja un ticket como issue de GitHub y tarjeta del Project."""
    cfg = config_github()
    if probar:
        console.print()
        console.print(f"[bold]espejo --probar[/]  |  config {DATOS / 'github.yml'}")
        console.print(f"  gh          {bin_gh() or '[red]NO ENCONTRADO[/]'}")
        rc, out, _ = gh("auth", "status")
        scopes = next((l.strip() for l in out.splitlines() if "scopes" in l.lower()), "?")
        console.print(f"  auth        {'ok' if rc == 0 else '[red]sin login[/]'}  {scopes}")
        console.print(f"  repo        {cfg.get('repo', '[red]falta[/]')}")
        pr = cfg.get("proyecto") or {}
        console.print(f"  proyecto    #{pr.get('numero', '?')} {pr.get('url', '')}")
        campo = cfg.get("campo_fase") or {}
        console.print(f"  campo fase  {campo.get('id', '[red]falta[/]')}  "
                      f"opciones: {', '.join(campo.get('opciones') or {}) or '[red]ninguna[/]'}")
        if pr.get("numero"):
            rc, out, err = gh("project", "field-list", str(pr["numero"]),
                              "--owner", pr.get("owner", ""), "--format", "json")
            console.print(f"  lectura     {'ok' if rc == 0 else '[red]' + err[:90] + '[/]'}")
        console.print()
        return

    objetivo = [t for t in tickets_todos() if t.abierto] if todos else \
               ([buscar_ticket(slug)] if slug else [])
    if not objetivo:
        raise click.ClickException("pasá un slug, o --todos, o --probar.")
    for t in objetivo:
        hechos = espejar(t, cfg)
        escribir_ticket(t)
        console.print(f"[bold]{t.slug}[/]  {t.issue_url}")
        for h in hechos:
            console.print(f"  {h}")
    regenerar_indice()


@cli.command("abrir")
@click.argument("slug")
def abrir_cmd(slug: str) -> None:
    """Abre el issue del ticket en el navegador (o imprime la URL)."""
    t = buscar_ticket(slug)
    if not t.issue_url:
        raise click.ClickException(
            f"'{t.slug}' no tiene issue todavia. `factoria espejo {t.slug}` lo crea."
        )
    console.print(t.issue_url)
    try:
        os.startfile(t.issue_url)  # type: ignore[attr-defined]
    except (AttributeError, OSError) as exc:
        console.print(f"[dim]no pude abrir el navegador ({exc}); la URL esta arriba.[/]")


def _entrada_unica(t: Ticket, repo: str | None, exacto: bool = False) -> RepoTicket:
    """La entrada del ticket para un repo.

    `exacto` cuando el nombre no lo tipeo una persona: por substring, el repo
    `factoria` matchea la entrada `.factoria` y `check` termina escribiendo la
    evidencia de un repo en el doc del otro. Para `--repo` el substring es
    comodidad; para un `rp.name` es un error silencioso.
    """
    def coincide(e: RepoTicket) -> bool:
        if not repo:
            return True
        return e.repo.lower() == repo.lower() if exacto else repo.lower() in e.repo.lower()
    entradas = [e for e in t.repos if coincide(e)]
    if not entradas:
        raise click.ClickException(
            f"'{t.slug}' no tiene entrada para --repo {repo}. Repos: "
            + ", ".join(e.repo for e in t.repos)
        )
    if len(entradas) > 1:
        raise click.ClickException(
            f"'{t.slug}' tiene {len(entradas)} repos, elegí uno con --repo: "
            + ", ".join(e.repo for e in entradas)
        )
    return entradas[0]


def _renombrar_rama(rp: Path, vieja: str, nueva: str) -> None:
    """Renombra la rama en git, o la crea si la vieja no existe."""
    if existe_rama(rp, nueva):
        raise click.ClickException(f"{rp.name} ya tiene una rama '{nueva}'.")
    if existe_rama(rp, vieja):
        # `git branch -m` funciona incluso con la rama activa. Lo que NO mueve es
        # el remoto: si ya se pusheo, alla queda el nombre viejo.
        up = git(rp, "for-each-ref", "--format=%(upstream:short)", f"refs/heads/{vieja}")
        _git_o_falla(rp, "branch", "-m", vieja, nueva)
        if up:
            console.print(
                f"[yellow]Ojo:[/] '{vieja}' tenia upstream [bold]{up}[/]. El remoto "
                f"sigue con el nombre viejo; el proximo push necesita "
                f"`git push -u origin {nueva}` y conviene borrar la vieja alla."
            )
    else:
        console.print(f"[dim]{rp.name} no tiene la rama '{vieja}'; solo se "
                      "actualiza el registro del ticket.[/]")


@cli.command("cotas")
@click.option("--contratos/--no-contratos", default=True)
@click.option("--tickets/--no-tickets", default=True)
@click.option("--docs/--no-docs", default=True)
def cotas_cmd(contratos: bool, tickets: bool, docs: bool) -> None:
    """Mide las cotas del estandar y sale con 1 si alguna se paso.

    La regla 2 del estandar pide guardian EXTERNO, y hasta ahora el unico era
    `board`, que informa y sale con 0. Sin codigo de salida no se puede colgar
    de un `verify:` ni de un pre-push, y toda cota que se auto-vigilaba en un
    .md decayo: el §7 de Cotizaciones tiene su cota escrita adentro y ocupa
    1395 lineas. Esto es rapido a proposito -- solo cuenta lineas, no toca git.
    """
    grupos: list[tuple[str, int, list[tuple[str, int]]]] = []
    if contratos:
        grupos.append(("contratos", COTA_CONTRATO, cotas_contratos()))
    if tickets:
        grupos.append(("tickets", COTA_TICKET,
                       sorted(((t.slug, t.lineas) for t in tickets_todos()),
                              key=lambda x: -x[1])))
    if docs:
        raiz = DATOS / "docs"
        medidos = [
            (str(p.relative_to(raiz)),
             len(p.read_text(encoding="utf-8", errors="replace").splitlines()))
            for p in sorted(raiz.rglob("*.md"))
        ] if raiz.is_dir() else []
        grupos.append(("docs de trabajo", COTA_DOC_TRABAJO,
                       sorted(medidos, key=lambda x: -x[1])))

    excedidos = 0
    for nombre, cota, medidos in grupos:
        malos = [(n, l) for n, l in medidos if l > cota]
        excedidos += len(malos)
        estado = f"[red]{len(malos)} pasan[/]" if malos else "[green]ok[/]"
        console.print(f"[bold]{nombre}[/] cota {cota}: {len(medidos)} medidos, {estado}")
        for n, l in malos:
            console.print(f"  [red]{l:>5}[/]  {n}  [dim]({l / cota:.1f}x)[/]")
    if excedidos:
        raise SystemExit(1)


def _elegir_sesion(t: Ticket, e: RepoTicket, cta: str,
                   restringida: bool) -> tuple[str, str, str]:
    """Que sesion adoptar sin uuid. Devuelve (uuid, cuenta, motivo).

    La cuenta sale de DONDE ESTA la sesion elegida, no del ticket: buscar en
    las dos y despues dejar la cuenta vieja escrita deja el ticket apuntando
    a un uuid que `resume` no encuentra, que es el bug que este comando
    arregla.

    El orden de la evidencia es la leccion de haber adoptado la sesion
    equivocada: el mtime no es evidencia de nada. Que el slug aparezca en el
    transcript si lo es, y es el unico criterio que decide solo. Sin esa
    evidencia el comando se niega y lista, porque la mas reciente del mismo cwd
    puede ser cualquier conversacion que haya pasado por ese repo.

    Sin `--cuenta` mira las dos: cuando `new` reserva el uuid con la cuenta del
    perfil y el trabajo pasa por la otra, la cuenta del ticket apunta al lado
    equivocado, y buscar solo ahi garantiza elegir mal.
    """
    cuentas = (cta,) if restringida else tuple(CUENTAS)
    objetivo = str(Path(e.cwd)).lower() if e.cwd else ""

    def mismo_cwd(s: Sesion) -> bool:
        return bool(objetivo and s.cwd) and str(Path(s.cwd)).lower() == objetivo

    universo = [s for s in inventario_sesiones() if s.cuenta in cuentas]
    # La sesion que corre el comando nombra el slug porque se acaba de tipear
    # el comando: no puede ser evidencia de si misma. Para esa esta `--aqui`.
    yo = (sesion_actual() or ("", ""))[0]
    nombran = sesiones_que_nombran(t.slug)
    marcadas = [s for s in universo if s.session_id in nombran and s.session_id != yo]
    if marcadas:
        elegida = max(marcadas, key=lambda s: (mismo_cwd(s), s.mtime))
        extra = "" if len(marcadas) == 1 else f", la mas reciente de {len(marcadas)}"
        return (elegida.session_id, elegida.cuenta,
                f"el slug aparece en su transcript{extra}")
    cerca = sorted((s for s in universo if mismo_cwd(s)), key=lambda s: -s.mtime)
    listado = "".join(
        f"\n    {s.session_id}  {s.cuenta:<8} {s.mb:5.1f} MB  {s.dias}d  "
        f"{s.titulo[:44] or 'sin titulo'}" for s in cerca[:6])
    donde = (f"  Sesiones con cwd {e.cwd}:{listado}" if cerca
             else f"  Ninguna sesion tiene cwd {e.cwd}.")
    raise click.ClickException(
        f"ninguna sesion nombra '{t.slug}' en su transcript, asi que no hay con "
        "que saber cual hizo el trabajo. Adoptar la mas reciente del mismo cwd "
        "es lo que hacia antes, y elige conversaciones ajenas.\n"
        + donde
        + "\n  Pasa el uuid, o --aqui si el trabajo sigue en esta sesion.")


@cli.command("adoptar")
@click.argument("slug")
@click.argument("session_id", required=False)
@click.option("--repo", help="Cuando el ticket tiene varios repos.")
@click.option("--cuenta", type=click.Choice(list(CUENTAS)),
              help="Restringir la busqueda a esta cuenta y repuntar el ticket a ella.")
@click.option("--aqui", is_flag=True,
              help="Adoptar la sesion desde la que se esta corriendo esto.")
@click.option("--forzar", is_flag=True, help="Reemplazar un id que si existe en disco.")
def adoptar_cmd(slug: str, session_id: str | None, repo: str | None,
                cuenta: str | None, aqui: bool, forzar: bool) -> None:
    """Registra en el ticket la sesion que hizo el trabajo de verdad.

    `new` reserva el uuid ANTES de que la sesion exista. Si el trabajo termino
    pasando por otra -- la que ya estaba abierta, un fork, una arrancada a mano
    -- el ticket apunta a un id que no esta en disco y `resume` abre una sesion
    nueva en vez de continuar: el modo de fallo exacto que factoria existe para
    evitar.

    Sin uuid la busca por evidencia: la sesion cuyo transcript nombra el slug.
    Si ninguna lo nombra se niega y lista, en vez de adoptar la mas reciente del
    mismo cwd, que puede ser -- y fue -- una conversacion ajena.

    `--aqui` adopta la sesion desde la que se corre el comando, sin adivinar
    nada. Es el caso "el ticket nacio en la otra cuenta y de aca en adelante se
    trabaja en esta": los transcripts de `dfv` y `personal` son directorios
    disjuntos (0 uuid en comun sobre 337), asi que la conversacion no se muda.
    Lo que se muda es a que sesion y a que cuenta apunta el ticket; el trabajo
    entra en la sesion nueva con `factoria contexto <slug>`.
    """
    t = buscar_ticket(slug)
    e = _entrada_unica(t, repo)
    cta = cuenta or e.cuenta
    if aqui:
        if session_id:
            raise click.ClickException(
                "--aqui y un uuid explicito son la misma decision dos veces: "
                "pasa uno solo")
        actual = sesion_actual()
        if not actual:
            raise click.ClickException(
                "--aqui solo corre DENTRO de una sesion de Claude Code: no hay "
                "CLAUDE_CODE_SESSION_ID en el entorno")
        session_id, cta_aqui = actual
        if cuenta and cuenta != cta_aqui:
            raise click.ClickException(
                f"esta sesion es de la cuenta {cta_aqui}, no {cuenta}: --aqui ya "
                "define la cuenta, saca --cuenta")
        cta = cta_aqui
    if session_id:
        nuevo = session_id
        if not jsonl_de(nuevo, cta):
            otra = next((c for c in CUENTAS if c != cta and jsonl_de(nuevo, c)), None)
            raise click.ClickException(
                f"no hay .jsonl de {nuevo} en la cuenta {cta}. "
                + (f"Si esta en '{otra}': agrega --cuenta {otra}"
                   if otra else "Adoptar un id inexistente reproduce el problema "
                                "que este comando arregla"))
    else:
        nuevo, cta, motivo = _elegir_sesion(t, e, cta, restringida=bool(cuenta))
        console.print(f"  [dim]{motivo}[/]")
    previa, cuenta_previa = e.session_id, e.cuenta
    if nuevo == previa and cta == cuenta_previa:
        console.print(f"[dim]{t.slug}/{e.repo} ya apunta a {nuevo}.[/]")
        return
    # Aviso y no error: una sesion sirviendo a dos tickets es un desorden real
    # (`resume` de los dos cae en la misma conversacion, `cortar` uno corta el
    # otro) pero pasa legitimamente cuando un ticket nace desde la sesion de
    # otro. Con error obligaria a --forzar de rutina, y --forzar tambien apaga
    # el guard de abajo, que importa mas: bypassear uno no puede bypassear los dos.
    ajenos = sorted({x.slug for x in tickets_todos() if x.slug != t.slug
                     for r in x.repos if r.session_id == nuevo})
    if ajenos:
        console.print(f"  [yellow]ojo:[/] {nuevo} ya es la sesion de "
                      f"{', '.join(ajenos)}")
    # El guard mira la cuenta VIEJA, que es donde vive la sesion que se estaria
    # dejando sin ticket. Mirar la nueva lo desactiva justo cuando se cambia de
    # cuenta, que es cuando mas hace falta: ahi el .jsonl anterior nunca esta.
    if previa and jsonl_de(previa, cuenta_previa) and not forzar:
        raise click.ClickException(
            f"{previa} existe en disco (cuenta {cuenta_previa}): reemplazarla la "
            "deja sin ticket que la encuentre. Repeti con --forzar si es lo que queres")
    if cta != cuenta_previa:
        console.print(f"  [dim]cuenta {cuenta_previa} -> {cta}[/]")
        e.cuenta = cta
    e.session_id = nuevo
    escribir_ticket(t)
    # Cuando solo cambio la cuenta, el "A -> A" es ruido: el cambio ya
    # se imprimio arriba.
    console.print(f"[bold]{t.slug}/{e.repo}[/]  " + (
        f"sesion {previa or '(ninguna)'} -> {nuevo}" if nuevo != previa
        else "misma sesion, ahora en la cuenta que la tiene"))
    if (j := jsonl_de(nuevo, cta)):
        tam = j.stat().st_size
        m = _meta_sesion(j, tam)
        mb = tam / 1_048_576
        console.print(f"  [dim]{mb:.1f} MB · rama '{m['rama'] or '?'}' · "
                      f"{m['titulo'] or 'sin titulo'}[/]")
        if m["cwd"] and e.cwd and str(Path(m["cwd"])).lower() != str(Path(e.cwd)).lower():
            # `resume` hace `cd <cwd>` antes de `-r`: apuntar a un directorio
            # donde la sesion nunca corrio la deja sin sus archivos abiertos.
            # Que el cwd no sea el del repo del ticket es legitimo -- una sesion
            # puede editar otro repo -- pero tiene que quedar registrado.
            console.print(f"  [dim]cwd {e.cwd} -> {m['cwd']} (donde corre la sesion)[/]")
            e.cwd = m["cwd"]
            escribir_ticket(t)
        if m["rama"] and e.rama and m["rama"] != e.rama:
            # No se corrige en silencio: cual de las dos es la buena es una
            # decision, y `rama` es el comando que la aplica en git tambien.
            console.print(f"  [yellow]la sesion esta en '{m['rama']}' y el ticket "
                          f"dice '{e.rama}'[/]: factoria rama {t.slug} <la correcta>")
        if mb > UMBRAL_SESION_MB:
            console.print(f"  [dim]pasa {UMBRAL_SESION_MB} MB: factoria cortar "
                          f"{t.slug} cuando quieras arrancar liviano.[/]")

    if aqui:
        console.print(f"  [dim]el trabajo del ticket todavia no esta en el "
                      f"contexto de esta sesion: factoria contexto {t.slug}[/]")


@cli.command("rama")
@click.argument("slug")
@click.argument("nueva")
@click.option("--repo", help="Cuando el ticket tiene varios repos.")
@click.option("--solo-registro", is_flag=True,
              help="No tocar git: solo cambiar a que rama apunta el ticket.")
def rama_cmd(slug: str, nueva: str, repo: str | None, solo_registro: bool) -> None:
    """Renombra la rama de un ticket, en git y en el registro.

    `--solo-registro` para cuando el ticket quedo apuntando a una rama AJENA:
    ahi renombrar en git le rompe la rama a otro trabajo. Es el caso de los
    tickets creados antes de que `new` dejara de adoptar la rama actual.
    """
    t = buscar_ticket(slug)
    e = _entrada_unica(t, repo)
    if e.rama == nueva:
        console.print(f"[dim]{t.slug}/{e.repo} ya esta en '{nueva}'.[/]")
        return
    rp = ruta_repo(e.repo)
    if not rp:
        raise click.ClickException(f"no encuentro el repo '{e.repo}' en disco.")
    vieja = e.rama
    if solo_registro:
        console.print(f"[dim]git no se toca: '{vieja}' sigue como esta en {e.repo}.[/]")
    else:
        _renombrar_rama(rp, vieja, nueva)
    e.rama = nueva
    escribir_ticket(t)
    regenerar_indice()
    console.print(f"[bold]{t.slug}[/] / {e.repo}:  {vieja or '(sin rama)'} -> {nueva}")


@cli.command("renombrar")
@click.argument("slug")
@click.argument("nuevo")
@click.option("--con-rama/--sin-rama", default=True, show_default=True,
              help="Renombrar tambien las ramas que contengan el slug viejo.")
def renombrar_cmd(slug: str, nuevo: str, con_rama: bool) -> None:
    """Renombra un ticket: el archivo, sus docs, y las ramas derivadas del slug."""
    t = buscar_ticket(slug)
    viejo = t.slug
    nuevo = normalizar_slug(nuevo)
    if not nuevo:
        raise click.ClickException("el nombre nuevo no deja nada usable como slug.")
    if nuevo == viejo:
        console.print(f"[dim]ya se llama '{nuevo}'.[/]")
        return
    destino = dir_tickets() / f"{nuevo}.md"
    if destino.exists():
        raise click.ClickException(f"ya existe un ticket '{nuevo}'.")

    hechos: list[str] = []
    for e in t.repos:
        # Ramas primero: si git falla, no quiero archivos ya movidos.
        if con_rama and e.rama and viejo in e.rama:
            rp = ruta_repo(e.repo)
            if rp:
                rama_nueva = e.rama.replace(viejo, nuevo)
                _renombrar_rama(rp, e.rama, rama_nueva)
                hechos.append(f"rama {e.rama} -> {rama_nueva}")
                e.rama = rama_nueva
    for e in t.repos:
        if not e.doc:
            continue
        viejo_doc = Path(e.doc)
        nuevo_doc = ruta_doc(e.repo, nuevo)
        if viejo_doc.is_file() and viejo_doc != nuevo_doc:
            nuevo_doc.parent.mkdir(parents=True, exist_ok=True)
            txt = viejo_doc.read_text(encoding="utf-8", errors="replace")
            nuevo_doc.write_text(
                re.sub(rf"^ticket:\s*{re.escape(viejo)}\s*$", f"ticket: {nuevo}",
                       txt, count=1, flags=re.M),
                encoding="utf-8", newline="\n",
            )
            viejo_doc.unlink()
            hechos.append(f"doc {viejo_doc.name} -> {nuevo_doc.name}")
        e.doc = str(nuevo_doc)

    viejo_path = t.path
    t.slug = nuevo
    t.path = destino
    # El titulo `# <slug>` del cuerpo tambien, si estaba.
    t.cuerpo = re.sub(rf"^#\s+{re.escape(viejo)}\s*$", f"# {nuevo}",
                      t.cuerpo, count=1, flags=re.M)
    escribir_ticket(t)
    if viejo_path and viejo_path.is_file() and viejo_path != destino:
        viejo_path.unlink()
        hechos.append(f"ticket {viejo_path.name} -> {destino.name}")
    regenerar_indice()

    console.print(f"[bold]{viejo}[/] -> [bold]{nuevo}[/]")
    for h in hechos:
        console.print(f"  {h}")
    console.print("[dim]La sesion no cambia: el session_id sigue siendo el mismo, "
                  f"asi que `factoria resume {nuevo}` cae en la misma conversacion.[/]")


# --------------------------------------------------------------------------
# Grafo: recuperacion por adyacencia (paso 6). NO es una base de datos: es un
# derivado regenerable de metadata que ya escribis. El anti-patron de "cero RAG
# de codigo" sigue en pie porque no hay embeddings de nada.
# --------------------------------------------------------------------------

RE_REF_CONTRATO = re.compile(r"\.contracts[\\/]([a-z0-9][a-z0-9._-]*?)\.md", re.I)
# Los contratos ya traen items cross-repo reales: `- [ ] **(defeve → Cotizaciones)**`
RE_BLOQUEO = re.compile(r"\((\w[\w.-]*)\s*(?:->|→|=>)\s*(\w[\w.-]*)\)")


def path_grafo() -> Path:
    return DATOS / "grafo.json"


def _tokens(s: str) -> set[str]:
    return set(re.split(r"[-_/.]+", s.lower())) - {""}


def base_efectiva(rp: Path, rama: str, base_repo: str) -> str:
    """De que rama salio esta, en serio: el ancestro comun mas reciente."""
    candidatas = [base_repo, *BASES_CANDIDATAS]
    mejor, mejor_ts = base_repo, -1
    for c in candidatas:
        if not c or c == rama or not existe_rama(rp, c):
            continue
        mb = git(rp, "merge-base", c, rama)
        if not mb:
            continue
        ts = git(rp, "show", "-s", "--format=%ct", mb)
        if ts.isdigit() and int(ts) > mejor_ts:
            mejor, mejor_ts = c, int(ts)
    return mejor


def archivos_de_rama(rp: Path, rama: str, base: str, cache: dict) -> list[str]:
    """Archivos que la rama cambio contra su base real, con cache por sha del tip.

    Un diff cuesta ~0.55 s y hay ~60 ramas con doc asociado: sin cache el grafo
    tardaria medio minuto en cada corrida.
    """
    if not rama or not base or rama == base:
        return []
    clave = f"{rp.name}/{rama}"
    sha = git(rp, "rev-parse", "--verify", "--quiet", f"refs/heads/{rama}")
    if not sha:
        return []
    guardado = cache.get(clave)
    if guardado and guardado.get("sha") == sha:
        return guardado["archivos"]
    real = base_efectiva(rp, rama, base)
    salida = git(rp, "diff", "--name-only", f"{real}...{rama}")
    archivos = [l for l in salida.splitlines() if l.strip()]
    cache[clave] = {"sha": sha, "base": real, "archivos": archivos}
    return archivos


def construir_grafo(reconstruir: bool = False) -> dict:
    viejo = {} if reconstruir else (cargar_json(path_grafo()) or {})
    cache = viejo.get("cache_ramas", {}) if isinstance(viejo, dict) else {}
    nodos: dict[str, dict] = {}
    aristas: list[dict] = []

    def nodo(nid: str, **datos) -> str:
        nodos.setdefault(nid, {}).update({k: v for k, v in datos.items() if v not in (None, "")})
        return nid

    def arista(o: str, d: str, t: str) -> None:
        aristas.append({"o": o, "d": d, "t": t})

    repos = {r.name: r for r in descubrir_repos()}
    bases = {n: (perfil(n).get("base") or base_de(r) or "") for n, r in repos.items()}

    # --- contratos: nodos + los bloqueos cross-repo que ya tienen escritos
    for nombre, largo in cotas_contratos():
        tema = nombre[:-3]
        cid = nodo(f"contrato:{tema}", tipo="contrato", lineas=largo)
        try:
            txt = (CONTRATOS / nombre).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for linea in txt.splitlines():
            if not linea.lstrip().startswith("- [ ]"):
                continue
            for a, b in RE_BLOQUEO.findall(linea):
                if a in repos and b in repos:
                    arista(nodo(f"repo:{a}", tipo="repo"), nodo(f"repo:{b}", tipo="repo"),
                           "bloquea")
                    arista(cid, nodo(f"repo:{b}", tipo="repo"), "bloquea")

    def enlazar_trabajo(nid: str, repo_nombre: str, rama: str, texto: str) -> None:
        """Aristas comunes a un ticket y a un doc de trabajo."""
        rp = repos.get(repo_nombre)
        arista(nid, nodo(f"repo:{repo_nombre}", tipo="repo"), "toca")
        for tema in {t.lower() for t in RE_REF_CONTRATO.findall(texto or "")}:
            arista(nid, nodo(f"contrato:{tema}", tipo="contrato"), "refiere")
        if rp and rama:
            for f in archivos_de_rama(rp, rama, bases.get(repo_nombre, ""), cache):
                arista(nid, nodo(f"archivo:{repo_nombre}/{f}", tipo="archivo",
                                 repo=repo_nombre, path=f), "toca")

    # --- tickets
    for t in tickets_todos():
        tid = nodo(f"ticket:{t.slug}", tipo="ticket", fase=t.fase, abierto=t.abierto,
                   issue=t.issue or None, path=str(t.path) if t.path else "")
        for e in t.repos:
            texto = t.cuerpo
            if e.doc and Path(e.doc).is_file():
                texto += Path(e.doc).read_text(encoding="utf-8", errors="replace")
            enlazar_trabajo(tid, e.repo, e.rama, texto)

    # --- docs de trabajo preexistentes: son 125 contra 1 ticket, asi que sin
    # ellos el grafo no responderia nada util todavia. La rama se deduce por
    # tokens del slug, que es como las nombras.
    for nombre, rp in repos.items():
        ramas = git(rp, "for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines()
        por_tokens = {r: _tokens(r) for r in ramas if r.strip()}
        for d in docs_de(rp):
            did = nodo(f"doc:{nombre}/{d.slug}", tipo="doc", repo=nombre, estado=d.estado,
                       pendientes=d.pendientes, lineas=d.lineas, path=str(d.path))
            ds = _tokens(d.slug)
            rama = next((r for r, tk in por_tokens.items() if ds <= tk or tk <= ds), "")
            if rama:
                arista(did, nodo(f"rama:{nombre}/{rama}", tipo="rama"), "deriva_de")
            try:
                texto = d.path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                texto = ""
            enlazar_trabajo(did, nombre, rama, texto)

    g = {"generado": time.strftime("%Y-%m-%dT%H:%M:%S"), "cache_ramas": cache,
         "nodos": nodos, "aristas": aristas}
    path_grafo().parent.mkdir(parents=True, exist_ok=True)
    path_grafo().write_text(json.dumps(g, indent=1, ensure_ascii=False),
                            encoding="utf-8", newline="\n")
    return g


def cargar_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None


def grafo_vigente(reconstruir: bool = False) -> dict:
    if reconstruir:
        return construir_grafo(True)
    g = cargar_json(path_grafo())
    if isinstance(g, dict) and g.get("nodos"):
        return g
    return construir_grafo()


def adyacentes(g: dict, nid: str) -> list[tuple[str, str, str]]:
    """(vecino, tipo_de_arista, direccion) para un nodo."""
    out = []
    for a in g.get("aristas", []):
        if a["o"] == nid:
            out.append((a["d"], a["t"], "->"))
        elif a["d"] == nid:
            out.append((a["o"], a["t"], "<-"))
    vistos, unicos = set(), []
    for v in out:
        if v not in vistos:
            vistos.add(v)
            unicos.append(v)
    return unicos


def resolver_nodo(g: dict, consulta: str) -> list[str]:
    q = consulta.lower()
    exactos = [n for n in g.get("nodos", {})
               if n.split(":", 1)[-1].lower() == q or n.lower() == q]
    if exactos:
        return exactos
    return [n for n in g.get("nodos", {}) if q in n.lower()]


@cli.command("grafo")
@click.option("--que-toca", "que_toca", help="Tickets y docs que tocaron ese archivo.")
@click.option("--vecinos", help="Nodos adyacentes a un slug, contrato o archivo.")
@click.option("--bloqueos", is_flag=True, help="La cadena de dependencias cross-repo.")
@click.option("--reconstruir", is_flag=True, help="Rehacer el grafo desde cero.")
def grafo_cmd(que_toca: str | None, vecinos: str | None, bloqueos: bool,
              reconstruir: bool) -> None:
    """Consulta el grafo de adyacencia. Reemplaza barrer 125 archivos."""
    g = grafo_vigente(reconstruir)
    nodos = g.get("nodos", {})
    console.print()
    console.print(f"[dim]grafo: {len(nodos)} nodos, {len(g.get('aristas', []))} aristas, "
                  f"generado {g.get('generado', '?')}[/]")

    if que_toca:
        q = que_toca.lower().replace("\\", "/")
        arch = [n for n, d in nodos.items() if d.get("tipo") == "archivo" and q in n.lower()]
        if not arch:
            console.print(f"[yellow]ningun archivo del grafo matchea '{que_toca}'.[/]")
            console.print("[dim]El grafo solo conoce archivos de ramas sin mergear.[/]")
            return
        trabajos: dict[str, set[str]] = {}
        for a in arch:
            for v, t, _ in adyacentes(g, a):
                if v.startswith(("ticket:", "doc:")):
                    trabajos.setdefault(v, set()).add(a.split("/", 1)[-1])
        console.print(f"[bold]{len(arch)} archivos, {len(trabajos)} trabajos que los tocaron[/]")
        for v, fs in sorted(trabajos.items(), key=lambda kv: -len(kv[1])):
            console.print(f"  {v}  [dim]({len(fs)} archivos)[/]")
        console.print()
        return

    if vecinos:
        cands = resolver_nodo(g, vecinos)
        if not cands:
            raise click.ClickException(f"ningun nodo matchea '{vecinos}'.")
        for nid in cands[:3]:
            console.print(f"[bold]{nid}[/]  {nodos.get(nid, {})}")
            porgrupo: dict[str, list[str]] = {}
            for v, t, dirn in adyacentes(g, nid):
                porgrupo.setdefault(f"{t} {dirn}", []).append(v)
            for k, vs in sorted(porgrupo.items()):
                archivos = [x for x in vs if x.startswith("archivo:")]
                otros = [x for x in vs if not x.startswith("archivo:")]
                if otros:
                    console.print(f"  {k:<14} " + ", ".join(otros[:8])
                                  + (f" y {len(otros) - 8} mas" if len(otros) > 8 else ""))
                if archivos:
                    console.print(f"  {k:<14} [dim]{len(archivos)} archivos[/]")
        console.print()
        return

    if bloqueos:
        bs = [a for a in g.get("aristas", []) if a["t"] == "bloquea"]
        if not bs:
            console.print("[dim]ningun bloqueo cross-repo declarado en los contratos.[/]")
            console.print()
            return
        pares: dict[tuple[str, str], int] = {}
        for a in bs:
            pares[(a["o"], a["d"])] = pares.get((a["o"], a["d"]), 0) + 1
        console.print(f"[bold]{len(pares)} dependencias cross-repo[/]")
        for (o, d), n in sorted(pares.items(), key=lambda kv: -kv[1]):
            console.print(f"  {o.split(':', 1)[-1]:<34} bloquea a {d.split(':', 1)[-1]}"
                          f"  [dim]({n} items)[/]")
        console.print()
        return

    tipos: dict[str, int] = {}
    for d in nodos.values():
        tipos[d.get("tipo", "?")] = tipos.get(d.get("tipo", "?"), 0) + 1
    console.print("  " + "  ".join(f"{k}={v}" for k, v in sorted(tipos.items())))
    console.print("[dim]  --que-toca <archivo> | --vecinos <slug> | --bloqueos[/]")
    console.print()


@cli.command("contexto")
@click.argument("slug")
@click.option("--vecinos", "max_vecinos", default=4, show_default=True,
              help="Cuantos vecinos incluir.")
def contexto_cmd(slug: str, max_vecinos: int) -> None:
    """El pack minimo para arrancar una sesion: el ticket y el estado de sus vecinos."""
    click.echo(pack_de_contexto(slug, max_vecinos))


def pack_de_contexto(slug: str, max_vecinos: int = 4) -> str:
    """El texto del pack. Aparte del comando porque `resume --nueva` lo inyecta
    como primer prompt de la sesion que abre, no solo lo imprime."""
    g = grafo_vigente()
    cands = [n for n in resolver_nodo(g, slug) if n.startswith(("ticket:", "doc:"))]
    if not cands:
        raise click.ClickException(
            f"'{slug}' no esta en el grafo. `factoria grafo --reconstruir` si es nuevo."
        )
    nid = cands[0]
    nodos = g["nodos"]

    # Vecindad de segundo grado: los trabajos que comparten archivo o contrato.
    compartido: dict[str, set[str]] = {}
    for v, t, _ in adyacentes(g, nid):
        if not v.startswith(("archivo:", "contrato:")):
            continue
        for w, _t2, _d in adyacentes(g, v):
            if w != nid and w.startswith(("ticket:", "doc:")):
                compartido.setdefault(w, set()).add(v)
    orden = sorted(compartido.items(), key=lambda kv: -len(kv[1]))[:max_vecinos]

    partes = [f"# Contexto de {nid}", ""]
    p = nodos.get(nid, {}).get("path")
    if p and Path(p).is_file():
        partes += [Path(p).read_text(encoding="utf-8", errors="replace").strip(), ""]
    if orden:
        partes += ["---", "", "## Vecinos (solo su estado actual)", ""]
        for w, via in orden:
            wp = nodos.get(w, {}).get("path")
            est = ""
            if wp and Path(wp).is_file():
                est = _seccion(Path(wp).read_text(encoding="utf-8", errors="replace"),
                               "## Estado actual")
            razon = ", ".join(sorted(x.split("/", 1)[-1] for x in list(via)[:3]))
            partes += [f"### {w}", f"_comparte: {razon}_", "",
                       (est[:1200] or "_(sin seccion 'Estado actual')_"), ""]
    # El doc de trabajo del PROPIO ticket. `contexto` emitia el estado de los
    # vecinos y se salteaba el suyo, que es donde esta el trabajo hecho: el pack
    # se quedaba corto justo en el ticket que se quiere retomar. Va el estado
    # actual completo y de lo demas solo el indice, para leer una seccion en vez
    # del doc entero -- el mismo criterio que INDICE.md, que se consulta y no se
    # lee.
    tk = next((x for x in tickets_todos() if f"ticket:{x.slug}" == nid), None)
    propios = [Path(e.doc) for e in (tk.repos if tk else [])
               if e.doc and Path(e.doc).is_file()]
    if propios:
        partes += ["---", "", "## Doc de trabajo", ""]
        for dp in propios:
            crudo = dp.read_text(encoding="utf-8", errors="replace")
            lineas = crudo.splitlines()
            partes += [f"### `{dp}`", "",
                       _seccion(crudo, "## Estado actual").strip()
                       or "_(sin seccion 'Estado actual')_", ""]
            idx = [i for i, l in enumerate(lineas) if l.startswith("## ")]
            resto = []
            for k, i in enumerate(idx):
                fin = idx[k + 1] if k + 1 < len(idx) else len(lineas)
                nombre = lineas[i][3:].strip()
                if nombre.lower() != "estado actual":
                    resto.append(f"- `## {nombre}` ({fin - i - 1} lineas)")
            if resto:
                partes += ["_El resto del doc, por si hace falta:_", ""] + resto + [""]
    contratos = [v for v, t, _ in adyacentes(g, nid) if v.startswith("contrato:")]
    if contratos:
        partes += ["---", "", "## Contratos que toca", ""]
        partes += [f"- `.contracts\\{c.split(':', 1)[-1]}.md`" for c in contratos]
    return "\n".join(partes)


# --------------------------------------------------------------------------
# sync-skills: una sola fuente de verdad para skills y agents (plan §5)
# --------------------------------------------------------------------------

def es_junction(p: Path) -> bool:
    try:
        return p.is_dir() and bool(os.readlink(str(p)))
    except OSError:
        return False


def _mismo_dir(a: Path, b: Path) -> bool:
    """Si los dos paths son el mismo directorio en disco, junctions mediante."""
    try:
        return os.path.samefile(str(a), str(b))
    except OSError:
        return False


def _vincular(enlace: Path, destino: Path) -> str:
    """Crea la junction, o dice por que no. Nunca sobrescribe un directorio real."""
    if not destino.is_dir():
        return f"[red]falta el origen[/] {destino}"
    if enlace.exists() or enlace.is_symlink():
        if es_junction(enlace):
            actual = Path(os.readlink(str(enlace)))
            # samefile, y no comparar los paths: os.readlink devuelve la junction
            # con el prefijo de path extendido (\\?\C:\...), que resolve() conserva:
            # la igualdad daba False siempre y toda junction correcta se reportaba
            # apuntando a otro lado.
            if _mismo_dir(enlace, destino):
                return "ya vinculado"
            return f"[yellow]junction apunta a otro lado[/] ({actual})"
        return ("[yellow]hay un directorio real, no una junction[/] -- puede tener "
                "cambios propios; revisalo y borralo a mano")
    enlace.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(enlace), str(destino)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        return f"[red]mklink falló[/] {(r.stderr or r.stdout).strip()[:120]}"
    return "vinculado"


@cli.command("sync-skills")
@click.option("--desvincular", is_flag=True,
              help="Quitar las junctions (con rmdir, no borrado recursivo).")
@click.option("--copiar", is_flag=True,
              help="Copiar en vez de vincular, si el descubrimiento no sigue junctions.")
def sync_skills(desvincular: bool, copiar: bool) -> None:
    """Vincula skills y agents del repo de DATOS a los dos config dirs.

    Hoy handoff/SKILL.md, ship/SKILL.md, buscador.md y planificador.md son
    byte-identicos entre las dos cuentas y hay que editarlos dos veces. La
    granularidad no es uniforme a proposito: `skills\\<nombre>` va por skill
    porque .claude-personal tiene 17 skills de superpowers que una junction del
    directorio entero taparia; `agents` va completo porque las dos cuentas
    tienen exactamente los mismos dos agentes.
    """
    # Viven en el repo de DATOS, que es privado, y no en el de codigo, que es
    # publico: estas skills nombran repos internos, roles y rutas de la empresa.
    origen_skills, origen_agents = DATOS / "skills", DATOS / "agents"
    console.print()
    if not origen_skills.is_dir() and not origen_agents.is_dir():
        console.print(f"[yellow]No hay `skills/` ni `agents/` en {DATOS}.[/]")
        console.print("[dim]Primero hay que mover los archivos del config dir al repo:"
                      " son la fuente de verdad. `--copiar` no aplica todavia.[/]")
        console.print()
        return

    mios = sorted(p.name for p in origen_skills.iterdir() if p.is_dir()) \
        if origen_skills.is_dir() else []
    for cuenta, raiz in CUENTAS.items():
        console.print(f"[bold]{cuenta}[/]  {raiz}")
        for nombre in mios:
            enlace = raiz / "skills" / nombre
            if desvincular:
                console.print(f"  skills/{nombre:<20} {_desvincular(enlace)}")
            elif copiar:
                console.print(f"  skills/{nombre:<20} {_copiar_dir(origen_skills / nombre, enlace)}")
            else:
                console.print(f"  skills/{nombre:<20} {_vincular(enlace, origen_skills / nombre)}")
        if origen_agents.is_dir():
            enlace = raiz / "agents"
            if desvincular:
                console.print(f"  agents{'':<21} {_desvincular(enlace)}")
            elif copiar:
                console.print(f"  agents{'':<21} {_copiar_dir(origen_agents, enlace)}")
            else:
                console.print(f"  agents{'':<21} {_vincular(enlace, origen_agents)}")
    console.print()
    if not desvincular and not copiar:
        console.print("[dim]Verificá que Claude Code siga las junctions: abrí una sesion "
                      "y pedile la lista de skills. Si no aparecen, `--copiar`.[/]")
        console.print()


def _desvincular(enlace: Path) -> str:
    """Saca la junction con rmdir. NUNCA borrado recursivo: una junction es un
    reparse point y un rm -r lo atraviesa, llevandose el repo de codigo."""
    if not (enlace.exists() or enlace.is_symlink()):
        return "no existe"
    if not es_junction(enlace):
        return "[yellow]es un directorio real, no se toca[/]"
    r = subprocess.run(["cmd", "/c", "rmdir", str(enlace)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return "desvinculado" if r.returncode == 0 else \
        f"[red]rmdir falló[/] {(r.stderr or r.stdout).strip()[:100]}"


def _copiar_dir(origen: Path, destino: Path) -> str:
    if es_junction(destino):
        return "[yellow]hay una junction; --desvincular primero[/]"
    destino.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in origen.rglob("*"):
        if f.is_file():
            d = destino / f.relative_to(origen)
            d.parent.mkdir(parents=True, exist_ok=True)
            d.write_bytes(f.read_bytes())
            n += 1
    return f"copiado ({n} archivos)"


# --------------------------------------------------------------------------
# commit: reemplazo del auto-commit (paso 7)
# --------------------------------------------------------------------------

COTA_MENSAJE = 60
TIPOS_COMMIT = ("feat", "fix", "chore", "refactor", "docs", "test", "perf",
                "build", "ci", "style", "config", "wip")
RE_MENSAJE = re.compile(rf"^({'|'.join(TIPOS_COMMIT)})(\([a-z0-9._/-]+\))?: \S.*$")

# Guardas que ya tenian los hooks y no se pierden en el reemplazo.
RAMAS_PROHIBIDAS = ("master", "main", "desarrollo-ari", "HEAD", "")


def path_msg(repo: str, rama: str) -> Path:
    """Un archivo por repo+rama: con varias sesiones en paralelo, un solo
    `.factoria/msg` seria una carrera y una sesion commitearia el mensaje de otra."""
    seguro = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{repo}__{rama}").strip("-")
    return DATOS / "msg" / f"{seguro}.txt"


def validar_mensaje(m: str) -> str | None:
    """Devuelve el motivo del rechazo, o None si esta bien."""
    m = m.strip()
    if not m:
        return "esta vacio"
    if "\n" in m:
        return "tiene mas de una linea"
    if len(m) > COTA_MENSAJE:
        return f"{len(m)} caracteres, cota {COTA_MENSAJE}"
    if not RE_MENSAJE.match(m):
        return (f"no tiene la forma `<tipo>: <imperativo>` con tipo en "
                f"{'/'.join(TIPOS_COMMIT[:6])}/...")
    if m.rstrip().endswith("."):
        return "termina en punto"
    return None


@cli.command("commit")
@click.option("--mensaje", help="El mensaje. Sin esto se lee el archivo de la sesion.")
@click.option("--imprimir-hook", is_flag=True,
              help="Imprime el stanza de settings.local.json para pegar.")
@click.option("--seco", is_flag=True, help="Mostrar que haria, sin commitear.")
def commit_cmd(mensaje: str | None, imprimir_hook: bool, seco: bool) -> None:
    """Commitea con un mensaje breve y validado. Pensado para el hook Stop."""
    if imprimir_hook:
        console.print("""
Pegar en el `settings.local.json` del repo, reemplazando el hook Stop que hoy
hace `auto-commit (claude): <timestamp>`:

  "hooks": {
    "Stop": [
      { "hooks": [ { "type": "command", "command": "factoria commit" } ] }
    ]
  }

factoria NO edita ese archivo: es tu configuracion y puede tener otros hooks
(en defeve hay un PostToolUse que bloquea con exit 2 si el Groovy no compila,
y un portero-roles.sh en Stop). El orden importa: dejá `factoria commit`
DESPUES de los hooks que validan.

El agente escribe el mensaje antes de terminar el turno:
  factoria commit --mensaje "fix: cerrar el modal al anular"
o lo deja en el archivo que `factoria commit --seco` te muestra.
""".strip())
        return

    rp = repo_del_cwd()
    if not rp:
        # Silencioso: el hook corre en cualquier sesion, no solo en un repo.
        return
    rama = git(rp, "rev-parse", "--abbrev-ref", "HEAD")
    pf = perfil(rp.name)
    # `or` no sirve aca: una lista vacia explicita es una decision, no un campo
    # sin llenar. El repo de datos declara `ramas_prohibidas: []` porque `main`
    # es el unico lugar donde su verdad puede estar, y con `or` volvia el
    # default y el commit se negaba siempre, en silencio.
    prohibidas = tuple(pf["ramas_prohibidas"] if "ramas_prohibidas" in pf
                       else RAMAS_PROHIBIDAS)
    if rama in prohibidas:
        # Se avisa siempre, no solo en --seco: quedarse callado con cambios sin
        # commitear en una rama protegida es como se pierde trabajo.
        console.print(f"[dim]{rp.name}: rama '{rama}' protegida, no se commitea.[/]")
        return

    archivo = path_msg(rp.name, rama)
    if not mensaje and archivo.is_file():
        mensaje = archivo.read_text(encoding="utf-8", errors="replace").strip()
    # Sin mensaje se commitea igual, marcado: perder el trabajo es peor que un
    # mensaje pobre, y `board` lo reporta.
    fallback = not mensaje
    if fallback:
        t = next((x for x in tickets_todos()
                  for e in x.repos if e.rama == rama and e.repo == rp.name), None)
        mensaje = f"wip: {t.slug if t else rama.rsplit('/', 1)[-1]}"[:COTA_MENSAJE]
    if (motivo := validar_mensaje(mensaje)):
        raise click.ClickException(
            f"mensaje rechazado ({motivo}):\n  {mensaje}\n"
            f"Forma: `<tipo>: <imperativo>`, hasta {COTA_MENSAJE} caracteres, "
            "sin punto final."
        )

    excluir = [f":(exclude){p}" for p in (pf.get("excluir_commit") or [])]
    proteger = list(pf.get("proteger_commit") or [])
    if seco:
        console.print(f"[bold]{rp.name}[/] rama {rama}")
        console.print(f"  mensaje   {mensaje}" + ("  [yellow](fallback)[/]" if fallback else ""))
        console.print(f"  archivo   {archivo}")
        console.print(f"  excluye   {', '.join(pf.get('excluir_commit') or []) or '-'}")
        console.print(f"  protege   {', '.join(proteger) or '-'}")
        console.print(f"  cambios   {len(git(rp, 'status', '--porcelain').splitlines())} archivos")
        return

    _git_o_falla(rp, "add", "-A", "--", ".", *excluir)
    for p in proteger:
        subprocess.run(["git", "-C", str(rp), "reset", "-q", "--", p],
                       capture_output=True, text=True)
    hay = subprocess.run(["git", "-C", str(rp), "diff", "--cached", "--quiet"],
                         capture_output=True, text=True)
    if hay.returncode == 0:
        return  # nada staged
    _git_o_falla(rp, "commit", "-q", "-m", mensaje)
    if archivo.is_file():
        archivo.unlink()
    console.print(f"[dim]{rp.name} {rama}: {mensaje}[/]")


# --------------------------------------------------------------------------
# aprobar / open / close (paso 9). Aca el spec adquiere dientes: `open` se
# niega si no esta aprobado, y esa negativa es el unico mecanismo que pone el
# requerimiento antes del codigo.
# --------------------------------------------------------------------------

# El checkbox se normaliza antes de hashear la huella: ver huella_spec.
RE_TILDE = re.compile(r"^(\s*[-*]\s*\[)[xX](\])", re.M)


def huella_spec(t: Ticket) -> str:
    """Hash de criterios + fuera de alcance. Es una constancia, no una firma:
    su valor es que la deriva se vuelve detectable.

    Tildar un criterio NO cuenta: es progreso, no un cambio de spec. Sin
    normalizar el checkbox la deriva saltaba en el camino normal -- se aprueba
    al final de `plan` con todo en `[ ]` y cada `[x]` de `dev`/`test` la
    disparaba, o sea que el gate acusaba de cambiar el spec justamente a quien
    lo estaba cumpliendo. Editar el TEXTO de un criterio si sigue siendo
    deriva, que es lo que el gate existe para ver.

    `## Entregables de código` tampoco entra, y a proposito: es el DONDE, no el
    que. Descubrir superficie nueva durante `dev` es sano y pasa casi siempre;
    pintarlo de spec-drift rojo convertiria el gate en ruido -- el mismo error
    que el checkbox, un escalon mas arriba.
    """
    import hashlib
    material = "\n".join(
        _seccion(t.cuerpo, e) for e in ("## Criterios de aceptación", "## Fuera de alcance")
    )
    material = RE_TILDE.sub(r"\1 \2", material)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def url_compare(rp: Path, base: str, rama: str) -> str:
    u = git(rp, "remote", "get-url", "origin")
    if not u:
        return ""
    if u.startswith("git@"):  # git@host:ws/repo.git
        u = "https://" + u[4:].replace(":", "/", 1)
    u = u.removesuffix(".git")
    if "bitbucket.org" in u:
        # Bitbucket no tiene /compare: la vista de la rama es el equivalente util.
        return f"{u}/branch/{rama}"
    return f"{u}/compare/{base}...{rama}"


@cli.command("aprobar")
@click.argument("slug")
def aprobar_cmd(slug: str) -> None:
    """Congela criterios y fuera de alcance. Habilita `open` en otros repos."""
    t = buscar_ticket(slug)
    faltan = [e for e in EXIGIDAS_PARA_APROBAR if not _tiene_items(t.cuerpo, e)]
    if faltan:
        # Vacia y ausente son dos arreglos distintos: una se llena, la otra se
        # pega. Un ticket anterior a que existiera `Entregables de código` no la
        # tiene, y decirle "esta vacia" lo manda a buscar algo que no esta.
        encabezados = {l.strip() for l in t.cuerpo.splitlines()}
        ausentes = [e for e in faltan if e not in encabezados]
        msg = ["no puedo aprobar un spec incompleto:"]
        if vacias := [e for e in faltan if e not in ausentes]:
            msg.append("  vacias:   " + ", ".join(e.removeprefix("## ") for e in vacias))
        if ausentes:
            msg.append("  ausentes: " + ", ".join(e.removeprefix("## ") for e in ausentes)
                       + "   (ticket anterior a la plantilla: pegá el encabezado)")
        msg.append(f"Editá {t.path} y volvé a correr.")
        raise click.ClickException("\n".join(msg))
    if not _tiene_items(t.cuerpo, "## Supuestos abiertos"):
        console.print("[yellow]Ojo:[/] 'Supuestos abiertos' esta vacio. Eso no significa "
                      "que no haya supuestos, significa que no se escribieron.\n")
    t.spec_congelado = huella_spec(t)
    escribir_ticket(t)
    regenerar_indice()
    console.print(f"[bold]{t.slug}[/] aprobado, huella {t.spec_congelado}")
    # El tablero tiene un campo `spec`: sin esto quedaria diciendo "sin aprobar"
    # hasta el proximo cambio de fase, que es justo el gate que `open` consulta.
    espejar_si_se_puede(t)
    console.print("[dim]Si los criterios o el fuera de alcance cambian de ahora en mas, "
                  "`board` lo marca como spec-drift.[/]")


@cli.command("open")
@click.argument("slug")
@click.option("--repo", required=True, help="El segundo repo que suma el ticket.")
@click.option("--cuenta", type=click.Choice(list(CUENTAS)))
@click.option("--rama", "rama_pedida", help="Nombre completo de la rama.")
@click.option("--tipo", type=click.Choice(TIPOS_RAMA), default="feature", show_default=True)
@click.option("--worktree/--sin-worktree", default=None,
              help="Forzar o evitar el ritual de worktree del perfil.")
@click.option("--no-lanzar", is_flag=True)
@click.option("--forzar", is_flag=True)
def open_cmd(slug: str, repo: str, cuenta: str | None, rama_pedida: str | None,
             tipo: str, worktree: bool | None, no_lanzar: bool, forzar: bool) -> None:
    """Suma un repo al ticket, con su propia sesion y cuenta. Exige spec aprobado."""
    t = buscar_ticket(slug)
    if not t.spec_congelado:
        raise click.ClickException(
            f"'{t.slug}' no tiene el spec aprobado.\n"
            "Cruzar a un segundo repo con el requerimiento sin cerrar es como se "
            f"multiplica una mala interpretacion. Corré `factoria aprobar {t.slug}`."
        )
    rp = ruta_repo(repo)
    if not rp:
        raise click.ClickException(f"no encuentro el repo '{repo}'.")
    if t.entrada(rp.name):
        raise click.ClickException(
            f"'{t.slug}' ya tiene entrada para {rp.name}. "
            f"`factoria resume {t.slug} --repo {rp.name}`"
        )
    pf = perfil(rp.name)
    cta = cuenta or pf.get("cuenta") or "dfv"
    base = pf.get("base") or base_de(rp) or ""
    rama = rama_pedida or f"{tipo}/{t.slug}"
    usar_wt = pf.get("worktree", False) if worktree is None else worktree

    if usar_wt:
        cwd = _ritual_worktree(rp, pf, rama, base)
    else:
        _resolver_rama(rp, base, git(rp, "rev-parse", "--abbrev-ref", "HEAD"),
                       rama, t.slug, tipo, False)
        cwd = str(rp)

    sid = str(uuid.uuid4())
    doc = crear_doc(rp.name, t.slug)
    t.repos.append(RepoTicket(repo=rp.name, cuenta=cta, rama=rama, cwd=cwd,
                              session_id=sid, doc=str(doc)))
    escribir_ticket(t)
    regenerar_indice()
    console.print(f"[bold]{t.slug}[/] + {rp.name} ({cta})  rama {rama}")
    console.print(f"  cwd     {cwd}")
    console.print(f"  doc     {doc}")
    console.print(f"  sesion  {sid}")
    if no_lanzar:
        console.print(f"[dim]Para abrirla: factoria resume {t.slug} --repo {rp.name}[/]")
        return
    _lanzar_en(cta, cwd, ["--session-id", sid], forzar, False, f"{rp.name} | {rama}")


def _ritual_worktree(rp: Path, pf: dict, rama: str, base: str) -> str:
    """El ritual de §10.1/§10.3: worktree, copias no versionadas y junctions.

    Las junctions de node_modules y target existen porque reinstalar y recompilar
    por worktree cuesta minutos; y son la razon de que `close` tenga que sacarlas
    con rmdir ANTES del worktree remove.
    """
    raiz_wt = Path(pf.get("worktree_raiz") or (rp.parent / "wt"))
    destino = raiz_wt / rama.rsplit("/", 1)[-1]
    if destino.exists():
        raise click.ClickException(f"ya existe {destino}.")
    raiz_wt.mkdir(parents=True, exist_ok=True)
    if existe_rama(rp, rama):
        _git_o_falla(rp, "worktree", "add", str(destino), rama)
    else:
        _git_o_falla(rp, "worktree", "add", "-b", rama, str(destino), base)
    console.print(f"[dim]worktree {destino}[/]")

    for rel in (pf.get("copiar_al_worktree") or []):
        o, d = rp / rel, destino / rel
        if o.is_file():
            d.parent.mkdir(parents=True, exist_ok=True)
            d.write_bytes(o.read_bytes())
            console.print(f"[dim]  copiado {rel}[/]")
    for rel in (pf.get("skip_worktree") or []):
        if (destino / rel).exists():
            subprocess.run(["git", "-C", str(destino), "update-index",
                            "--skip-worktree", rel], capture_output=True, text=True)
            console.print(f"[dim]  skip-worktree {rel}[/]")
    for rel in (pf.get("junctions") or []):
        o, d = (rp / rel), (destino / rel)
        if o.is_dir() and not d.exists():
            d.parent.mkdir(parents=True, exist_ok=True)
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(d), str(o)],
                               capture_output=True, text=True)
            console.print(f"[dim]  junction {rel}"
                          + ("" if r.returncode == 0 else " [red]FALLO[/]") + "[/]")
    return str(destino)


def _archivar(t: Ticket, hist: Path) -> str:
    """Mueve el cuerpo al historial y deja el ticket en una linea.

    Devuelve la seccion `## Decisiones`, que es lo unico que sobrevive al
    resumen porque `close` la imprime para pasarla a un ADR.
    """
    hist.parent.mkdir(parents=True, exist_ok=True)
    hist.write_text(
        f"# {t.slug}\n\nCerrado {time.strftime('%Y-%m-%d')}. "
        f"Fase final: {t.fase}. Issue: {t.issue_url or '-'}\n\n" + t.cuerpo.strip() + "\n",
        encoding="utf-8", newline="\n")
    decisiones = _seccion(t.cuerpo, "## Decisiones")
    t.cuerpo = (f"# {t.slug}\n\n## Estado actual\n\nCerrado. El cuerpo completo esta en "
                f"`historial/{t.slug}.md`.\n")
    return decisiones


def _pasos_manuales(slug: str, rp: Path, trabajo: Path, e: RepoTicket, base: str,
                    pusheada: bool) -> list[str]:
    """Los pasos que factoria NO hace, en el orden en que hay que hacerlos.

    El borrado de rama va condicionado al merge y con `-d` minuscula a
    proposito: si falla es que algo no se mergeo, y ahi hay que investigar, no
    forzar con `-D`. Y el worktree va ANTES del borrado local: mientras la rama
    este checkouteada en un worktree, `branch -d` no puede sacarla.
    """
    rama = e.rama
    if not rama:
        return []
    # Nunca proponer borrar la base ni una rama protegida: seria el peor consejo
    # posible, y pasa de verdad porque `new --aqui` registra la rama actual.
    pasos = []
    if rama == base:
        # `new --aqui` registra la rama actual, que puede ser la base. Ahi no hay
        # PR (comparar una rama contra si misma no dice nada) ni rama que borrar.
        pasos.append(f"git -C {trabajo} push origin {rama}" if not pusheada else
                     f"[dim]el trabajo esta en '{rama}' y ya pusheado: no hay PR "
                     "ni rama que borrar.[/]")
        return pasos
    prohibidas = set(perfil(e.repo).get("ramas_prohibidas") or ())
    borrable = rama not in prohibidas
    if not pusheada:
        pasos.append(f"git -C {trabajo} push -u origin {rama}")
    if (u := url_compare(rp, base, rama)):
        bitbucket = "bitbucket.org" in u
        pasos.append(("abrir el PR a mano en Bitbucket" if bitbucket
                      else f"abrir el PR de {rama} a {base}") + f":  {u}")
    if not borrable:
        pasos.append(f"[dim]'{rama}' es la base o una rama protegida: no se borra.[/]")
        return pasos
    hay_wt = trabajo != rp
    pasos.append("[bold]cuando el PR este mergeado[/], y no antes:")
    if hay_wt:
        pasos.append(f"  factoria close {slug} --repo {e.repo} --limpiar-worktree"
                     "   [dim](junctions con rmdir y despues worktree remove)[/]")
    pasos.append(f"  git -C {rp} branch -d {rama}   "
                 "[dim](-d minuscula: si falla, no se mergeo)[/]")
    pasos.append(f"  git -C {rp} push origin --delete {rama}")
    return pasos


@cli.command("close")
@click.argument("slug")
@click.option("--repo", help="Cerrar solo la entrada de un repo.")
@click.option("--pushear/--sin-pushear", default=True, show_default=True)
@click.option("--limpiar-worktree", is_flag=True,
              help="Sacar junctions y el worktree. Irreversible sobre el directorio.")
def close_cmd(slug: str, repo: str | None, pushear: bool, limpiar_worktree: bool) -> None:
    """Cierra el ticket: pushea, archiva el cuerpo y deja listo el compare. NO crea el PR."""
    t = buscar_ticket(slug)
    entradas = [e for e in t.repos if not repo or repo.lower() in e.repo.lower()]
    if not entradas:
        raise click.ClickException(f"'{t.slug}' no tiene entrada para --repo {repo}.")

    urls = []
    for e in entradas:
        rp = ruta_repo(e.repo)
        if not rp:
            console.print(f"[yellow]{e.repo}: no esta en disco, se saltea.[/]")
            continue
        trabajo = Path(e.cwd) if e.cwd and Path(e.cwd).is_dir() else rp
        if (sucio := git(trabajo, "status", "--porcelain")):
            raise click.ClickException(
                f"{e.repo} tiene cambios sin commitear:\n{sucio[:300]}\n"
                "Cerrá con el arbol limpio: si no, lo que quede afuera no se pushea."
            )
        base = perfil(e.repo).get("base") or base_de(rp) or ""
        pusheada = False
        if pushear and e.rama:
            r = subprocess.run(["git", "-C", str(trabajo), "push", "-u", "origin", e.rama],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace")
            pusheada = r.returncode == 0
            console.print(f"[dim]{e.repo}: push {'ok' if pusheada else 'FALLO'}[/]")
            if not pusheada:
                console.print(f"  [yellow]{(r.stderr or '').strip()[:200]}[/]")
        elif e.rama:
            # Sin push no hay nada que comparar todavia: la rama no esta en el
            # remoto. `pusheada` queda en False y el primer paso manual es el push.
            pusheada = bool(git(rp, "rev-parse", "--verify", "--quiet",
                                f"refs/remotes/origin/{e.rama}"))
        if limpiar_worktree and e.cwd and Path(e.cwd) != rp and Path(e.cwd).exists():
            _quitar_worktree(rp, Path(e.cwd))
            trabajo = rp
        if (pasos := _pasos_manuales(t.slug, rp, trabajo, e, base, pusheada)):
            urls.append((e.repo, pasos))

    # Archivar UNA vez. Al segundo `close` el cuerpo ya es el puntero al
    # historial, asi que re-archivar sobrescribe el historial con "el cuerpo
    # completo esta en historial/" -- se pierde lo archivado la primera vez.
    hist = DATOS / "historial" / f"{t.slug}.md"
    if not t.abierto and hist.is_file():
        console.print(f"[dim]{t.slug} ya estaba cerrado: no se re-archiva.[/]")
        decisiones = ""
    else:
        decisiones = _archivar(t, hist)
    t.fase, t.abierto = "cerrado", False
    escribir_ticket(t)
    espejar_si_se_puede(t)
    regenerar_indice()

    console.print()
    console.print(f"[bold]{t.slug}[/] cerrado. Historial en {hist}")
    if decisiones:
        console.print("[bold]Decisiones para pasar a ADR:[/]")
        for l in decisiones.splitlines():
            if l.strip():
                console.print(f"  {l.strip()}")
    if urls:
        console.print("\n[bold]Falta a mano, en este orden:[/]")
        for nombre, pasos in urls:
            console.print(f"\n  [bold]{nombre}[/]")
            for n, paso in enumerate(pasos, 1):
                # Los sub-pasos del borrado ya vienen indentados: no se numeran.
                pref = f"  {n}. " if not paso.startswith("  ") else "     "
                console.print(f"  {pref}{paso}")
    console.print("\n[dim]factoria no crea el PR ni mergea: eso es tuyo y de los "
                  "revisores.[/]")
    console.print()


def _quitar_worktree(rp: Path, wt: Path) -> None:
    """Junctions primero, con rmdir. Al reves, el borrado recursivo del worktree
    atraviesa el reparse point y se lleva el node_modules y el target del
    checkout principal."""
    for hijo in sorted(wt.rglob("*"), reverse=True):
        if es_junction(hijo):
            r = subprocess.run(["cmd", "/c", "rmdir", str(hijo)],
                               capture_output=True, text=True)
            console.print(f"[dim]  junction fuera: {hijo.name}"
                          + ("" if r.returncode == 0 else " [red]FALLO[/]") + "[/]")
    r = subprocess.run(["git", "-C", str(rp), "worktree", "remove", str(wt)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    console.print(f"[dim]  worktree remove: {'ok' if r.returncode == 0 else (r.stderr or '').strip()[:150]}[/]")


# --------------------------------------------------------------------------
# check + pre-push (paso 8)
# --------------------------------------------------------------------------

COLA_SALIDA = 20  # lineas de stdout que van al doc; el resto, a logs/


@cli.command("check")
@click.option("--slug", help="Escribir la evidencia en el doc de este ticket.")
@click.option("--instalar-pre-push", is_flag=True,
              help="Poblar el hooksPath del repo con un pre-push que corra esto.")
def check_cmd(slug: str | None, instalar_pre_push: bool) -> None:
    """Corre las verificaciones del perfil y las cotas. Sirve como pre-push."""
    rp = repo_del_cwd()
    if not rp:
        raise click.ClickException("no estas dentro de un repo conocido.")
    pf = perfil(rp.name)

    if instalar_pre_push:
        # NO se re-apunta core.hooksPath: en defeve ya esta seteado a .githooks
        # y ese directorio esta VACIO, lo que tiene los hooks de git apagados.
        # Re-apuntarlo hubiera dejado el problema intacto; hay que POBLARLO.
        configurado = git(rp, "config", "core.hooksPath")
        destino = (rp / configurado) if configurado else (rp / ".git" / "hooks")
        destino.mkdir(parents=True, exist_ok=True)
        h = destino / "pre-push"
        h.write_text(
            "#!/bin/sh\n"
            "# Instalado por `factoria check --instalar-pre-push`.\n"
            "exec factoria check\n",
            encoding="utf-8", newline="\n")
        os.chmod(h, 0o755)
        console.print(f"[bold]pre-push instalado[/] en {h}")
        if configurado:
            console.print(f"[dim]core.hooksPath ya apuntaba a '{configurado}' y estaba "
                          "vacio: por eso los hooks de git no corrian. Ahora si.[/]")
        else:
            console.print("[dim]core.hooksPath no estaba seteado; se uso .git/hooks.[/]")
        return

    fallas: list[str] = []
    lineas_ev: list[str] = []

    # 1. Archivos protegidos que nunca deben viajar.
    for p in (pf.get("proteger_commit") or []):
        r = subprocess.run(["git", "-C", str(rp), "log", "--oneline", "-1", "--", p],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        staged = subprocess.run(["git", "-C", str(rp), "diff", "--cached", "--name-only", "--", p],
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
        if staged.stdout.strip():
            fallas.append(f"{p} esta staged y no debe commitearse")
        lineas_ev.append(f"- [{'x' if not staged.stdout.strip() else ' '}] `{p}` no staged")

    # 2. Cotas del estandar. Es lo unico que hoy se puede verificar sin suite.
    if (ca := cota_estado_actual(rp)):
        linea, largo = ca
        ok = largo <= COTA_ESTADO_ACTUAL
        if not ok:
            fallas.append(f"CLAUDE.md 'Estado de la Mision' tiene {largo} lineas "
                          f"(cota {COTA_ESTADO_ACTUAL}), linea {linea}")
        lineas_ev.append(f"- [{'x' if ok else ' '}] CLAUDE.md estado actual "
                         f"{largo}/{COTA_ESTADO_ACTUAL} lineas")

    # 3. Comandos declarados en el perfil. Hoy ninguno tiene: ver el aviso final.
    for cmd in (pf.get("verify") or []):
        r = subprocess.run(cmd, shell=True, cwd=str(rp), capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        ok = r.returncode == 0
        salida = (r.stdout or "") + (r.stderr or "")
        cola = "\n".join(salida.splitlines()[-COLA_SALIDA:])
        log = DATOS / "logs" / f"{rp.name}-{time.strftime('%Y%m%d-%H%M%S')}.txt"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(salida, encoding="utf-8", newline="\n")
        if not ok:
            fallas.append(f"`{cmd}` salio con {r.returncode}")
        lineas_ev.append(f"- [{'x' if ok else ' '}] `{cmd}`\n```\n{cola}\n```\n"
                         f"  log completo: `{log}`")

    console.print()
    for l in lineas_ev:
        # markup=False: estas lineas son para el archivo y arrancan con `[x]`
        # o `[ ]`, que rich se come como si fuera un tag de estilo.
        console.print("  " + l.splitlines()[0], markup=False)
    if not pf.get("verify"):
        console.print("[yellow]Este perfil no declara comandos `verify:`[/], asi que esto "
                      "solo verifico cotas y archivos protegidos.\n"
                      f"[dim]Agregá en {DATOS / 'profiles' / (rp.name + '.yml')}:\n"
                      "  verify:\n    - <el comando mas pobre que ya tengas: que compile, "
                      "que levante>[/]")

    if slug:
        t = buscar_ticket(slug)
        e = _entrada_unica(t, rp.name, exacto=True)
        if e.doc and Path(e.doc).is_file():
            bloque = (f"\n## Evidencia ({time.strftime('%Y-%m-%d %H:%M')})\n\n"
                      + "\n".join(lineas_ev) + "\n")
            with Path(e.doc).open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(bloque)
            console.print(f"[dim]evidencia agregada a {e.doc}[/]")

    if fallas:
        console.print()
        for f in fallas:
            console.print(f"  [red]FALLA[/] {f}")
        raise SystemExit(1)
    console.print("\n[green]ok[/]\n")


@cli.command("perfiles")
@click.option("--sobrescribir", is_flag=True, help="Regenerar los que ya existen.")
def perfiles_cmd(sobrescribir: bool) -> None:
    """Genera un perfil por repo, detectando donde van sus docs de trabajo."""
    escritos = sembrar_perfiles(sobrescribir)
    d = DATOS / "profiles"
    console.print()
    if escritos:
        console.print(f"[bold]Escritos[/] en {d}: " + ", ".join(escritos))
    else:
        console.print(f"[dim]Ya existian todos en {d} (--sobrescribir para regenerar).[/]")
    for p in sorted(d.glob("*.yml")):
        pf = perfil(p.stem)
        console.print(f"  {p.stem:<22} cuenta={pf.get('cuenta'):<9} "
                      f"docs={pf.get('docs'):<10} -> {ruta_doc(p.stem, '<slug>')}")
    console.print()


if __name__ == "__main__":
    cli()
