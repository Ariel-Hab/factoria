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


def test_lista_vacia_explicita_no_vuelve_al_default():
    """`ramas_prohibidas: []` es una decision del repo de datos, no un hueco.

    Con `or` volvia la tupla por defecto y `commit` se negaba en `main` siempre
    y sin decir nada, o sea el hook Stop no commiteaba nunca.
    """
    pf = fx.perfil(".factoria")
    assert "ramas_prohibidas" in pf
    assert pf["ramas_prohibidas"] == []
    fuente = FUENTE.read_text(encoding="utf-8")
    assert "or RAMAS_PROHIBIDAS" not in fuente


def test_entrada_por_nombre_de_repo_es_exacta():
    """`factoria` no puede matchear la entrada `.factoria`.

    Por substring, `check` corrido en el repo de codigo escribia su evidencia
    en el doc del ticket del repo de datos.
    """
    t = fx.Ticket(slug="x", repos=[fx.RepoTicket(repo=".factoria", cuenta="dfv")])
    assert fx._entrada_unica(t, "factoria").repo == ".factoria"   # comodidad de --repo
    import pytest as _p
    with _p.raises(Exception):
        fx._entrada_unica(t, "factoria", exacto=True)


def test_adoptar_no_huerfana_al_cambiar_de_cuenta():
    """El guard de `adoptar` mira la cuenta VIEJA, no la nueva.

    Los transcripts de dfv y personal son directorios disjuntos, asi que al
    cambiar de cuenta el .jsonl previo nunca esta en la nueva: mirar ahi
    desactiva el guard justo cuando mas hace falta.
    """
    fuente = FUENTE.read_text(encoding="utf-8")
    i = fuente.index("def adoptar_cmd")
    j = fuente.index("@cli.command", i)
    cuerpo = fuente[i:j]
    assert "jsonl_de(previa, cuenta_previa)" in cuerpo
    assert "jsonl_de(previa, cta)" not in cuerpo


def _pasos(rama, base, trabajo_es_worktree=False, pusheada=False, repo="defeve"):
    from pathlib import Path as _P
    rp = _P(r"C:\ariel\dfv") / repo
    trabajo = _P(r"C:\ariel\dfv\wt\x") if trabajo_es_worktree else rp
    e = fx.RepoTicket(repo=repo, cuenta="dfv", rama=rama, cwd=str(trabajo))
    return "\n".join(fx._pasos_manuales("slug", rp, trabajo, e, base, pusheada))


def test_close_no_propone_borrar_la_base():
    """`new --aqui` registra la rama actual, que puede ser la base."""
    txt = _pasos("main", "main", pusheada=True)
    assert "branch -d" not in txt
    assert "--delete" not in txt
    assert "http" not in txt     # ni URL de compare: no hay nada que comparar


def test_close_no_propone_borrar_una_rama_protegida():
    txt = _pasos("desarrollo-ari", "master", pusheada=True)
    assert "branch -d" not in txt and "--delete" not in txt
    assert "protegida" in txt


def test_close_ordena_push_pr_y_recien_despues_el_borrado():
    txt = _pasos("feature/x", "master", trabajo_es_worktree=True)
    i_push = txt.index("push -u origin feature/x")
    i_pr = txt.index("PR")
    i_wt = txt.index("--limpiar-worktree")
    i_del = txt.index("branch -d feature/x")
    assert i_push < i_pr < i_wt < i_del, txt
    # El worktree ANTES del borrado local: con la rama checkouteada ahi,
    # `branch -d` no puede sacarla.
    assert "mergeado" in txt
    assert "-D" not in txt      # nunca forzar


def test_close_sin_worktree_no_menciona_worktree():
    txt = _pasos("fix/y", "master", pusheada=True)
    assert "worktree" not in txt
    assert "branch -d fix/y" in txt


# --------------------------------------------------------------------------
# adoptar: elegir la sesion por evidencia, no por mtime
# --------------------------------------------------------------------------

# Con barras normales a proposito: Path lo normaliza a backslash en los dos
# lados de la comparacion, y asi el literal no depende del escapeo del shell.
CWD = "C:/ariel/dfv/defeve"


def _ses(sid, cuenta, mtime, cwd=CWD, titulo="x"):
    return fx.Sesion(cuenta=cuenta, proyecto="p", session_id=sid, mb=1.0,
                     dias=0, cwd=cwd, mtime=mtime, titulo=titulo)


