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


def test_el_repo_prefijo_se_puede_elegir_cuando_conviven():
    """Con `factoria` y `.factoria` en el MISMO ticket, el de codigo era
    inseleccionable: su nombre es prefijo del otro, el substring matcheaba las
    dos y `adoptar`/`resume` fallaban siempre con "tiene 2 repos". No habia
    texto que lo eligiera.
    """
    t = fx.Ticket(slug="x", repos=[
        fx.RepoTicket(repo="factoria", cuenta="personal"),
        fx.RepoTicket(repo=".factoria", cuenta="dfv"),
    ])
    assert fx._entrada_unica(t, "factoria").repo == "factoria"
    assert fx._entrada_unica(t, ".factoria").repo == ".factoria"


def test_adoptar_ya_no_necesita_guard_de_huerfanas():
    """El guard viejo exigia --forzar para reemplazar una sesion que existiera en
    disco, o sea en el caso normal, y eso era lo que volvia inusable al comando.
    Con el historial no hay nada que proteger: la anterior queda asociada.
    """
    fuente = FUENTE.read_text(encoding="utf-8")
    i = fuente.index("def adoptar_cmd")
    cuerpo = fuente[i:fuente.index("@cli.command", i)]
    assert "jsonl_de(previa" not in cuerpo
    assert "deja sin ticket que la encuentre" not in cuerpo
    assert "e.asociar(" in cuerpo, "la responsable se mueve con asociar()"


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
# adoptar: una sola sesion responsable, y ninguna se pierde
# --------------------------------------------------------------------------

# Con barras normales a proposito: Path lo normaliza a backslash en los dos
# lados de la comparacion, y asi el literal no depende del escapeo del shell.
CWD = "C:/ariel/dfv/defeve"


def _ses(sid, cuenta, mtime, cwd=CWD, titulo="x"):
    return fx.Sesion(cuenta=cuenta, proyecto="p", session_id=sid, mb=1.0,
                     dias=0, cwd=cwd, mtime=mtime, titulo=titulo)


def _ticket_con(sid="vieja", cuenta="dfv"):
    e = fx.RepoTicket(repo="defeve", cuenta=cuenta, rama="feature/x", cwd=CWD,
                      session_id=sid,
                      sesiones=[fx.SesionTicket(sid, cuenta, "2026-09-01")])
    return fx.Ticket(slug="mi-slug", repos=[e]), e


def _adoptar(monkeypatch, tmp_path, args, t, aqui=("nueva", "personal"),
             cuenta_de="personal"):
    monkeypatch.setattr(fx, "buscar_ticket", lambda _s: t)
    monkeypatch.setattr(fx, "tickets_todos", lambda: [t])
    monkeypatch.setattr(fx, "escribir_ticket", lambda _t: None)
    monkeypatch.setattr(fx, "sesion_actual", lambda: aqui)
    monkeypatch.setattr(fx, "jsonl_de", lambda sid, cta: None)
    monkeypatch.setattr(fx, "_cuenta_de", lambda sid: cuenta_de if sid else None)
    monkeypatch.setattr(fx, "CONTEXTOS", tmp_path / "contexto")
    monkeypatch.setattr(fx, "pack_de_contexto", lambda _s, *a, **k: "# pack")
    return CliRunner().invoke(fx.cli, ["adoptar", *args])


def test_adoptar_aqui_no_pierde_la_sesion_anterior(monkeypatch, tmp_path):
    """El bug que motivo todo esto: cambiar de responsable borraba la referencia
    a la anterior, y no habia forma de volver ni de saber por donde paso."""
    t, e = _ticket_con()
    r = _adoptar(monkeypatch, tmp_path, ["mi-slug", "--aqui"], t)
    assert r.exit_code == 0, r.output
    assert (e.session_id, e.cuenta) == ("nueva", "personal")
    assert [s.id for s in e.sesiones] == ["vieja", "nueva"]
    assert [s.id for s in e.otras()] == ["vieja"]      # una sola responsable
    assert "vieja" in r.output                         # y se ve, no queda oculta


