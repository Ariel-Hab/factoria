#!/usr/bin/env python
"""factoria - gestor de tickets, sesiones y documentacion.

Paso 1: `board` (solo lectura). No escribe nada: releva el estado real del
ecosistema a partir de los artefactos que ya existen (branches, worktrees,
docs de trabajo, sesiones, contratos) y reporta las cotas violadas.
"""
from __future__ import annotations

import json
import re
import subprocess
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
            out.append(Sesion(
                cuenta=cuenta,
                # El slug del proyecto es el primer componente bajo projects/,
                # no el directorio inmediato (hay sesiones anidadas).
                proyecto=jsonl.relative_to(proyectos).parts[0],
                session_id=jsonl.stem,
                mb=st.st_size / 1_048_576,
                dias=int((ahora - st.st_mtime) // 86400),
            ))
    return sorted(out, key=lambda s: -s.mb)


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
            + ", ".join(f"{s.proyecto}/{s.mb:.1f}MB" for s in caras[:4])
            + (f" y {len(caras) - 4} mas" if len(caras) > 4 else "")
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


if __name__ == "__main__":
    cli()
