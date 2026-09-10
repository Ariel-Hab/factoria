# factoria

Gestor de tickets, sesiones y documentación para el ecosistema DFV. Un archivo
Python, 23 comandos, sin servidor y sin base de datos.

```mermaid
flowchart LR
    A["factoria new<br/>rediseno-home"]
    B["ticket .md<br/><i>la fuente de verdad</i>"]
    C["rama<br/>feature/rediseno-home"]
    D["sesión de Claude<br/><i>uuid conocido</i>"]
    E["doc de trabajo"]
    F["issue + tarjeta<br/>en el tablero"]
    A --> B & C & D & E & F
    style A fill:#238636,color:#fff
    style B fill:#1f6feb,color:#fff
```

Un comando y ya tenés las cinco cosas atadas entre sí. Después todo se hace por
el slug: `factoria resume rediseno-home` te devuelve **la misma conversación**,
en la cuenta y el directorio correctos.

## Los primeros 5 minutos

```bash
factoria board                    # ¿qué hay? Solo lectura, no puede romper nada
factoria tickets                  # los tickets en vuelo
factoria new probar --repo defeve --pedido "lo que te pidieron, literal"
#   ...escribís criterios y supuestos en la sesión que se abrió...
factoria commit --mensaje "feat: primer cambio"
factoria check --slug probar      # corre los gates y escribe la evidencia
factoria close probar             # pushea, archiva, imprime el compare
```

## El ciclo completo

```mermaid
flowchart TD
    N["<b>new</b><br/>ticket + rama + sesión"] --> P
    P["<b>fase: plan</b><br/>criterios de aceptación<br/>supuestos abiertos"]
    P -->|"<b>aprobar</b><br/>congela la huella"| AP{{"spec aprobado"}}
    AP -->|"<b>open --repo R2</b><br/>se niega sin aprobar"| P2["2º repo<br/>sesión propia"]
    AP --> D["<b>fase: dev</b><br/>commit · commit · commit"]
    D --> T["<b>fase: test</b><br/><b>check</b> escribe evidencia"]
    T -->|falla| D
    T --> C["<b>close</b><br/>pushea · archiva<br/>imprime el compare"]
    C --> PR["PR a mano<br/><i>factoria no lo crea</i>"]
    style N fill:#238636,color:#fff
    style AP fill:#8250df,color:#fff
    style C fill:#1f6feb,color:#fff
    style PR fill:#6e7681,color:#fff
```

Los comandos, en el orden en que se usan:

| Paso | Comando | Qué pasa |
|---|---|---|
| 1 | `new <slug> --repo R` | ticket en `fase: plan`, **rama nueva** `<tipo>/<slug>`, sesión con uuid conocido, issue + tarjeta |
| 2 | *(en la sesión)* | se escriben `## Criterios de aceptación` y `## Supuestos abiertos` |
| 3 | `aprobar <slug>` | congela la huella de criterios + fuera de alcance |
| 4 | `open <slug> --repo R2` | suma un 2º repo con su propia sesión. **Se niega si no está aprobado** |
| 5 | `commit --mensaje "…"` | valida `<tipo>: <imperativo>` ≤60 chars, protege ramas y archivos |
| 6 | `check --slug <slug>` | corre los `verify:` del perfil + las cotas, y escribe la evidencia |
| 7 | `fase <slug> dev\|test` | metadata. **No corta la sesión** |
| 8 | `close <slug>` | archiva el cuerpo e imprime **los pasos que faltan a mano**, en orden. `--sin-pushear` para cerrar sin tocar el remoto |

Flags de `new` que importan: `--aqui` (no crear rama, usar la actual),
`--pedido "…"` (en vez de abrir el editor), `--no-lanzar`, `--tipo fix|chore`.

### Cerrar sin pushear

`close` pushea por defecto. Con `--sin-pushear` no toca el remoto y el push pasa
a ser el primer paso de la lista que imprime:

```
Falta a mano, en este orden:

  defeve
    1. git -C C:\ariel\dfv\wt\rediseno-home push -u origin feature/rediseno-home
    2. abrir el PR a mano en Bitbucket:  https://bitbucket.org/dfvsrl/defeve/branch/...
    3. cuando el PR este mergeado, y no antes:
         factoria close mi-slug --repo defeve --limpiar-worktree
         git -C C:\ariel\dfv\defeve branch -d feature/rediseno-home
         git -C C:\ariel\dfv\defeve push origin --delete feature/rediseno-home
```