def test_adoptar_sin_decir_quien_no_adivina(monkeypatch, tmp_path):
    """Sin modo no hay default razonable: adoptar cambia quien manda."""
    t, _ = _ticket_con()
    r = _adoptar(monkeypatch, tmp_path, ["mi-slug"], t)
    assert r.exit_code != 0
    assert "--cuenta personal" in r.output     # la otra cuenta, ya tipeada
    assert "--aqui" in r.output
    assert "resume mi-slug --nueva" in r.output  # el caso que NO es adoptar


def test_adoptar_cuenta_abre_una_sesion_nueva_alla(monkeypatch, tmp_path):
    """--cuenta es el caso principal: la otra cuenta agarra el ticket, y como
    los transcripts son disjuntos eso solo puede ser una sesion nueva."""
    t, e = _ticket_con()
    vistas = []
    monkeypatch.setattr(fx, "_arrancar_fresca",
                        lambda tk, ent, cta, imp, frz: vistas.append(cta))
    r = _adoptar(monkeypatch, tmp_path, ["mi-slug", "--cuenta", "personal"], t)
    assert r.exit_code == 0, r.output
    assert vistas == ["personal"]


def test_adoptar_rechaza_un_uuid_que_no_existe(monkeypatch, tmp_path):
    """Adoptar un id que no esta en disco reproduce el fantasma que el comando
    viene a arreglar: `resume` abre una sesion vacia."""
    t, _ = _ticket_con()
    r = _adoptar(monkeypatch, tmp_path, ["mi-slug", "no-existe"], t, cuenta_de=None)
    assert r.exit_code != 0
    assert "--cuenta" in r.output      # la salida: abrirla en vez de fingirla


def test_adoptar_no_acepta_dos_modos_juntos(monkeypatch, tmp_path):
    t, _ = _ticket_con()
    r = _adoptar(monkeypatch, tmp_path, ["mi-slug", "--aqui", "--cuenta", "dfv"], t)
    assert r.exit_code != 0
    assert "uno solo" in r.output


def test_readoptar_la_misma_sesion_no_duplica_ni_repisa_la_fecha():
    _, e = _ticket_con()
    e.asociar("vieja", "dfv")
    assert len(e.sesiones) == 1
    assert e.sesiones[0].desde == "2026-09-01"


def test_volver_a_la_anterior_es_adoptarla_de_nuevo():
    """Que la vuelta sea el mismo comando es todo el punto: antes la referencia
    no estaba en ningun lado."""
    _, e = _ticket_con()
    e.asociar("nueva", "personal")
    e.asociar("vieja", "dfv")
    assert (e.session_id, e.cuenta) == ("vieja", "dfv")
    assert [s.id for s in e.sesiones] == ["vieja", "nueva"]
    assert len(e.sesiones) == 2      # ni duplicados ni perdidas


def test_una_sesion_menor_no_le_saca_la_posta_a_la_responsable():
    """Sesiones menores asociadas al ticket sin que manden: el `responsable=False`
    existe para eso."""
    _, e = _ticket_con()
    e.asociar("menor", "dfv", responsable=False)
    assert e.session_id == "vieja"
    assert [s.id for s in e.otras()] == ["menor"]


def test_el_historial_sobrevive_al_ida_y_vuelta_del_yaml(tmp_path):
    t, e = _ticket_con()
    e.asociar("nueva", "personal")
    t.path = tmp_path / "mi-slug.md"
    t.cuerpo = "## Pedido crudo\nalgo\n"
    fx.escribir_ticket(t)
    leido = fx.leer_ticket(t.path)
    e2 = leido.repos[0]
    assert e2.session_id == "nueva" and e2.cuenta == "personal"
    assert [(s.id, s.cuenta) for s in e2.sesiones] == [
        ("vieja", "dfv"), ("nueva", "personal")]
    # una linea por sesion: el ticket tiene una cota de 120 y el historial no
    # puede comerse el presupuesto del contenido
    assert "\n  - vieja " in t.texto() or "\n    - vieja " in t.texto()


