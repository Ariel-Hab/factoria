"""La UI de `factoria panel`, en Textual.

No sabe nada de tickets: recibe el arbol ya armado (`Nodo` de factoria.py) y
las funciones que necesita, y devuelve la accion elegida como tupla. Lanzar la
sesion o cerrar el ticket lo hace factoria despues de que la app termino, asi
Claude no hereda la pantalla a medio limpiar.
"""
from __future__ import annotations

from typing import Callable

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, OptionList, Static, Tree
from textual.widgets.option_list import Option

ESTILO = {"epica": "bold", "hito": "bold cyan", "gate": "dim italic", "ticket": ""}


class Elegir(ModalScreen):
    """Una pregunta con opciones. Devuelve el id de la elegida, o None con Esc."""

    BINDINGS = [Binding("escape", "cancelar", "volver")]
    DEFAULT_CSS = """
    Elegir { align: center middle; }
    Elegir > Vertical { width: 72; height: auto; max-height: 80%;
                        border: round $accent; background: $surface; padding: 1 2; }
    Elegir OptionList { height: auto; max-height: 12; margin-top: 1; }
    """

    def __init__(self, pregunta: str, opciones: list[tuple[str, str]]) -> None:
        super().__init__()
        self.pregunta, self.opciones = pregunta, opciones

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(Text(self.pregunta))
            yield OptionList(*(Option(Text(texto), id=clave)
                               for clave, texto in self.opciones))

    def on_option_list_option_selected(self, ev: OptionList.OptionSelected) -> None:
        self.dismiss(ev.option.id)

    def action_cancelar(self) -> None:
        self.dismiss(None)


class Arbol(Tree):
    BINDINGS = [Binding("right", "expandir", "expandir"),
                Binding("left", "colapsar", "colapsar")]

    def action_expandir(self) -> None:
        if (n := self.cursor_node) and n.allow_expand:
            n.expand()

    def action_colapsar(self) -> None:
        n = self.cursor_node
        if n is None:
            return
        if n.allow_expand and n.is_expanded:
            n.collapse()
        elif n.parent is not None and n.parent is not self.root:
            self.move_cursor(n.parent)