El orden no es decorativo: el worktree va **antes** del borrado local, porque
mientras la rama esté checkouteada ahí `branch -d` no puede sacarla. Y es `-d`
minúscula a propósito — si falla, algo no se mergeó: hay que mirar, no forzar
con `-D`.

Si la rama registrada es la base (pasa con `new --aqui`) o está en
`ramas_prohibidas`, **no propone borrarla**. Y cerrar dos veces no re-archiva:
el historial no se pisa.

## Volver a una tarea

```mermaid
flowchart LR
    S(["quiero seguir<br/>con algo"]) --> Q{"¿el ticket apunta<br/>a la sesión real?"}
    Q -->|sí| R["<b>resume</b> slug<br/><i>continúa la charla</i>"]
    Q -->|"no: quedó el<br/>uuid reservado"| AD["<b>adoptar</b> slug"]
    AD --> R
    R --> W{"¿pesa más<br/>de 1 MB?"}
    W -->|no| GO(["seguir"])
    W -->|sí| CU["<b>cortar --fork</b><br/>+ <b>contexto</b>"]
    CU --> GO
    style R fill:#238636,color:#fff
    style CU fill:#bf8700,color:#fff
```

```bash
factoria resume <slug>          # sin argumentos, lista lo que hay en vuelo
factoria adoptar <slug>         # registra la sesión que hizo el trabajo de verdad
factoria cortar <slug> --fork   # corta cuando pesa, preservando la anterior
factoria contexto <slug>        # el pack mínimo para arrancar fresco: ticket + vecinos
```

**Las cuentas `dfv` y `personal` son transcripts disjuntos** (dos directorios
distintos, 0 uuid en común sobre 337): una sesión **no se puede reanudar desde
la otra cuenta**. Lo que se cambia es a qué cuenta apunta el ticket.

| Quiero… | Comando |
|---|---|
| la sesión que ya existe en la otra cuenta | `resume <slug> --repo R` |
| arreglar "la abrí en la cuenta equivocada" | `adoptar <slug> --cuenta personal` |
| una entrada nueva, con su propia sesión | `open <slug> --repo R --cuenta personal` |
| el trabajo, no la conversación | `contexto <slug>` y sesión fresca donde sea |

## El tablero de GitHub

```mermaid
flowchart LR
    MD["<b>ticket .md</b><br/>canónico"] -->|"new · aprobar<br/>fase · close"| GH["issue + tarjeta"]
    GH -.->|"arrastrar una tarjeta<br/>NO vuelve al archivo"| MD
    style MD fill:#1f6feb,color:#fff
    style GH fill:#6e7681,color:#fff
```

Una sola vía. El tablero es un **espejo legible**, y cada campo sale del front
matter del ticket:

| En el ticket | En GitHub |
|---|---|
| `slug` | título del issue |
| `fase` | campo `fase` → **la columna del tablero** |
| `spec_congelado` | campo `spec`: `sin aprobar` / `aprobado` / `deriva` |
| `repos[].cuenta` | campo `cuenta` + label `cuenta:dfv` |
| `repos[].rama` | campo `rama` |
| `repos[].repo` | labels `repo:defeve`, `repo:Cotizaciones`… |
| pedido, criterios, fuera de alcance, supuestos | cuerpo del issue, regenerado |
| `abierto: false` | issue cerrado |

Los repos van como **label** y no como campo del Project porque un ticket cruza
repos por naturaleza y un single-select no puede tener N valores.

Sin red o sin `gh`, **todo comando sigue funcionando** y `board` avisa
`espejo N tickets abiertos sin issue`. `factoria abrir <slug>` abre el issue en
el navegador; `factoria espejo --todos` resincroniza.

> ⚠️ Los `- [ ]` de los criterios se ven como checklist en el issue, pero son de
> **solo lectura**: tildarlos ahí se pierde en la próxima sincronización.

**Dos pasos que quedan en la UI** — `gh` no puede configurar vistas: agrupar el
tablero por `fase` (⋯ del board → *Group* → `fase`) y apagar los dos workflows
de fábrica que repueblan `Status` (⚙ → *Workflows*). `Status` no se puede
borrar: es un campo built-in de GitHub.

## Mirar el estado