def test_un_ticket_viejo_estrena_historial_con_la_sesion_que_tenia(tmp_path):
    """Los tickets anteriores a esto solo tienen `session_id`. Si al leerlos no
    se sembrara, la primera adopcion perderia la unica sesion conocida."""
    p = tmp_path / "viejo.md"
    p.write_text("---\nslug: viejo\nrepos:\n- repo: defeve\n  cuenta: dfv\n"
                 "  session_id: la-unica\n---\n\ncuerpo\n", encoding="utf-8")
    e = fx.leer_ticket(p).repos[0]
    assert [(s.id, s.cuenta) for s in e.sesiones] == [("la-unica", "dfv")]


def test_una_sesion_mal_escrita_a_mano_no_tumba_el_ticket():
    """El ticket es un .md que se edita: negarse a leerlo perderia el historial."""
    assert fx.SesionTicket.leer("") is None
    assert fx.SesionTicket.leer("solo-el-uuid") == fx.SesionTicket("solo-el-uuid", "")
    assert fx.SesionTicket.leer({"id": "u", "cuenta": "dfv"}).cuenta == "dfv"


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
    """Aviso y no error: un ticket que nace desde la sesion de otro es
    legitimo, y convertirlo en error obligaria a un flag de rutina."""
    fuente = FUENTE.read_text(encoding="utf-8")
    i = fuente.index("def adoptar_cmd")
    cuerpo = fuente[i:fuente.index("@cli.command", i)]
    j = cuerpo.index("ajenos = ")
    assert "raise" not in cuerpo[j:cuerpo.index("e.asociar(", j)]
    # y el no-op tiene que resolverse ANTES del aviso
    assert cuerpo.index("ya apunta a") < j


# --------------------------------------------------------------------------
# resume --nueva: sesion nueva registrada, con el pack inyectado
# --------------------------------------------------------------------------

def _fresca(monkeypatch, tmp_path, lanzo, imprimir=False):
    t = fx.Ticket(slug="mi-slug",
                  repos=[fx.RepoTicket(repo="defeve", cuenta="personal",
                                       cwd=CWD, session_id="vieja")])
    monkeypatch.setattr(fx, "CONTEXTOS", tmp_path / "contexto")
    monkeypatch.setattr(fx, "pack_de_contexto", lambda _s: "# pack")
    monkeypatch.setattr(fx, "escribir_ticket", lambda _t: None)
    vistos = []

    def falso_lanzar(cta, cwd, args, forzar, impr, nota=""):
        vistos.append((cta, cwd, args))
        return lanzo

    monkeypatch.setattr(fx, "_lanzar_en", falso_lanzar)
    fx._arrancar_fresca(t, t.repos[0], "dfv", imprimir, False)
    return t.repos[0], vistos


def test_sesion_fresca_queda_registrada_y_recibe_el_pack(monkeypatch, tmp_path):
    e, vistos = _fresca(monkeypatch, tmp_path, lanzo=True)
    assert e.cuenta == "dfv"                    # la cuenta pedida, no la del ticket
    assert e.session_id not in ("", "vieja")
    cta, _cwd, args = vistos[0]
    assert cta == "dfv"
    assert args[:2] == ["--session-id", e.session_id]
    # el prompt inicial va como UN argumento, con el slug y la ruta del pack
    assert len(args) == 3 and "mi-slug" in args[2]


def test_sesion_fresca_se_revierte_si_no_se_lanzo(monkeypatch, tmp_path):
    """Adentro de otra sesion `_lanzar_en` imprime la receta y no lanza. Dejar
    el uuid escrito ahi seria el fantasma que `adoptar` viene a arreglar."""
    e, _ = _fresca(monkeypatch, tmp_path, lanzo=False)
    assert (e.session_id, e.cuenta) == ("vieja", "personal")


def test_sesion_fresca_con_imprimir_no_toca_el_ticket(monkeypatch, tmp_path):
    e, vistos = _fresca(monkeypatch, tmp_path, lanzo=False, imprimir=True)
    assert (e.session_id, e.cuenta) == ("vieja", "personal")
    assert vistos, "igual tiene que mostrar la receta"