class PanelApp(App):
    TITLE = "factoria panel"
    CSS = """
    #busqueda { display: none; }
    #busqueda.visible { display: block; }
    #arbol { width: 1fr; border: round $primary; }
    #previa-marco { width: 1fr; border: round $secondary; padding: 0 1; }
    """
    BINDINGS = [Binding("enter", "acciones", "acciones", show=True, priority=False),
                Binding("slash", "buscar", "buscar"),
                Binding("c", "cerrados", "cerrados"),
                Binding("escape", "limpiar", "limpiar búsqueda", show=False),
                Binding("q", "quit", "salir")]

    def __init__(self, construir: Callable[[bool], list], filtrar: Callable[[list, str], list],
                 vista_previa: Callable[[object], str],
                 resumen_cierre: Callable[[object, bool], list[str]],
                 cuentas: list[str], cerrados: bool = False) -> None:
        super().__init__()
        self.construir, self.filtrar = construir, filtrar
        self.vista_previa, self.resumen_cierre = vista_previa, resumen_cierre
        self.cuentas, self.con_cerrados = cuentas, cerrados
        self.nodos: list = []

    def compose(self) -> ComposeResult:
        yield Input(placeholder="buscar (Esc limpia)", id="busqueda")
        with Horizontal():
            arbol = Arbol("tickets", id="arbol")
            arbol.show_root = False
            arbol.auto_expand = False
            yield arbol
            with VerticalScroll(id="previa-marco"):
                yield Static(id="previa")
        yield Footer()

    def on_mount(self) -> None:
        self.recargar()
        self.query_one(Arbol).focus()

    # --- arbol ---------------------------------------------------------------

    def recargar(self) -> None:
        self.nodos = self.construir(self.con_cerrados)
        self.pintar()

    def pintar(self) -> None:
        texto = self.query_one("#busqueda", Input).value
        nodos = self.filtrar(self.nodos, texto)
        arbol = self.query_one(Arbol)
        arbol.clear()
        self._agregar(arbol.root, nodos, expandir=bool(texto.strip()))
        self.sub_title = ("abiertos y cerrados" if self.con_cerrados else "abiertos") + (
            f" · '{texto}'" if texto.strip() else "")
        if arbol.root.children:
            arbol.move_cursor(arbol.root.children[0])
        self._previa(arbol.cursor_node)

    def _agregar(self, padre, nodos: list, expandir: bool) -> None:
        for n in nodos:
            estilo = ESTILO.get(n.tipo, "")
            if n.ticket is not None and not n.ticket.abierto:
                estilo += " dim"
            etiqueta = Text(n.etiqueta, style=estilo.strip())
            if n.hijos:
                hoja = padre.add(etiqueta, data=n, expand=expandir)
                self._agregar(hoja, n.hijos, expandir)
            else:
                padre.add_leaf(etiqueta, data=n)

    def _previa(self, nodo_ui) -> None:
        n = nodo_ui.data if nodo_ui is not None else None
        self.query_one("#previa", Static).update(
            Text(self.vista_previa(n)) if n is not None else Text("(sin tickets)"))

    def on_tree_node_highlighted(self, ev: Tree.NodeHighlighted) -> None:
        self._previa(ev.node)

    def on_tree_node_selected(self, ev: Tree.NodeSelected) -> None:
        self.abrir_acciones(ev.node.data)

    # --- busqueda y filtro -----------------------------------------------------

    def action_buscar(self) -> None:
        caja = self.query_one("#busqueda", Input)
        caja.add_class("visible")
        caja.focus()

    def on_input_changed(self, ev: Input.Changed) -> None:
        self.pintar()

    def on_input_submitted(self, ev: Input.Submitted) -> None:
        self.query_one(Arbol).focus()

    def action_limpiar(self) -> None:
        caja = self.query_one("#busqueda", Input)
        caja.value = ""
        caja.remove_class("visible")
        self.query_one(Arbol).focus()

    def action_cerrados(self) -> None:
        if self.focused is self.query_one("#busqueda", Input):
            return
        self.con_cerrados = not self.con_cerrados
        self.recargar()

    # --- acciones ------------------------------------------------------------

    def action_acciones(self) -> None:
        n = self.query_one(Arbol).cursor_node
        if n is not None:
            self.abrir_acciones(n.data)

    def abrir_acciones(self, n) -> None:
        if n is None or n.ticket is None:
            self.notify("Un hito en gate no tiene ticket: no hay acciones.")
            return
        t = n.ticket
        if not t.repos:
            self.notify(f"{t.slug} no tiene repos registrados.", severity="warning")
            return
        opciones = [("resume", "resume — reanuda la sesión responsable"),
                    ("nueva", "nueva sesión — arranca limpia con el pack, en la cuenta que elijas")]
        if t.abierto:
            opciones.append(("cerrar", "cerrar — con o sin push, con confirmación"))

        def elegida(accion: str | None) -> None:
            if accion == "resume":
                self._con_repo(t, lambda r: self.exit(("resume", t.slug, r)))
            elif accion == "nueva":
                self._con_repo(t, lambda r: self._nueva(t, r))
            elif accion == "cerrar":
                self._cerrar(t)

        self.push_screen(Elegir(f"{t.slug}: ¿qué hacemos?", opciones), elegida)

    def _con_repo(self, t, seguir: Callable[[str], None]) -> None:
        if len(t.repos) == 1:
            seguir(t.repos[0].repo)
            return
        self.push_screen(
            Elegir("¿En qué repo?", [(e.repo, f"{e.repo} ({e.cuenta}) {e.rama}")
                                     for e in t.repos]),
            lambda r: r and seguir(r))

    def _nueva(self, t, repo: str) -> None:
        actual = next((e.cuenta for e in t.repos if e.repo == repo), "")
        opciones = [(c, c + ("  (la de la responsable)" if c == actual else ""))
                    for c in self.cuentas]
        self.push_screen(Elegir(f"{t.slug}: ¿con qué cuenta?", opciones),
                         lambda c: c and self.exit(("nueva", t.slug, repo, c)))

    def _cerrar(self, t) -> None:
        def con_push(eleccion: str | None) -> None:
            if eleccion is None:
                return
            pushear = eleccion == "con"
            resumen = "\n".join(self.resumen_cierre(t, pushear))
            self.push_screen(
                Elegir(resumen, [("si", "Sí, cerrar"), ("no", "No")]),
                lambda r: r == "si" and self.exit(("cerrar", t.slug, pushear)))

        self.push_screen(Elegir(f"Cerrar {t.slug}: ¿con o sin push?",
                                [("con", "con push"), ("sin", "sin push")]), con_push)


def correr(construir, filtrar, vista_previa, resumen_cierre, cuentas: list[str],
           cerrados: bool = False) -> tuple | None:
    return PanelApp(construir, filtrar, vista_previa, resumen_cierre, cuentas,
                    cerrados).run()