def _elegir(monkeypatch, sesiones, nombran, yo="", cuenta_ticket="dfv",
            restringida=False):
    monkeypatch.setattr(fx, "inventario_sesiones", lambda: sesiones)
    monkeypatch.setattr(fx, "sesiones_que_nombran", lambda _a: set(nombran))
    monkeypatch.setattr(fx, "sesion_actual", lambda: (yo, "dfv") if yo else None)
    t = fx.Ticket(slug="mi-slug")
    e = fx.RepoTicket(repo="defeve", cuenta=cuenta_ticket, cwd=CWD)
    return fx._elegir_sesion(t, e, cuenta_ticket, restringida=restringida)


def test_adoptar_elige_por_evidencia_y_no_por_mtime(monkeypatch):
    """El bug real: adoptaba la mas reciente del mismo cwd, que era una
    conversacion ajena que solo habia pasado por ese repo."""
    ss = [_ses("la-del-ticket", "dfv", 100), _ses("ajena-reciente", "dfv", 999)]
    sid, cta, motivo = _elegir(monkeypatch, ss, {"la-del-ticket"})
    assert (sid, cta) == ("la-del-ticket", "dfv")
    assert "transcript" in motivo


def test_adoptar_repunta_la_cuenta_a_la_que_tiene_la_sesion(monkeypatch):
    """Buscar en las dos cuentas y dejar escrita la vieja deja el ticket
    apuntando a un uuid que `resume` no encuentra: el bug que arregla."""
    ss = [_ses("alla", "personal", 100), _ses("aca-ajena", "dfv", 999)]
    sid, cta, _ = _elegir(monkeypatch, ss, {"alla"}, cuenta_ticket="dfv")
    assert (sid, cta) == ("alla", "personal")


def test_adoptar_se_niega_si_ninguna_sesion_nombra_el_slug(monkeypatch):
    """Sin evidencia no adivina: lista y pide el uuid o --aqui."""
    ss = [_ses("ajena", "dfv", 999, titulo="otra cosa")]
    with pytest.raises(fx.click.ClickException) as exc:
        _elegir(monkeypatch, ss, set())
    assert "--aqui" in exc.value.message
    assert "ajena" in exc.value.message      # la lista, para poder elegir


def test_adoptar_no_se_adopta_a_si_misma(monkeypatch):
    """Correr `adoptar <slug>` deja el slug en el transcript de la sesion que lo
    corre, asi que seria evidencia de si misma. Para esa esta --aqui."""
    ss = [_ses("yo", "dfv", 999)]
    with pytest.raises(fx.click.ClickException):
        _elegir(monkeypatch, ss, {"yo"}, yo="yo")


def test_adoptar_con_cuenta_explicita_no_mira_la_otra(monkeypatch):
    ss = [_ses("alla", "personal", 999)]
    with pytest.raises(fx.click.ClickException):
        _elegir(monkeypatch, ss, {"alla"}, cuenta_ticket="dfv", restringida=True)


def test_sesion_actual_sale_del_entorno(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(fx.CUENTAS["personal"]))
    assert fx.sesion_actual() == ("abc", "personal")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    assert fx.sesion_actual() is None


def test_sesiones_que_nombran_encuentra_el_slug_partido_por_el_corte(
        monkeypatch, tmp_path):
    """La aguja puede caer a caballo de dos lecturas de 1 MB."""
    proj = tmp_path / "projects" / "p"
    proj.mkdir(parents=True)
    aguja = "mi-slug-largo"
    relleno = b"x" * ((1 << 20) - 5)          # deja 5 bytes de aguja en el 1er trozo
    (proj / "s1.jsonl").write_bytes(relleno + aguja.encode())
    (proj / "s2.jsonl").write_bytes(relleno + b"otra cosa")
    monkeypatch.setattr(fx, "CUENTAS", {"dfv": tmp_path})
    assert fx.sesiones_que_nombran(aguja) == {"s1"}


def test_adoptar_avisa_pero_no_falla_si_la_sesion_es_de_otro_ticket():
    """Aviso y no error: --forzar tambien apaga el guard de huerfanas, asi que
    obligar a --forzar de rutina bypassearia el que importa mas."""
    fuente = FUENTE.read_text(encoding="utf-8")
    i = fuente.index("def adoptar_cmd")
    cuerpo = fuente[i:fuente.index("@cli.command", i)]
    j = cuerpo.index("ajenos = ")
    assert "raise" not in cuerpo[j:cuerpo.index("previa, cuenta_previa", j)]
    # y el no-op tiene que resolverse ANTES del aviso
    assert cuerpo.index("ya apunta a") < j