def test_lanzar_avisa_cuando_no_lanzo():
    """Quien registra un uuid antes de lanzar necesita saber si se lanzo."""
    assert fx._lanzar_en("dfv", CWD, ["-r", "x"], False, True) is False


def test_el_pack_de_contexto_es_reutilizable_como_funcion():
    """`resume --nueva` lo inyecta como primer prompt: no alcanza con imprimirlo."""
    import inspect
    assert callable(fx.pack_de_contexto)
    # el decorador de click envuelve la funcion: la real es .callback
    assert "pack_de_contexto" in inspect.getsource(fx.contexto_cmd.callback)


def test_resume_en_la_cuenta_que_no_tiene_sesion_apunta_a_adoptar():
    """El error tiene que llevar a algun lado: era un callejon sin salida. Y el
    lugar al que lleva es `adoptar`, que es el comando de cambiar de cuenta."""
    fuente = FUENTE.read_text(encoding="utf-8")
    i = fuente.index("no tiene entrada para esos filtros")
    assert "factoria adoptar {t.slug} --cuenta {cuenta}" in fuente[i:i + 700]


def test_un_ticket_cerrado_no_reporta_spec_drift():
    """`close` archiva el cuerpo y deja un puntero, asi que la huella cambia por
    diseño. Reportar deriva ahi es acusar al propio `close`."""
    t = fx.Ticket(slug="x", abierto=False, spec_congelado="deadbeef",
                  cuerpo="## Estado actual\nCerrado, el cuerpo esta en historial/.\n")
    assert fx._estado_spec(t) == "aprobado"
    t.abierto = True
    assert fx._estado_spec(t) == "deriva"
    # y el hallazgo de board tiene que filtrar igual
    assert "if t.abierto and t.spec_congelado" in FUENTE.read_text(encoding="utf-8")


def test_el_umbral_esta_calibrado_y_no_estimado():
    """El numero puede cambiar; lo que no puede es volver a ser una estimacion
    sin la regla de payback que salio de la medicion."""
    assert fx.UMBRAL_SESION_MB == 1.0
    assert fx.TURNOS_PARA_QUE_CONVENGA == 3


def test_tildar_un_criterio_no_es_spec_drift():
    """Se aprueba al final de `plan` con todo en `[ ]`, y cada `[x]` de dev/test
    disparaba deriva: el gate acusaba a quien lo estaba cumpliendo."""
    base = ("## Criterios de aceptación\n- [ ] uno\n- [ ] dos\n\n"
            "## Fuera de alcance\n- nada\n")
    a = fx.Ticket(slug="x", cuerpo=base)
    b = fx.Ticket(slug="x", cuerpo=base.replace("- [ ] uno", "- [x] uno"))
    assert fx.huella_spec(a) == fx.huella_spec(b)
    # pero editar el TEXTO de un criterio si tiene que saltar
    c = fx.Ticket(slug="x", cuerpo=base.replace("- [ ] uno", "- [ ] uno y medio"))
    assert fx.huella_spec(a) != fx.huella_spec(c)


def test_toda_sesion_arranca_con_el_repo_de_datos_habilitado():
    """El ticket y su doc viven afuera de todo repo de codigo: sin --add-dir la
    primera lectura de cualquier sesion se para en un pedido de permiso."""
    import inspect
    src = inspect.getsource(fx._lanzar_en)
    assert '"--add-dir", str(DATOS)' in src
    i, j = src.index("--add-dir"), src.index("list2cmdline")
    assert i < j, "tiene que entrar en args ANTES de armar la receta"


# --------------------------------------------------------------------------
# Entregables de código: el QUE se toca, declarado antes de que exista
# --------------------------------------------------------------------------

