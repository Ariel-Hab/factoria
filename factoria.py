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
from dataclasses import dataclass, field
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

# --------------------------------------------------------------------------
# Layout (ver plan §4). Todo por path absoluto: no depende del cwd.
# --------------------------------------------------------------------------
ARIEL = Path(r"C:\ariel")
DATOS = ARIEL / ".factoria"
CONTRATOS = ARIEL / "dfv" / ".contracts"
RAICES = [ARIEL / "dfv", ARIEL / "integhra"]
# gstack es un template de terceros; skills/~ es un directorio espurio.
IGNORAR_REPOS = {"gstack", "skills", "factoria"}

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
        out.append(Rama(repo.name, nombre, dias, bool(upstream) or nombre in remotas))
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
    return rel


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

    for wt in rel.worktrees:
        if (p := wt.problema(rel.bases.get(wt.repo, ""))):
            h.append(f"[red]worktree miente[/] {wt.repo}: {Path(wt.path).name} -> {p}")

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
            f"{UMBRAL_SESION_MB:.1f} MB, conviene cortarlas: "
            + ", ".join(f"{s.rama or s.proyecto} ({s.mb:.1f}MB)" for s in caras[:3])
            + (f" y {len(caras) - 3} mas" if len(caras) > 3 else "")
            + "  ->  factoria cortar <rama> --fork"
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

    console.print()
    console.print(
        f"[bold]factoria board[/]  |  {len(rel.repos)} repos  |  "
        f"{len(rel.docs)} docs  |  {len(rel.ramas)} ramas sin mergear  |  "
        f"{len(rel.sesiones)} sesiones"
    )

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


def _lanzar(s: Sesion, args: list[str], forzar: bool, imprimir: bool) -> None:
    """Arranca claude con la cuenta y el cwd registrados de la sesion."""
    binario = shutil.which("claude")
    cmd = ["claude", *args]
    receta = (f'set CLAUDE_CONFIG_DIR={s.config_dir}\n'
              f'cd /d {s.cwd}\n'
              f'{" ".join(cmd)}')
    if imprimir:
        console.print(receta)
        return
    if not s.cwd_existe:
        raise click.ClickException(
            f"el cwd registrado ya no existe: {s.cwd}\n"
            "La sesion sigue en disco; si el worktree se borro, recrealo o usa --imprimir."
        )
    if os.environ.get("CLAUDECODE") and not forzar:
        console.print(
            "[yellow]Estas adentro de una sesion de Claude.[/] Anidar otra encima consume "
            "tokens de las dos y el output se mezcla. Corre esto en una terminal aparte:\n"
        )
        console.print(receta)
        console.print("\n[dim](o --forzar si de verdad querias anidarla)[/]")
        return
    if not binario:
        raise click.ClickException("no encuentro `claude` en el PATH.")
    entorno = os.environ.copy()
    entorno["CLAUDE_CONFIG_DIR"] = str(s.config_dir)
    console.print(f"[dim]{s.cuenta} | {s.cwd} | {s.rama or '(sin rama)'}[/]")
    subprocess.run([binario, *args], cwd=s.cwd, env=entorno)


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


@cli.command()
@click.argument("consulta")
@click.option("--cuenta", type=click.Choice(list(CUENTAS)), help="Acotar a una cuenta.")
@click.option("--elegir", type=int, help="Indice de la lista cuando matchean varias.")
@click.option("--dias", default=0, help="Solo sesiones de hace <= N dias. 0 = todas.")
@click.option("--imprimir", is_flag=True, help="Mostrar el comando sin ejecutarlo.")
@click.option("--forzar", is_flag=True, help="Permitir anidar dentro de otra sesion.")
def resume(consulta: str, cuenta: str | None, elegir: int | None, dias: int,
           imprimir: bool, forzar: bool) -> None:
    """Reanuda la sesion de una tarea: continua la conversacion, no abre otra."""
    ss = buscar_sesiones(consulta, cuenta, dias or None)
    s = _elegir_una(ss, consulta, elegir, "resume")
    if s.mb >= UMBRAL_SESION_MB:
        console.print(
            f"[yellow]Ojo:[/] esta sesion pesa {s.mb:.1f} MB (umbral {UMBRAL_SESION_MB}). "
            "Cada turno paga lectura de cache sobre todo ese prefijo.\n"
            f"[dim]Alternativa: factoria cortar {consulta} --fork[/]\n"
        )
    _lanzar(s, ["-r", s.session_id], forzar, imprimir)


@cli.command()
@click.argument("consulta")
@click.option("--fork", is_flag=True,
              help="Ramificar desde la sesion actual, preservandola intacta.")
@click.option("--cuenta", type=click.Choice(list(CUENTAS)))
@click.option("--elegir", type=int)
@click.option("--imprimir", is_flag=True)
@click.option("--forzar", is_flag=True)
def cortar(consulta: str, fork: bool, cuenta: str | None, elegir: int | None,
           imprimir: bool, forzar: bool) -> None:
    """Corta una sesion cara. Con --fork ramifica; sin el, arranca limpia."""
    ss = buscar_sesiones(consulta, cuenta, None)
    s = _elegir_una(ss, consulta, elegir, "cortar")
    if fork:
        _lanzar(s, ["-r", s.session_id, "--fork-session"], forzar, imprimir)
        return
    console.print(
        f"[yellow]Sesion nueva y limpia[/] en {s.cwd} ({s.cuenta}), rama "
        f"{s.rama or '(sin rama)'}.\n"
        f"[dim]La anterior queda intacta: factoria resume {consulta} --elegir 1[/]\n"
        "[dim]El pack de contexto automatico (`factoria contexto`) llega en el paso 6; "
        "por ahora la sesion arranca solo con el CLAUDE.md del repo.[/]\n"
    )
    _lanzar(s, [], forzar, imprimir)


if __name__ == "__main__":
    cli()
