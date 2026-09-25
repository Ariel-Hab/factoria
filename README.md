# factoria

Gestor de tickets, sesiones y documentación para el ecosistema DFV. Un archivo
Python, 22 comandos, sin servidor y sin base de datos.

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
    P["<b>fase: plan</b><br/>criterios · entregables<br/>supuestos abiertos"]
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
| 2 | *(en la sesión)* | se escriben `## Criterios de aceptación`, `## Entregables de código` y `## Supuestos abiertos` |
| 3 | `aprobar <slug>` | congela la huella de criterios + fuera de alcance |
| 4 | `open <slug> --repo R2` | suma un 2º repo con su propia sesión. **Se niega si no está aprobado** |
| 5 | `commit --mensaje "…"` | valida `<tipo>: <imperativo>` ≤60 chars, protege ramas y archivos |
| 6 | `check --slug <slug>` | corre los `verify:` del perfil + las cotas, y escribe la evidencia |
| 7 | `fase <slug> dev\|test` | metadata. **No corta la sesión** |
| 8 | `close <slug>` | archiva el cuerpo e imprime **los pasos que faltan a mano**, en orden. `--sin-pushear` para cerrar sin tocar el remoto |

Flags de `new` que importan: `--aqui` (no crear rama, usar la actual),
`--pedido "…"` (en vez de abrir el editor), `--no-lanzar`, `--tipo fix|chore`, y
los de épica — `--epica`, `--continua`, `--push-etapas` — que tienen
[su sección](#épicas-etapas-encadenadas).

### Qué se declara en `plan`

Cuatro secciones, y las tres primeras `aprobar` las exige no vacías:

- **`## Pedido crudo`** — literal, como llegó. Se escribe una vez y no se toca
  más: es contra lo que se compara si después hubo malentendido.
- **`## Criterios de aceptación`** — cada uno respondible con sí/no y nombrando
  *su* evidencia. "funciona bien" no es un criterio.
- **`## Fuera de alcance`** — lo que se decidió NO hacer.
- **`## Entregables de código`** — qué se va a tocar, **por unidad con nombre en
  el sistema**: un servicio, un endpoint, una tabla, una pantalla, un job. Con
  el verbo adelante, porque crear y tocar algo preexistente no cuestan lo mismo
  de revisar:

  ```markdown
  - **nuevo** servicio `AuthService` — emite y valida el token
  - **modifica** `LoginController` — delega en AuthService, saca el check inline
  - **nuevo** endpoint `GET /api/usuarios` — solo rol `admin`
  - **nueva** tabla `usuario_sesion`
  ```

  **Si la unidad declara permisos, el rol o tipo de usuario permitido va en la
  misma línea** — `solo rol admin`, `cualquier usuario autenticado`, `público`.
  Un permiso sin rol declarado no se puede revisar: es de las pocas cosas que,
  mal interpretadas, no se ven hasta que ya están en producción.

  Si no sabés cómo nombrarlo no es un entregable, es implementación; y si pasás
  de ~7 bullets el ticket son dos tickets. Nada de firmas ni de snippets: para
  eso está el doc de trabajo.
- **`## Supuestos abiertos`** — cada cosa que el modelo tuvo que adivinar,
  escrita **como adivinanza**. Acá `aprobar` avisa pero no bloquea: un ticket
  sin supuestos es sospechoso, no inválido.

**La huella que congela `aprobar` es criterios + fuera de alcance, y nada más.**
Los entregables se exigen pero no se congelan a propósito: son el *dónde*, no el
*qué*, y descubrir superficie nueva durante `dev` es sano. Pintarlo de
spec-drift rojo convertiría el gate en ruido.

### Cerrar sin pushear

`close` pushea por defecto — salvo una etapa intermedia de una épica
`al-final`, ver abajo. Con `--sin-pushear` no toca el remoto y el push pasa a
ser el primer paso de la lista que imprime:

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

### Épicas: etapas encadenadas

Una épica es **un ticket común que hace de paraguas**; no hay objeto nuevo. Sus
etapas son tickets que la nombran, cada una con su rama encadenada a la de la
anterior:

```bash
factoria new rediseno --push-etapas al-final --pedido "…" --no-lanzar   # la épica
factoria new etapa-1 --epica rediseno --pedido "…" --no-lanzar          # desde el base:
factoria new etapa-2 --continua etapa-1 --pedido "…" --no-lanzar        # desde feature/etapa-1
```

`--continua` hereda la épica de la etapa anterior y hace nacer la rama de la de
ella. Queda en el front matter como `epica:` y `continua_a:` — el puntero va
**hacia atrás**, porque cuando nace la etapa N la N+1 no existe.

`push_etapas:`, en la épica, decide cuándo se pushea:

| Modo | Etapa intermedia | La que cierra la cadena |
|---|---|---|
| `al-final` *(default)* | no pushea, ni imprime compare | pushea su rama —que ya contiene toda la cadena— y **un** compare contra el `base:` |
| `por-etapa` | pushea, compare contra la rama de la anterior | igual |

Un `--pushear` o `--sin-pushear` explícito le gana al modo. En `al-final`, al
cerrar la cadena `close` verifica que las ramas de las otras etapas estén
contenidas en la que pushea: si alguna no lo está —se cerraron fuera de orden,
o vive en otro repo— lo dice en rojo con el push que falta.

`board` y `tickets` listan las etapas **debajo de su épica, en orden de
cadena**, y avisan si la cadena está rota. En `board`, una rama sin push de
una épica `al-final` figura como `diferido`, no como riesgo — mientras a la
cadena le quede una etapa abierta, o si ya quedó contenida en la rama pusheada.
El grafo mide cada etapa contra la rama de la anterior, así `--que-toca` no le
atribuye a la etapa 2 lo que hizo la 1.

## Volver a una tarea

Un ticket tiene **una sesión responsable** — la que `resume` reanuda — y guarda
**todas las que pasaron por él**. Cambiar de responsable no borra a la anterior:
queda asociada, y volver es el mismo comando con su uuid.

```mermaid
flowchart LR
    S(["quiero seguir<br/>con algo"]) --> Q{"¿desde dónde?"}
    Q -->|"la cuenta<br/>que lo tiene"| R["<b>resume</b> slug<br/><i>continúa la charla</i>"]
    Q -->|"la otra cuenta"| AD["<b>adoptar</b> slug<br/><b>--cuenta</b> X"]
    AD --> NU["sesión nueva allá,<br/>arranca leyendo el pack"]
    R --> W{"¿pesa más<br/>de 1 MB?"}
    W -->|no| GO(["seguir"])
    W -->|sí| CU["<b>resume --fork</b><br/>o <b>resume --nueva</b>"]
    CU --> GO
    NU --> GO
    style R fill:#238636,color:#fff
    style AD fill:#8250df,color:#fff
    style CU fill:#bf8700,color:#fff
```

```bash
factoria resume <slug>                 # continúa la sesión responsable
factoria resume <slug> --fork          # ramifica cuando pesa: mismo contexto, .jsonl nuevo
factoria resume <slug> --nueva         # limpia, con el pack, en la misma cuenta
factoria adoptar <slug> --cuenta dfv   # que lo agarre la otra cuenta
factoria adoptar <slug> --aqui         # que lo agarre esta sesión, que ya está abierta
factoria sesiones --ticket <slug>      # por dónde pasó el ticket
factoria contexto <slug>               # el pack: ticket + doc de trabajo + vecinos
```

### Una responsable, ninguna perdida

El ticket lleva el registro adentro, una línea por sesión:

```yaml
repos:
- repo: defeve
  cuenta: personal
  session_id: 11111111-...        # la responsable: la que `resume` reanuda
  sesiones:
  - b7f7288f-...  dfv       2026-09-09
  - 11111111-...  personal  2026-09-11
```

Eso es lo que arregla el modo de fallo que tenía `adoptar`: antes había **un solo
slot**, así que cambiar de sesión responsable borraba la referencia a la anterior
y no había cómo volver ni cómo saber por dónde había pasado el trabajo. Ahora
`adoptar <slug> <uuid-viejo>` la devuelve, y las sesiones menores — una consulta
al costado, un fix puntual — pueden quedar asociadas sin que ninguna le saque la
posta a la que manda.

### Cambiar de cuenta: para eso está `adoptar`

**Las cuentas `dfv` y `personal` son transcripts disjuntos** (dos directorios
distintos, 0 uuid en común sobre 337): una sesión **no se puede reanudar desde la
otra cuenta**. Lo que se muda no es la conversación, es el ticket.

```bash
factoria adoptar <slug> --cuenta dfv
```

```mermaid
flowchart LR
    T["ticket<br/><i>cuenta: personal</i>"] --> N["<b>adoptar slug<br/>--cuenta dfv</b>"]
    N --> S["sesión nueva en dfv<br/><i>arranca leyendo el pack</i>"]
    N --> R["responsable: la nueva"]
    N --> A["la de personal<br/>queda asociada"]
    T -.->|"la conversación<br/>NO se muda"| X(("✗"))
    style N fill:#238636,color:#fff
    style S fill:#1f6feb,color:#fff
    style X fill:#6e7681,color:#fff
```

Hace las tres cosas juntas: abre la sesión en la cuenta que le pidas, le pasa el
pack de `contexto` como **primer prompt**, y le da la responsabilidad del ticket
— así el `resume` siguiente ya cae ahí.

Si al final no se lanza nada — por ejemplo porque estás adentro de otra sesión y
no querés anidar — **el registro se revierte** y te deja la receta para pegar en
otra terminal. Cuando esa sesión abra, `factoria adoptar <slug> --aqui` adentro
la anota. Un uuid escrito que nadie va a abrir sigue siendo un fantasma.

| Quiero… | Comando |
|---|---|
| **que el ticket lo siga la otra cuenta** | `adoptar <slug> --cuenta dfv` |
| **que lo siga esta sesión, que ya está abierta** | `adoptar <slug> --aqui` |
| volver a la sesión de antes | `adoptar <slug> <uuid>` |
| seguir donde estaba | `resume <slug>` |
| cortar porque se puso cara | `resume <slug> --fork` (ramifica) o `--nueva` (limpia, con pack) |
| ver por dónde pasó el ticket | `sesiones --ticket <slug>` |
| una entrada nueva para un 2º repo | `open <slug> --repo R2 --cuenta personal` |

`adoptar` **no adivina**: sin `--cuenta`, sin `--aqui` y sin uuid te dice las dos
recetas y para. La versión anterior elegía sola barriendo los transcripts, y
elegir mal ahí significaba pisar el único puntero que existía. Esa búsqueda sigue
disponible como **pregunta** en `sesiones --ticket <slug>`, que es de solo
lectura y marca cuál es la responsable.

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
| pedido, criterios, fuera de alcance, entregables, supuestos | cuerpo del issue, regenerado |
| `abierto: false` | issue cerrado |

Los repos van como **label** y no como campo del Project porque un ticket cruza
repos por naturaleza y un single-select no puede tener N valores. `epica`,
`continua_a` y `push_etapas` no se espejan: el issue sigue plano.

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
| `resume` abre una sesión vacía | el ticket apunta a un uuid reservado que nunca se abrió | `factoria adoptar <slug> --aqui` desde la sesión real, o `sesiones --ticket <slug>` para encontrarla |
| cambié de sesión responsable y perdí la anterior | había un solo slot: `adoptar` la pisaba | ya no pasa; quedan todas asociadas, y `adoptar <slug> <uuid>` vuelve a cualquiera |
| `adoptar` pide que diga quién | cambiar de responsable no tiene default razonable | `--cuenta X` abre una sesión nueva allá; `--aqui` toma la actual |
| el ticket no aparece en el tablero | el espejo quedó pendiente (sin red, sin `gh`) | `factoria espejo --todos` |
| un `cd C:\ruta` en bash no llega | bash se come los `\` | `git -C C:/ruta`, o barras normales |

Los tests son el `verify:` de este repo: `python -m pytest tests -q` más
`python -m ruff check --select F --isolated factoria.py`.
