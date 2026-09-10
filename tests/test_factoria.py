"""Smoke y regresiones de factoria. Es el `verify:` del perfil: sin esto
`check` mide cotas y nada mas, o sea es un linter de documentacion.

Cubre lo que `ruff --select F` no ve. Se comprobo empiricamente que ruff SI
atrapa nombres indefinidos (F821, el bug que motivo esto) pero NO atrapa una
funcion definida dos veces en este archivo, asi que eso va como test.
"""
import ast
import importlib.util
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

RAIZ = Path(__file__).resolve().parent.parent
FUENTE = RAIZ / "factoria.py"


def _cargar():
    spec = importlib.util.spec_from_file_location("factoria_bajo_prueba", FUENTE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


fx = _cargar()


def test_nada_definido_dos_veces():
    """Un `def` repetido gana silenciosamente y el primero queda muerto."""
    arbol = ast.parse(FUENTE.read_text(encoding="utf-8"))
    vistos: dict[str, int] = {}
    dobles = []
    for n in arbol.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if n.name in vistos:
                dobles.append(f"{n.name} en {vistos[n.name]} y {n.lineno}")
            vistos[n.name] = n.lineno
    assert not dobles, "definiciones duplicadas: " + "; ".join(dobles)


@pytest.mark.parametrize("nombre", sorted(fx.cli.commands))
def test_help_de_cada_comando(nombre):
    """Atrapa errores de decorador, de firma y de `--help` sin docstring."""
    r = CliRunner().invoke(fx.cli, [nombre, "--help"])
    assert r.exit_code == 0, r.output
    assert r.output.strip(), f"{nombre} no documenta nada"


@pytest.mark.parametrize("bruto,esperado", [
    ("rediseño web", "rediseno-web"),
    ("Fix: IVA/IIBB", "fix-iva-iibb"),
    ("  guiones   sueltos  ", "guiones-sueltos"),
    ("---", ""),
])
def test_normalizar_slug(bruto, esperado):
    assert fx.normalizar_slug(bruto) == esperado


@pytest.mark.parametrize("msg,vale", [
    ("feat: agregar el gate de check", True),
    ("fix(grafo): usar el ancestro real", True),
    ("wip: seguir-factoria", True),
    ("arreglos varios", False),
    ("feat agregar sin dos puntos", False),
    ("feat: ", False),
])
def test_mensaje_de_commit(msg, vale):
    assert bool(fx.RE_MENSAJE.match(msg)) is vale


def test_mensaje_respeta_la_cota():
    assert fx.COTA_MENSAJE == 60


def test_huella_spec_ignora_lo_que_no_es_spec():
    """Editar el pedido crudo no puede disparar spec-drift."""
    a = fx.Ticket(slug="x", cuerpo="## Pedido crudo\nuno\n\n"
                                   "## Criterios de aceptación\n- [ ] a\n\n"
                                   "## Fuera de alcance\n- nada\n")
    b = fx.Ticket(slug="x", cuerpo="## Pedido crudo\nOTRO TEXTO\n\n"
                                   "## Criterios de aceptación\n- [ ] a\n\n"
                                   "## Fuera de alcance\n- nada\n")
    assert fx.huella_spec(a) == fx.huella_spec(b)
    c = fx.Ticket(slug="x", cuerpo="## Pedido crudo\nuno\n\n"
                                   "## Criterios de aceptación\n- [ ] a\n- [ ] b\n\n"
                                   "## Fuera de alcance\n- nada\n")
    assert fx.huella_spec(a) != fx.huella_spec(c)


def test_las_ramas_prohibidas_no_se_repiten():
    for p in (fx.DATOS / "profiles").glob("*.yml"):
        pr = fx.perfil(p.stem).get("ramas_prohibidas") or []
        assert len(pr) == len(set(pr)), f"{p.stem}: {pr}"


def test_toda_fase_tiene_skills_sin_tracker():
    """Las cuatro que guardan estado en un tracker no vuelven por la ventana."""
    descartadas = {n for n, _ in fx.SKILLS_DESCARTADAS}
    adoptadas = {n for items in fx.SKILLS_POR_FASE.values() for n, _ in items}
    adoptadas |= {n for n, _ in fx.SKILLS_TRANSVERSALES}
    assert not (adoptadas & descartadas)
    assert set(fx.SKILLS_POR_FASE) <= set(fx.FASES)