def test_los_entregables_no_entran_en_la_huella():
    """Es el DONDE, no el que. Descubrir superficie nueva en `dev` pasa casi
    siempre; si eso pintara spec-drift el gate seria ruido y se ignoraria, que
    es exactamente lo que le paso al checkbox."""
    base = ("## Criterios de aceptación\n- [ ] uno\n\n"
            "## Fuera de alcance\n- nada\n\n"
            "## Entregables de código\n- **nuevo** servicio `AuthService`\n")
    a = fx.Ticket(slug="x", cuerpo=base)
    b = fx.Ticket(slug="x", cuerpo=base + "- **nueva** tabla `usuario_sesion`\n")
    assert fx.huella_spec(a) == fx.huella_spec(b)


def test_la_plantilla_trae_todo_lo_que_aprobar_exige():
    """Si `new` escribiera un ticket al que le falta un encabezado exigido,
    ningun ticket nuevo se podria aprobar sin pegarlo a mano."""
    for enc in fx.EXIGIDAS_PARA_APROBAR:
        assert enc in fx.CUERPO_TICKET, enc


def test_aprobar_exige_los_entregables_de_codigo(monkeypatch):
    """Criterios y fuera de alcance ya bloqueaban; entregables tambien, porque
    sin el la seccion es un renglon opcional que se llena de compromiso."""
    t = fx.Ticket(slug="x", cuerpo=("## Criterios de aceptación\n- [ ] uno\n\n"
                                    "## Fuera de alcance\n- nada\n\n"
                                    "## Entregables de código\n\n-\n"))
    monkeypatch.setattr(fx, "buscar_ticket", lambda _s: t)
    r = CliRunner().invoke(fx.cli, ["aprobar", "x"])
    assert r.exit_code != 0
    assert "Entregables de código" in r.output
    assert not t.spec_congelado


def test_aprobar_distingue_seccion_vacia_de_ausente(monkeypatch):
    """Un ticket anterior a la plantilla no tiene el encabezado, y decirle
    'esta vacia' lo manda a buscar algo que no esta: una se llena, la otra se
    pega."""
    viejo = fx.Ticket(slug="x", cuerpo=("## Criterios de aceptación\n- [ ] uno\n\n"
                                        "## Fuera de alcance\n- nada\n"))
    monkeypatch.setattr(fx, "buscar_ticket", lambda _s: viejo)
    r = CliRunner().invoke(fx.cli, ["aprobar", "x"])
    assert "ausentes" in r.output and "vacias" not in r.output

    vacio = fx.Ticket(slug="x", cuerpo=viejo.cuerpo + "\n## Entregables de código\n\n-\n")
    monkeypatch.setattr(fx, "buscar_ticket", lambda _s: vacio)
    r = CliRunner().invoke(fx.cli, ["aprobar", "x"])
    assert "vacias" in r.output and "ausentes" not in r.output


def test_el_issue_espeja_los_entregables():
    """El issue se regenera entero: una seccion que no este en la lista
    desaparece del espejo sin que nada falle."""
    t = fx.Ticket(slug="x", cuerpo=("## Entregables de código\n"
                                    "- **nuevo** servicio `AuthService`\n"))
    assert "AuthService" in fx.cuerpo_issue(t)


@pytest.mark.parametrize("cita,tema", [
    (r"C:\ariel\dfv\.contracts\ingesta.md", "ingesta"),
    ("C:/ariel/dfv/.contracts/ingesta.md", "ingesta"),
    (r".contracts\referencia\ingesta.md", "ingesta"),
    (r".contracts\referencia\ingesta\01-ventas.md", "ingesta"),
    (".contracts/referencia/ingesta/01-ventas.md", "ingesta"),
    (r".contracts\historial\ingesta.md", "ingesta"),
    # La forma vault-relativa: `contratos/` es el nombre real del directorio
    # adentro del vault, que es como queda un link de Obsidian.
    ("contratos/ingesta.md", "ingesta"),
    ("../contratos/ingesta.md", "ingesta"),
    ("contratos/referencia/ingesta/01-ventas.md", "ingesta"),
    ("../../contratos/historial/ingesta.md", "ingesta"),
])
def test_referencia_partida_refiere_al_contrato_padre(cita, tema):
    r"""Partir un contrato en carpeta no puede sacarlo del grafo.

    Antes solo matcheaba el .md colgado directo de .contracts\, asi que un doc
    que citaba una ficha de `referencia\<tema>\` no generaba ninguna arista
    `refiere`: aplicar el estandar penalizaba en silencio al que lo aplicaba.
    """
    assert fx.RE_REF_CONTRATO.findall(cita) == [tema]


