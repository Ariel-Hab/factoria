# factoria

Gestor de tickets, sesiones y documentación para el ecosistema DFV. Un archivo
Python, 23 comandos, sin servidor y sin base de datos.

Se invoca `factoria <comando>` desde cualquier parte: hay un `.cmd` y un shim sin
extensión en `~/.local/bin` (bash no usa `PATHEXT`, por eso son dos). Los dos
ejecutan **el archivo que esté checkouteado** en `C:\ariel\integhra\factoria`.

Dos repos, a propósito:

| | Dónde | Qué guarda |
|---|---|---|
| **código** | `C:\ariel\integhra\factoria` | este CLI y sus tests |
| **datos** | `C:\ariel\.factoria` (privado) | tickets, contratos, perfiles, skills, docs |

El CLI llega a los datos por la constante `DATOS`, nunca por copia. Ningún
archivo vive en los dos.

## El ciclo de un ticket

```bash
factoria new rediseno-home --repo defeve      # ticket + rama feature/rediseno-home + sesión
```

Crea el ticket en `fase: plan`, **siempre en una rama nueva** (`<tipo>/<slug>`),
reserva el uuid de la sesión y la lanza. Flags que importan:

- `--aqui` — no crear rama, registrar la actual. Escape hatch explícito.
- `--pedido "..."` — el pedido crudo literal, en vez de abrir el editor.
- `--no-lanzar` — crear el ticket sin abrir la sesión.
- `--tipo fix|chore` — prefijo de la rama (default `feature`).

**Dentro de la sesión** se escriben `## Criterios de aceptación` (cada uno
respondible con sí/no y nombrando *su* evidencia) y `## Supuestos abiertos`. Esa
sección vacía en fase `plan` significa que no se entendió el pedido.

```bash
factoria aprobar rediseno-home                # congela el hash de criterios + fuera de alcance
factoria open rediseno-home --repo Cotizaciones   # segundo repo; se niega si no está aprobado
```

Después, trabajando:

```bash
factoria commit --mensaje "feat: cerrar el modal al anular"   # <tipo>: <imperativo>, ≤60 chars
factoria check --slug rediseno-home           # corre los verify: del perfil y escribe la evidencia
factoria fase rediseno-home dev               # metadata; NO corta la sesión
factoria close rediseno-home                  # pushea, archiva, imprime el compare. NO crea el PR
```

`commit` normalmente lo llama el hook `Stop` del repo (stanza para pegar:
`factoria commit --imprimir-hook`). Valida la forma, protege las ramas del perfil
y saca del stage lo que el perfil marque como protegido.

## Volver a una tarea

```bash
factoria resume rediseno-home        # continúa la conversación; sin argumentos lista lo que hay
factoria adoptar rediseno-home       # registra la sesión que hizo el trabajo de verdad
factoria cortar rediseno-home --fork # corta cuando la sesión pesa, preservando la anterior
factoria contexto rediseno-home      # el pack mínimo para arrancar fresco: ticket + vecinos
```

`adoptar` es para cuando el ticket apunta a un uuid que no está en disco —
`new` lo reserva antes de que la sesión exista, así que si el trabajo pasó por
otra (un fork, una que ya estaba abierta), `resume` abriría una vacía.

## Mirar el estado

```bash
factoria board       # el relevamiento completo + HALLAZGOS. Solo lectura
factoria tickets     # fase, repo, rama, cuenta, sesión, issue, líneas
factoria cotas       # mide las cotas del estándar y SALE CON 1 si alguna se pasó
factoria sesiones --paralelas   # ramas con más de una sesión viva: el agujero de trazabilidad
factoria grafo --que-toca <archivo> | --vecinos <slug> | --bloqueos
factoria abrir <slug>           # abre el issue de GitHub en el navegador
```

## Mantenimiento

| Comando | Para qué |
|---|---|
| `espejo [<slug>] [--todos] [--probar]` | ticket → issue + tarjeta del Project. Una sola vía |
| `skills [<fase>]` | qué skill de mattpocock usar en cada fase, y cuál queda afuera |
| `sync-skills [--desvincular]` | vincula las skills y agents del repo de datos a los dos config dirs |
| `perfiles` | genera un perfil por repo detectando dónde van sus docs |
| `rama <slug> <nueva> [--solo-registro]` | renombra la rama. `--solo-registro` si la rama es ajena |
| `renombrar <slug> <nuevo>` | renombra el ticket, sus docs y sus ramas |
| `check --instalar-pre-push` | pobla el `hooksPath` del repo con un pre-push que corre `check` |

## Las cuatro reglas que explican el resto

1. **La fase no corta la sesión.** Reconstruir contexto cuesta más que seguir. Se
   corta cuando el `.jsonl` pesa (`board` lo dice), no cuando cambia la fase.
2. **Los `.md` son canónicos, GitHub es espejo.** Sin red, todo comando sigue
   funcionando y `board` marca el espejo pendiente. Arrastrar una tarjeta no
   vuelve al archivo: la fase se cambia con `factoria fase`.
3. **Lo determinista y con estado va en código; el juicio sobre contenido, en una
   skill.** Las cotas escritas en un `.md` decayeron todas — por eso `cotas`
   sale con código de error y se cuelga de un `verify:`.
4. **No hay PR automático ni merge.** `close` pushea e imprime el compare. En
   `defeve` el merge a `master` es siempre por PR en Bitbucket.

## Cuando algo no anda

- **`commit` no commitea y no dice nada** → la rama está en `ramas_prohibidas`
  del perfil. Ahora lo avisa; si el repo trabaja legítimamente en `main` (el de
  datos), su perfil declara `ramas_prohibidas: []`.
- **`check` no verifica casi nada** → ese perfil no declara `verify:`. Sin eso es
  un linter de cotas, no un DoD.
- **`resume` abre una sesión vacía** → el ticket apunta a un uuid reservado que
  nunca se abrió. `factoria adoptar <slug>`.
- **Un `cd C:\ruta` en bash no llega** → bash se come los `\`. Usar
  `git -C C:/ruta` o barras normales.

Los tests son el `verify:` de este repo: `python -m pytest tests -q` más
`python -m ruff check --select F --isolated factoria.py`.