```bash
factoria board       # el relevamiento completo + HALLAZGOS
factoria tickets     # fase, repo, rama, cuenta, sesión, issue, líneas
factoria cotas       # mide las cotas del estándar y SALE CON 1 si alguna se pasó
factoria sesiones --paralelas          # ramas con más de una sesión viva
factoria grafo --que-toca <archivo>    # quién más tocó eso
factoria grafo --bloqueos              # la cadena de dependencias cross-repo
```

## Mantenimiento

| Comando | Para qué |
|---|---|
| `espejo [<slug>] [--todos] [--probar]` | resincroniza. `--probar` diagnostica sin escribir |
| `skills [<fase>]` | qué skill de mattpocock usar en cada fase, y cuál queda afuera |
| `sync-skills [--desvincular]` | vincula las skills y agents del repo de datos a los dos config dirs |
| `perfiles` | genera un perfil por repo detectando dónde van sus docs |
| `rama <slug> <nueva> [--solo-registro]` | renombra la rama. `--solo-registro` si la rama es ajena |
| `renombrar <slug> <nuevo>` | renombra el ticket, sus docs y sus ramas |
| `check --instalar-pre-push` | pobla el `hooksPath` del repo con un pre-push que corre `check` |
| `commit --imprimir-hook` | el stanza de `Stop` para pegar en `settings.local.json` |

## Dónde vive todo

```mermaid
flowchart TB
    L["lanzador en ~/.local/bin<br/><i>+ el .cmd, porque bash<br/>no usa PATHEXT</i>"] --> PY
    subgraph COD["repo de CÓDIGO"]
        PY["factoria.py"]
        TS["tests/"]
    end
    subgraph DAT["repo de DATOS · privado"]
        TK["tickets/ · docs/ · historial/"]
        CT["contratos/"]
        PF["profiles/"]
        SK["skills/ · agents/"]
    end
    PY -->|"constante DATOS,<br/>nunca por copia"| TK
    SK -->|"junction<br/>mklink /J"| CFG["los dos<br/>config dirs de Claude"]
    style PY fill:#0d1117,color:#fff
    style TK fill:#1f6feb,color:#fff
```

| Qué | Dónde |
|---|---|
| código | `C:\ariel\integhra\factoria` |
actoria` |
| datos (privado) | `C:\ariel\.factoria` |
| contratos | `C:\ariel\dfv\.contracts` → junction a `.factoria\contratos` |
| skills en uso | `~\.claude-dfv\skills` y `~\.claude-personal\skills` → junctions |

Los lanzadores ejecutan **el archivo que esté checkouteado**. Ningún archivo vive
en los dos repos: las skills salen del repo de datos, que es privado justamente
porque nombran repos y rutas internas.

## Las cuatro reglas que explican el resto

1. **La fase no corta la sesión.** Reconstruir contexto cuesta más que seguir. Se
   corta cuando el `.jsonl` pesa (`board` lo dice), no cuando cambia la fase.
2. **Los `.md` son canónicos, GitHub es espejo.** Sin red todo sigue andando. La
   fase se cambia con `factoria fase`, no arrastrando una tarjeta.
3. **Lo determinista y con estado va en código; el juicio sobre contenido, en una
   skill.** Las cotas escritas en un `.md` decayeron todas — por eso `cotas` sale
   con código de error y se cuelga de un `verify:`.
4. **No hay PR automático ni merge.** `close` te deja los comandos y ahí para.
   En `defeve` el merge a `master` es siempre por PR en Bitbucket.

## Cuando algo no anda

| Síntoma | Causa | Salida |
|---|---|---|
| `commit` no commitea y no dice nada | la rama está en `ramas_prohibidas` del perfil | ahora avisa; si el repo trabaja en `main` legítimamente, poner `ramas_prohibidas: []` |
| `check` no verifica casi nada | ese perfil no declara `verify:` | agregarlo, o es un linter de cotas y no un DoD |
| `resume` abre una sesión vacía | el ticket apunta a un uuid reservado que nunca se abrió | `factoria adoptar <slug>` |
| el ticket no aparece en el tablero | el espejo quedó pendiente (sin red, sin `gh`) | `factoria espejo --todos` |
| un `cd C:\ruta` en bash no llega | bash se come los `\` | `git -C C:/ruta`, o barras normales |

Los tests son el `verify:` de este repo: `python -m pytest tests -q` más
`python -m ruff check --select F --isolated factoria.py`.