def test_cotas_referencia_mide_por_ficha_e_incluye_el_readme(tmp_path, monkeypatch):
    """El techo es por ficha; el indice de la carpeta cuenta como una mas."""
    carpeta = tmp_path / "referencia" / "tema"
    carpeta.mkdir(parents=True)
    (carpeta / "README.md").write_text("indice\n" * 10, encoding="utf-8")
    (carpeta / "01-larga.md").write_text(
        "linea\n" * (fx.COTA_REFERENCIA + 1), encoding="utf-8")
    monkeypatch.setattr(fx, "CONTRATOS", tmp_path)

    medidos = dict(fx.cotas_referencia())
    assert medidos[str(Path("tema") / "README.md")] == 10
    assert medidos[str(Path("tema") / "01-larga.md")] == fx.COTA_REFERENCIA + 1

    r = CliRunner().invoke(
        fx.cli, ["cotas", "--no-contratos", "--no-tickets", "--no-docs"])
    assert r.exit_code == 1, r.output
    assert "01-larga.md" in r.output


@pytest.mark.parametrize("ref", [
    r"C:\ariel\.factoria\contratos\tema.md",
    r"C:\Ariel\.factoria\contratos\tema.md",      # el casing convive en el corpus
    "C:/ariel/.factoria/contratos/tema.md",
    r"C:\ariel\dfv\.contracts\tema.md",           # el symlink, mismo archivo
])
def test_destino_en_vault_normaliza_casing_y_symlink(ref):
    """`C:\\Ariel\\...` y `C:\\ariel\\...` abren lo mismo en Windows pero como
    texto son dos nodos distintos, y `.contracts\\` es un symlink a `contratos\\`.
    Los cuatro tienen que caer en el mismo archivo."""
    assert fx._destino_en_vault(ref) == fx.DATOS / "contratos" / "tema.md"


def test_destino_fuera_del_vault_no_se_traduce():
    """Una raiz de repo es una ubicacion, no una nota."""
    assert fx._destino_en_vault(r"C:\ariel\dfv\defeve") is None
    assert fx._destino_en_vault(r"C:\ariel\integhra\factoria\factoria.py") is None


def test_el_frontmatter_no_se_migra_nunca():
    """`doc:` y `cwd:` son paths que lee el codigo, no prosa.

    Convertirlos en link markdown deja al ticket sin poder encontrar su propio
    doc de trabajo. En la primera corrida real, 16 de 19 referencias detectadas
    eran campos del frontmatter.
    """
    front = "---\nslug: x\ndoc: C:" + chr(92) + "ariel" + chr(92) + ".factoria" + chr(92) + "docs" + chr(92) + "x.md\n---\n"
    cuerpo = "ver el doc.\n"
    f, c = fx._cuerpo_sin_frontmatter(front + cuerpo)
    assert f == front and c == cuerpo
    assert "doc:" not in c


def test_links_es_idempotente_y_sale_con_1_si_queda_algo(tmp_path, monkeypatch):
    """Segunda corrida: nada que hacer, y el gate pasa."""
    vault = tmp_path / "vault"
    (vault / "contratos").mkdir(parents=True)
    (vault / "tickets").mkdir()
    (vault / "contratos" / "tema.md").write_text("# tema\n", encoding="utf-8")
    t = vault / "tickets" / "t.md"
    t.write_text(f"ver `{vault}" + chr(92) + "contratos" + chr(92)
                 + "tema.md` ahi.\n", encoding="utf-8")
    monkeypatch.setattr(fx, "ARIEL", tmp_path)
    monkeypatch.setattr(fx, "DATOS", vault)
    monkeypatch.setattr(fx, "CONTRATOS", vault / "contratos")

    assert len(fx.links_del_vault()) == 1
    assert CliRunner().invoke(fx.cli, ["links"]).exit_code == 1
    assert CliRunner().invoke(fx.cli, ["links", "--migrar"]).exit_code == 0
    assert "(../contratos/tema.md)" in t.read_text(encoding="utf-8")
    assert fx.links_del_vault() == []
    assert CliRunner().invoke(fx.cli, ["links"]).exit_code == 0


def test_links_no_toca_skills_ni_agents(tmp_path, monkeypatch):
    """Se cargan desde la sesion de cualquier repo: ahi un path relativo al
    vault no apunta a ningun lado, y el absoluto es la forma correcta."""
    vault = tmp_path / "vault"
    (vault / "contratos").mkdir(parents=True)
    (vault / "skills" / "handoff").mkdir(parents=True)
    (vault / "contratos" / "tema.md").write_text("# tema\n", encoding="utf-8")
    ref = f"{vault}" + chr(92) + "contratos" + chr(92) + "tema.md"
    (vault / "skills" / "handoff" / "SKILL.md").write_text(
        "usar siempre " + ref + "\n", encoding="utf-8")
    monkeypatch.setattr(fx, "ARIEL", tmp_path)
    monkeypatch.setattr(fx, "DATOS", vault)
    monkeypatch.setattr(fx, "CONTRATOS", vault / "contratos")
    assert fx.links_del_vault() == []


# --------------------------------------------------------------------------
# tag-por-ticket: tipo (feature|fix|chore) por ticket
# --------------------------------------------------------------------------

def test_new_persiste_el_tipo(monkeypatch, tmp_path):
    """`--tipo` no era mas que el prefijo de la rama: quedaba invisible en
    cuanto la rama se renombraba o el ticket se miraba desde `tickets.base`."""
    datos = tmp_path / "datos"
    (datos / "tickets").mkdir(parents=True)
    monkeypatch.setattr(fx, "DATOS", datos)
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(fx, "ruta_repo", lambda _n: repo)
    monkeypatch.setattr(fx, "perfil", lambda _n: {"cuenta": "dfv", "base": "main"})
    monkeypatch.setattr(fx, "git", lambda *a, **k: "main")
    monkeypatch.setattr(fx, "_resolver_rama", lambda *a, **k: "fix/x")
    monkeypatch.setattr(fx, "regenerar_indice", lambda: None)

    r = CliRunner().invoke(fx.cli, ["new", "x", "--repo", "repo", "--pedido", "algo",
                                    "--tipo", "fix", "--no-lanzar"])
    assert r.exit_code == 0, r.output
    t = fx.leer_ticket(datos / "tickets" / "x.md")
    assert t.tipo == "fix"


def test_el_tipo_se_infiere_de_la_rama(tmp_path):
    """Los tickets anteriores a este campo no tienen `tipo:`: se infiere del
    prefijo de la primera rama que lo diga, y `feature` si ninguna lo dice."""
    con_rama = tmp_path / "con-rama.md"
    con_rama.write_text(
        "---\nslug: con-rama\nrepos:\n- repo: defeve\n  cuenta: dfv\n"
        "  rama: fix/algo\n---\n\ncuerpo\n", encoding="utf-8")
    assert fx.leer_ticket(con_rama).tipo == "fix"

    sin_prefijo = tmp_path / "sin-prefijo.md"
    sin_prefijo.write_text(
        "---\nslug: sin-prefijo\nrepos:\n- repo: defeve\n  cuenta: dfv\n"
        "  rama: desarrollo-ari\n---\n\ncuerpo\n", encoding="utf-8")
    assert fx.leer_ticket(sin_prefijo).tipo == "feature"

    explicito = tmp_path / "explicito.md"
    explicito.write_text(
        "---\nslug: explicito\ntipo: chore\nrepos:\n- repo: defeve\n"
        "  cuenta: dfv\n  rama: fix/algo\n---\n\ncuerpo\n", encoding="utf-8")
    assert fx.leer_ticket(explicito).tipo == "chore"


def test_tickets_filtra_por_tipo(monkeypatch):
    ts = [
        fx.Ticket(slug="aaa-feature", tipo="feature",
                  repos=[fx.RepoTicket(repo="defeve", cuenta="dfv")]),
        fx.Ticket(slug="bbb-fix", tipo="fix",
                  repos=[fx.RepoTicket(repo="defeve", cuenta="dfv")]),
    ]
    monkeypatch.setattr(fx, "tickets_todos", lambda: ts)
    r = CliRunner().invoke(fx.cli, ["tickets", "--tipo", "fix"])
    assert r.exit_code == 0, r.output
    assert "bbb-fix" in r.output
    assert "aaa-feature" not in r.output

    r_todos = CliRunner().invoke(fx.cli, ["tickets"])
    assert "aaa-feature" in r_todos.output and "bbb-fix" in r_todos.output


def test_tipo_cmd_cambia_y_persiste(monkeypatch):
    t = fx.Ticket(slug="x", tipo="feature",
                  repos=[fx.RepoTicket(repo="defeve", cuenta="dfv")])
    monkeypatch.setattr(fx, "buscar_ticket", lambda _s: t)
    escritos = []
    monkeypatch.setattr(fx, "escribir_ticket", lambda tk: escritos.append(tk.tipo))
    monkeypatch.setattr(fx, "regenerar_indice", lambda: None)
    monkeypatch.setattr(fx, "espejar_si_se_puede", lambda tk: None)

    r = CliRunner().invoke(fx.cli, ["tipo", "x", "chore"])
    assert r.exit_code == 0, r.output
    assert t.tipo == "chore"
    assert escritos == ["chore"]

    # sin cambio: no reescribe
    r2 = CliRunner().invoke(fx.cli, ["tipo", "x", "chore"])
    assert r2.exit_code == 0, r2.output
    assert escritos == ["chore"]


def test_el_issue_lleva_la_label_del_tipo():
    t = fx.Ticket(slug="x", tipo="fix",
                  repos=[fx.RepoTicket(repo="defeve", cuenta="dfv")])
    assert "tipo:fix" in fx._labels_del_ticket(t, {})
    # prefijo configurable en github.yml, igual que repo: y cuenta:
    labels = fx._labels_del_ticket(t, {"labels": {"prefijo_tipo": "kind:"}})
    assert "kind:fix" in labels and "tipo:fix" not in labels


def test_board_ordena_por_jerarquia(monkeypatch):
    """Dentro de la misma fase: feature antes que fix, fix antes que chore."""
    ts = [
        fx.Ticket(slug="c-chore", fase="dev", tipo="chore", abierto=True),
        fx.Ticket(slug="a-feature", fase="dev", tipo="feature", abierto=True),
        fx.Ticket(slug="b-fix", fase="dev", tipo="fix", abierto=True),
    ]
    monkeypatch.setattr(fx, "relevar", lambda: fx.Relevamiento(tickets=ts))
    monkeypatch.setattr(fx, "regenerar_indice", lambda: None)

    r = CliRunner().invoke(
        fx.cli, ["board", "--no-docs", "--no-ramas", "--no-sesiones"])
    assert r.exit_code == 0, r.output
    i_feature = r.output.index("a-feature")
    i_fix = r.output.index("b-fix")
    i_chore = r.output.index("c-chore")
    assert i_feature < i_fix < i_chore, r.output


def test_el_indice_y_la_ficha_muestran_el_tipo(tmp_path, monkeypatch):
    datos = tmp_path / "datos"
    datos.mkdir()
    monkeypatch.setattr(fx, "DATOS", datos)
    monkeypatch.setattr(fx, "descubrir_repos", lambda: [])
    t = fx.Ticket(slug="x", fase="dev", tipo="fix",
                  repos=[fx.RepoTicket(repo="defeve", cuenta="dfv")])
    monkeypatch.setattr(fx, "tickets_todos", lambda: [t])

    fx.regenerar_indice()
    indice = (datos / "INDICE.md").read_text(encoding="utf-8")
    assert "| tipo |" in indice
    assert "| fix |" in indice

    ficha = (datos / "repos" / "defeve.md").read_text(encoding="utf-8")
    assert "fix" in ficha
