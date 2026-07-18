# ⏳g SincroGit — Documento de diseño

> Una máquina del tiempo automática y versionada para tus repos (y continuidad
> multi-máquina de bajo esfuerzo), con **cero** disciplina de Git.
> Plataforma objetivo: **Windows** (uso interactivo, una sola máquina a la vez).

> **Cómo leer este documento.** §1–§11 describen el sistema **tal como está construido**
> y se mantienen sincronizados con el código (§1 conserva los objetivos originales en
> primera persona; §9 es el *plan* de despliegue — la pieza de la Fase 3 aún pendiente).
> §12 (roadmap) y §13 (registro de decisiones) registran **historia**: sus entradas no se
> reescriben retroactivamente — cuando la realidad avanzó, lo dice una nota explícita
> *(superado después …)*. Para el día a día, el [Manual](MANUAL_ES.md); para la referencia
> de configuración, el [LEAME](LEAME.md#configuración); para la relación de SincroGit con
> las herramientas vecinas (jj, GitButler, dura, …) y la lista de trabajo pendiente, las
> secciones [Cómo se compara](LEAME.md#cómo-se-compara-con-las-herramientas-vecinas) y
> [TODO](LEAME.md#todo) del LEAME.

---

## 1. Objetivos y alcance

**Quiero dos cosas a la vez:**

1. **Versionado + máquina del tiempo, con cero disciplina.** Volver cualquier fichero *guardado* a un estado anterior (rompiste/borraste/sobrescribiste algo) o a ayer — sin ejecutar `git` jamás. (Fotografía lo que está en disco, no los buffers no guardados del editor; un corte de luz con el disco intacto no pierde nada de todos modos — el valor es el rollback, no sobrevivir al crash. Un *fallo total de disco* es el único caso que cubre el espejo remoto, y es raro.)
2. **Sincronización entre equipos (secuencial).** Trabajo casi siempre en el sobremesa y ocasionalmente en el portátil, **nunca a la vez**. Al cambiar de máquina, quiero que las fuentes se actualicen automáticamente (en minutos — ver §4.2; no instantáneo).

**Fuera de alcance (de momento):**

- Edición simultánea en dos máquinas / resolución de conflictos compleja.
- Versionado de binarios o ficheros grandes (esos los commiteo/pusheo **a mano**).
- Cualquier SO que no sea Windows. En Linux/otros haré `pull` a mano y no editaré.

---

## 2. Modelo conceptual: dos niveles (el diseño "shadow")

El truco para conciliar *"snapshot casi instantáneo"* con *"no quiero miles de commits"* — Y con *"mi `git log`/`git status` tienen que seguir siendo míos"* — es separar dos niveles, manteniendo el rápido FUERA de la rama del usuario:

| Nivel | Qué es | Frecuencia | Visible en `git log` |
|-------|--------|-----------|----------------------|
| **Snapshot (shadow)** | Un commit construido con un **índice privado** (`.git/sincro-index`) y añadido a un **ref lateral** `refs/sincro/wip/<rama>` — HEAD, el índice del usuario y su worktree no se tocan nunca | Cada ~5 min (con debounce) | No (ref lateral; cualquier herramienta git ve un repo normal) |
| **Sellado (historia)** | El árbol acumulado de snapshots se commitea como **un commit real** en la rama (`commit-tree` + `update-ref`); la cadena shadow **se re-ancla** ahí | Cada ~6 h | Sí (commit permanente) |

```
rama:    ... ── sellado_N ─────────────────────── sellado_N+1   ← HEAD (solo commits reales)
                     │                                 ▲
shadow:              └── s1 ── s2 ── s3 ── … ── s42 ───┘  refs/sincro/wip/<rama>
                        (un commit-snapshot cada ~5 min; al sellar, la cadena se
                         re-ancla en sellado_N+1 y la vieja queda en el reflog del
                         ref lateral ~30 días)
```

**Por qué funciona:**

- El estado guardado actual se commitea cada ~5 min → un **punto de rollback** con resolución ~5 min (la punta shadow = último snapshot; los anteriores son commits reales de la cadena, y las cadenas pre-sellado quedan en el reflog del ref lateral). OJO: esto *no* es protección ante cortes de luz — los ficheros guardados sobreviven al corte en el disco igualmente, y los buffers no guardados nunca se capturan; el valor es la máquina del tiempo.
- Los snapshots no aparecen jamás en la rama: solo aterrizan **~4 commits/día** (un sellado cada 6 h) — y `git status` sigue mostrando los cambios sin commitear reales del usuario, con su staging intacto.
- El historial "limpio" (sellados) es lo único que viaja a la rama del remoto → **pull siempre limpio, sin force-push** (ver §4).

> **Historia de este diseño:** la v0.1 mantenía el snapshot como un único commit WIP *en la punta*, amendeado in situ. Funcionaba, pero ocupaba HEAD (confundía a toda herramienta git y secuestraba `git status`/staging). La v0.2 movió los snapshots al ref shadow — mismos ritmos, mismas ventanas de recuperación, invisible. Los repos antiguos se migran solos al arrancar (el WIP de la punta pasa al ref shadow y la rama vuelve a su padre; las ediciones sin sellar reaparecen como cambios sin commitear normales).

---

## 3. Flujo detallado

### 3.1 Arranque del servicio (por repo)
1. Validar que es repo git (una carpeta ausente o inválida salta ese repo; los demás
   siguen). El remoto se comprueba perezosamente en cada sync (`has_remote`); estar en la
   rama configurada es trabajo de la guarda de rama (§11).
2. **Migrar el WIP legado de la punta** si existe (repos v0.1; ver §2) y asegurar que
   existe el **ref shadow** (anclado en HEAD; se pone `core.logAllRefUpdates=always`
   local para que el ref lateral tenga reflog), y registrar el repo en el watcher.
3. **Snapshot inicial**, antes de tocar la red: captura los cambios previos a esta
   ejecución (p. ej. ediciones hechas con SincroGit apagado, o tras un reinicio).
4. **Sync inicial en un hilo de fondo** — una red lenta nunca retrasa la red de
   seguridad local: `fetch` + (solo si el remoto adelanta) rebase de la rama local
   (`--autostash`), exactamente como el pull periódico (§3.4).
   - **Si hay conflicto** → `git rebase --abort`, se **pausa el autosync de ese repo**, se notifica al usuario y se registra en log. **Nunca** se resuelve de forma destructiva ni se hace force. (Esto es raro en uso secuencial, pero la política es: ante la duda, no perder datos.)

### 3.2 Ciclo de snapshot (cada ~5 min, con debounce)
- El **watcher** (eventos del sistema de ficheros) marca el repo como *dirty* y reinicia un debounce (p. ej. 20-30 s sin cambios).
- Cuando el debounce se asienta **y** ha pasado ≥5 min desde el último snapshot:
  1. Sincronizar el **índice privado** con la punta shadow (barato en régimen), difearlo
     contra el worktree — el "qué cambió desde el último snapshot" exacto — y aplicar el
     **filtro** (§5) a los candidatos.
  2. `git add <solo los candidatos>` EN el índice privado (`GIT_INDEX_FILE`).
  3. `write-tree`; si el árbol difiere del de la punta shadow (con textconv, así un
     reguardado de `.docx` solo-estilo no cuenta): `commit-tree` + `update-ref` del ref
     shadow (`sincro: snapshot`). HEAD, el índice del usuario y `git status` intactos.
- Sin cambios → no se hace nada.
- **Anti-inanición:** una fuente que nunca se asienta (un build largo, un log que escribe
  dentro del repo) reinicia el debounce sin parar — así que pasadas **2× el intervalo de
  snapshot** desde el último, se toma uno igualmente, con o sin debounce
  (`Engine.SNAPSHOT_STARVATION_FACTOR`). `debounce_sec: inf` conserva su significado de
  "no disparar nunca".

### 3.3 Sellado (cada 6 h)
**Único disparador automático:** temporizador de **6 h desde el último sellado**.

0. **Los commits del propio usuario cuentan como sellos.** Al vencer el temporizador, si
   hay un commit permanente (no-WIP) más nuevo que la base del reloj — un `git commit`
   manual hecho en una terminal, o commits que integró un pull — la ventana **se reinicia
   desde él** en vez de apilar un checkpoint `sincro:` pegado al suyo. (El "external
   commit detected; seal clock reset" de la v0.1, portado al modelo shadow; se comprueba
   solo cuando un sello vence, así que no cuesta nada en régimen. El recordatorio purista
   se refresca de la misma fuente.)
1. Si el usuario tiene algo **staged** (un commit manual en preparación) → **ceder** este
   ciclo; un auto-sellado nunca absorbe un commit hecho a mano. (Un Smart Commit
   explícito sí procede — lo pidió el usuario.)
2. Snapshot final; si el árbol del snapshot coincide con el de HEAD (con textconv) →
   **no sellar** (no ensuciar el historial).
3. Generar mensaje con IA a partir de `git diff HEAD <árbol-snapshot>` (§6).
4. `commit-tree <árbol-snapshot> -p HEAD -m "<mensaje IA>"` + avanzar la rama, refrescar
   el índice del usuario (reset mixed; el worktree — que ES ese árbol — no se toca) y
   **re-anclar la cadena shadow** en el sellado nuevo.
5. **Push** (§4).

> No hay sellado por inactividad ni por apagado. Para forzar un sellado+push puntual (p. ej. justo antes de irme al portátil): *Seal now* / *Seal+Push* por repo en la bandeja, `--seal-once` desde la CLI, o un Smart Commit.

### 3.4 Pull periódico (cada 10 min)
Además del pull de arranque (§3.1), el demonio comprueba el remoto cada **10 min** para traer lo que dejó la otra máquina, **sin** que tenga que reiniciar sesión ni pullear a mano.

1. **`git fetch`** (barato; no toca el árbol de trabajo).
2. Comprobar si el remoto tiene commits nuevos:
   `git rev-list --count HEAD..<remote>/<branch>`.
   - Si es **0** → no hay nada que traer → **no se hace nada** (caso habitual mientras trabajo en esta máquina).
   - Si es **> 0** → primero un **snapshot** (la garantía de recuperación de todo lo de
     abajo) y después **`git rebase --autostash <remote>/<rama>`** — las ediciones del
     usuario viven sin commitear en el worktree, y el autostash las lleva por encima
     del rebase.
3. Dos formas de conflicto, ambas → **pausar autosync de ese repo + notificar**, resolver
   a mano (nunca force, nunca pérdida de datos):
   - el **rebase en sí conflicta** → `git rebase --abort`, árbol intacto;
   - el rebase termina pero **re-aplicar las ediciones sucias conflicta** → git deja
     marcadores en los ficheros afectados (y una entrada de stash); el estado exacto
     pre-pull está a un restore de la máquina del tiempo gracias al snapshot del paso 2.

> Como el uso es **secuencial** (nunca las dos máquinas a la vez), mientras trabajo en una, la otra no pushea → el paso 2 da 0 y el pull no se dispara. Al sentarme en la otra máquina, en ≤10 min coge sola lo que sellé en la primera.

---

## 4. Push y multi-máquina

**Regla de oro: solo se pushean commits sellados; el WIP nunca sale de la máquina.**

- Push: en el modelo shadow **HEAD solo contiene sellados y commits del usuario** (el WIP
  vive en el ref lateral `refs/sincro/wip/<rama>`, §2), así que empujar HEAD es seguro por
  construcción y nunca filtra el WIP: `git push origin HEAD:refs/heads/<rama>` → el remoto
  recibe historia inmutable. (Un backlog sin subir viaja implícito y reintenta en el próximo sync.)
- Como los sellados son inmutables y nunca se reescriben, **el push es siempre fast-forward** y el **pull de la otra máquina es siempre limpio**. No hace falta force-push en ningún caso del flujo normal.

**Handoff entre máquinas (uso secuencial):**

```
Sobremesa: trabaja → cada 6h (o un Seal now / Smart Commit manual) sella + push  ──►  remoto al día
Portátil:  arranca → pull --rebase (limpio) → trabaja → sella + push ──► remoto al día
Sobremesa: arranca → pull --rebase (limpio) → continúa...
```

**Handoff normal (rama limpia):** el portátil hace `pull --rebase` de la rama y arranca con lo **sellado** (hasta 6 h atrás). Para un handoff a media ventana, lanza un sellado manual (**Seal now** / `--seal-once` / Smart Commit) antes de levantarte y el portátil arrancará con todo por la vía limpia.

### 4.1 Autosnap (espejo en vivo) — recuperación ante desastre

Como sellar cada 6 h dejaría hasta 6 h de trabajo fuera del remoto, **autosnap** desacopla el *backup remoto* del *historial*: cada **30 min** (y solo si hubo cambios) se hace `push --force` del **shadow tip** (`refs/sincro/wip/<rama>` — sellados **+ el WIP vivo**) a un ref lateral **por usuario y máquina** `refs/autosnap/<user>/<host>/<rama>` (el namespace se detalla en §4.2).

- **No ensucia la rama:** nadie pullea ese ref para trabajar; la rama `main` sigue recibiendo solo sellados → pull siempre limpio. Es la excepción deliberada a "el WIP no sale de la máquina", acotada a un ref de backup.
- **RPO ante fallo total de disco ≈ 30 min** (en vez de 6 h). En la otra máquina: *Fetch autosnaps* → explorar/restaurar el último estado (fichero o repo entero).
- **Coste:** hasta ~48 push/día/repo en trabajo activo (force-push barato; **nada** en repos inactivos, porque solo sube si el shadow tip cambió desde el último espejo). Objetos huérfanos en el remoto hasta su GC.
- **Corte de luz / crash de SO** no necesita nada especial del autosnap: los ficheros guardados sobreviven en el disco local, y el snapshot de 5 min / el `reflog` dan los puntos de rollback. El autosnap es para el caso *la-máquina-ya-no-está* (y el relevo, §4.2).

### 4.2 Relevo entre máquinas (WIP vivo)

El espejo autosnap es también el sustrato del **relevo automático entre máquinas**,
desacoplado del sellado (así funciona también en modo purista, donde el sello no se
dispara). Dos puntos de diseño:

- **Namespace `refs/autosnap/<user>/<host>/<rama>`.** El `<host>` mantiene a cada máquina
  como *único escritor* de su propio ref (un `--force` plano es seguro, sin pisado). El
  `<user>` (`git config user.email` saneado) permite que una máquina reconozca a sus
  *propias* otras máquinas frente a las de un compañero, así el relevo solo baja
  `refs/autosnap/<user>/*` — barato y *team-safe* (nunca toca `main`/feature, solo refs
  laterales personales).
- **Comparar por CONTENIDO de trabajo, no por ancestría.** Sutileza clave: las cadenas de
  snapshots de dos máquinas son *hermanas* (ambas ancladas en el sello compartido), nunca
  descendientes una de otra — la ancestría reportaría divergencia constantemente. En su
  lugar `GitRepo.work_relationship(mine, theirs)` compara las dos puntas shadow, respecto
  al merge base, por las *rutas que cambió cada lado*: si `theirs` coincide con `mine` en
  toda ruta que yo cambié (y tiene más) es `theirs_contains` → seguro adoptar; si no, se
  clasifica `equal` / `mine_contains` / `diverged`.

Comportamiento — `live_handoff` es un mando de 3 estados (`auto` por defecto | `ask` | `off`):
- **`theirs_contains` → apply seguro, primero-el-contenido**: el WORKTREE se hace coincidir
  con el árbol del snapshot del peer (escrituras/borrados solo-worktree de las rutas que
  difieren — el HEAD y la rama del usuario no se mueven jamás; la historia sellada se
  reconcilia por el pull normal) y un snapshot de cierre lo registra en MI cadena.
  Demostrablemente sin pérdida (el peer tiene todo mi contenido de las rutas que cambié),
  reversible vía el reflog shadow, y se **rechaza (con notificación)** allí donde tocaría
  contenido que los snapshots no tienen (`untracked_collisions`, o ediciones locales que
  el filtro rechazó — no existen en ningún sitio de git, así que sobrescribirlas las
  destruiría). En `auto` se aplica al momento **y se lanza una notificación de
  bandeja** (el nivel b nunca es *silencioso* — que el working tree cambie bajo tus pies
  sorprende aunque no se pierda nada). En `ask` NO se aplica: se registra el candidato
  (`pending_handoff`, expuesto en `status()` y el panel), se notifica, y un **Apply** de un
  clic (`Engine.apply_handoff` / `--apply-handoff`) revalida desde cero (re-fetch +
  re-clasificar + re-chequear colisiones, porque el peer pudo moverse) antes del
  apply (nivel a / consentimiento).
- **`diverged` → avisar, nunca auto-merge.** A propósito sin merge 3-way automático de dos
  montones de trabajo en curso sin revisar (un árbol roto en silencio es el peor desenlace).
  Avisa **una vez** por estado distinto del peer y deja ambos intactos; el usuario resuelve
  **sellando un lado con Smart Commit y luego sincronizando** (rebase normal, con su
  pausa-conflicto habitual), o inspeccionando/fusionando el ref lateral a mano. Ver el README.

Corre al final del ciclo de sync (necesita `pull` o `push` activo) y dentro del `op_lock`
del repo. Niveles (a)+(b) de la Fase 2; un modo de auto-merge real queda fuera de alcance a
propósito.

**Acelerado por eventos del SO (baja la latencia de ~40 min a segundos).** Dos mitades, sobre
los momentos que enmarcan un cambio de máquina:
- **Irse** (sesión Windows **lock** o **suspend**): `Engine.flush_now()` fuerza snapshot +
  push de autosnap *ya* (ignorando el intervalo) en un hilo de fondo, así el espejo remoto
  queda fresco en segundos. Best-effort al suspender (~2 s antes de que muera la red; el
  intervalo normal de autosnap es el backstop); fiable al bloquear.
- **Irse de verdad** (bloqueado y ausente ≥ `seal_on_leave_min`, 20 min por defecto):
  el **leave seal**. El lock arma una cuenta atrás de reloj de pared (de pared, no
  monotónico: debe seguir contando a través de un suspend); unlock/resume la desarma;
  al disparar corre las reglas normales del sellado fuera del tick con título
  `sincro: [leave]` y pushea. Armarla no toca el reloj de 6 h — si el sellado normal
  (o un commit manual) llega antes, el leave seal no encuentra nada que publicar y NO
  mueve ningún reloj. Como mucho una vez por repo por ausencia; apagado en seco en
  modo purista (la rama sigue siendo 100 % del usuario). Un SUSPEND inminente con la
  cuenta atrás en marcha lo dispara al momento con el mensaje determinista (sin IA:
  los ~2 s de gracia perderían el commit), porque una máquina dormida no tiene timer.
- **Irse del todo** (**apagar / reiniciar / cerrar sesión**): `WM_QUERYENDSESSION` /
  `WM_ENDSESSION` (ambos, deduplicados — un apagado crítico puede saltarse el primero)
  disparan un `flush_now(wait=True, wait_timeout=20)` SÍNCRONO: el proceso muere cuando el
  handler retorna, así que en asíncrono el push se perdería en silencio.
  `ShutdownBlockReasonCreate` muestra "backing up your latest work" en la pantalla de
  apagado mientras corre (sin él Windows mata un proceso GUI a los ~5 s); la cota de 20 s
  garantiza que el apagado nunca queda secuestrado. Un `ENDSESSION(FALSE)` (alguna app lo
  vetó) re-arma el hook.
- **Llegar** (**unlock** / **resume**): `Engine.sync_soon()` deja un fetch/pull/relevo debido
  en el próximo tick y despierta el loop, así el trabajo del peer se recoge al instante.

Los disparadores: un `QAbstractNativeEventFilter` de Windows (en la app de bandeja) capta
`WM_WTSSESSION_CHANGE` (lock/unlock, vía `WTSRegisterSessionNotification` sobre el HWND del
panel) y `WM_POWERBROADCAST` (suspend/resume); irse→`flush_now`, llegar→`sync_soon`,
debounced (lock suele preceder a suspend; resume a unlock). Un **detector de salto de reloj de
pared** en el loop del motor (sin dependencias) también dispara la parte de "llegar" tras
cualquier suspensión larga — así el lado del despertar funciona también en headless (los
relojes monotónicos pueden congelarse al suspender; el de pared no).

---

## 5. Filtro de ficheros: solo código

**Criterio: se versiona automáticamente solo lo que sea TEXTO y < 1 MB.** Todo lo demás (binarios, ficheros grandes) lo gestiono **a mano**.

- **Detección de "texto"** por contenido, no por extensión (más fiable que una lista de extensiones). Como el filtro de tamaño ya garantiza que el fichero es ≤ 1 MB, se inspecciona un prefijo grande (hasta ~1 MB, no solo unos KB) y se clasifica por capas: vacío → texto; **BOM** Unicode (UTF‑8/16/32) → texto; un byte **NUL** → binario; si no, se decide por la **proporción de bytes de control** (los < 0x20 que no son espacios/tab/saltos, + DEL): muy pocos → texto legible (incluye UTF‑8 con acentos/emoji/CJK y Latin‑1); muchos → binario. *(Limitación: UTF‑16/32 **sin** BOM contiene NUL → se trata como binario, igual que git.)*
- **Tamaño:** descartar si > 1 MB.
- **Implementación clave:** el filtro vive en la **lógica de `git add` de la herramienta** (se hace `git add` *solo* de los ficheros que pasan el filtro; **nunca** `git add -A`).
  - Ventaja: como **no** uso `.gitignore` para esto, si algún día quiero meter un binario o un fichero grande, basta con `git add <fichero>` a mano y commitear — la herramienta no me lo impide, simplemente no lo toca por su cuenta.
- Configurable: tamaño máximo y patrones de exclusión extra (p. ej. `node_modules/`, `.venv/`, `dist/`).
- **Smart Ignore (`suggest_excludes`, on por defecto):** el filtro reporta cada fichero rechazado (binario/grande, no un exclude del usuario) al motor, que los agrupa por carpeta de primer nivel. Cuando una carpeta acumula **≥ `NOISE_SUGGEST_THRESHOLD` (50)** ficheros distintos filtrados —casi siempre salida de build o caché— **sugiere una vez** (notificación + log) añadir `**/<carpeta>/**` a `extra_excludes`. Nunca auto-edita la config, salta como mucho una vez por carpeta por sesión, y solo cuenta ficheros *rechazados* (un refactor grande de texto pasa el filtro, así que no lo dispara). Caza el ruido que los excludes por defecto no cubren, sin dar la lata.
- **Lista de inclusión (`extra_includes`)**: patrones que se versionan **aunque sean binarios** (p. ej. `**/*.docx`), bajo un tope de tamaño aparte (`max_include_bytes`, 25 MB). Para `.docx` y similares, SincroGit mapea el fichero a un **driver de diff `textconv` con pandoc** en `.gitattributes` (versionado, viaja) e inyecta el comando textconv **en línea** (`git -c diff.pandoc.textconv=…`) en cada diff → diffs legibles (markdown) sin `git config` por máquina; alimenta los mensajes de IA y la time-machine. El `.docx` es la fuente de verdad; el markdown es una vista *lossy*. La ruta de pandoc es configurable (`pandoc_path`, por máquina); sin pandoc, degrada a versionar el blob opaco. **Consecuencia:** como la detección de cambios usa ese diff, un `.docx` se versiona/sincroniza **solo cuando su markdown cambia** (texto y formato estructural: negrita, encabezados, listas, tablas); la maquetación puramente visual (fuente/color/layout) y el ruido de reguardado de Word no disparan versión hasta que un cambio de contenido los arrastre.
- **`.pptx` (convert.py)**: mismo opt-in, conversor distinto — un **extractor in-process** sobre `python-pptx` (dependencia opcional; MIT, se empaqueta en el exe) convierte las diapositivas a markdown (títulos, viñetas con nivel de sangría, tablas, notas del orador) para previews/diffs/búsqueda de la GUI y "Save a copy". Deliberadamente NO es un driver textconv de git: eso requiere un ejecutable externo que git pueda lanzar (el papel de pandoc), y un entry point Python paga el arranque del intérprete por invocación — inaceptable dentro de `git diff`. Consecuencias: el diff que ve la IA trata el `.pptx` como binario (`--stat`), y la detección de cambios es por **bytes** (cada reguardado versiona), a diferencia del `.docx` con gating por markdown. La cadena pptx→docx→pandoc se evaluó y descartó: pandoc no lee pptx, así que necesitaría LibreOffice/COM de Office como tercera etapa — más pesado y menos determinista que leer el XML directamente.

---

## 6. Mensajes de commit con IA (modo híbrido)

Se generan al sellar (automático) y al hacer un **commit manual** (Smart Commit).

**Estrategia híbrida (elegida):**
1. Si **Ollama** está disponible localmente → usarlo (gratis, sin cuota, **el código no sale de la máquina**).
2. Si no → caer a un proveedor de **nube** (Gemini) con API key.
3. **Modo privacidad:** opción para enviar a la nube **solo `git diff --stat` + nombres de fichero** (no el contenido), para no exponer código sensible.
4. **Fallback siempre disponible:** si la IA falla (sin red, sin cuota, timeout) → mensaje automático determinista, p. ej.:
   `sincro: 4 file(s) (1 modified, 1 new, 1 deleted)`.
   **El commit/sellado nunca se bloquea por culpa de la IA.**

**Dos convenciones de prefijo (clave para distinguir máquina vs humano):**
- **Sellado automático → `sincro:`** (un *time-bucket*; no se finge clasificarlo como `feat`/`fix`). El prefijo marca el commit como "de la máquina".
- **Commit manual (Smart Commit) → Conventional Commits** (`feat:`/`fix:`/`docs:`/`refactor:`/…). El usuario lo dispara desde la GUI, la IA **propone** el mensaje (editable) y al confirmar se sella el WIP actual con él y se **reinicia el temporizador de 6 h**.

**Entrada al modelo:**
- *Sellado automático:* `git diff <árbol-HEAD> <árbol-snapshot> --stat` + diff troncado (la ventana sellada: árbol de HEAD → árbol del último snapshot) → mensaje `sincro:` conciso.
- *Commit manual:* diff **desde el último commit manual** (saltando los `sincro:`) hasta el WIP → la IA resume la *unidad de trabajo* completa. El commit solo contiene el delta del WIP, así que el cuerpo anota honestamente que es un **resumen acumulado** (parte del código está en sellados `sincro:` previos).

---

## 7. Arquitectura del software (Python)

```
sincrogit/
├─ sincrogit/
│  ├─ __main__.py        # entrypoint / CLI (tray, headless, --history, --autosnaps, ...)
│  ├─ config.py          # carga/valida config YAML
│  ├─ runtime.py         # config del exe, instancia única (+ handshake), consola
│  ├─ gitrepo.py         # wrapper de git (subprocess): snapshot/seal/autosnap/restore
│  ├─ watcher.py         # watchdog + debounce por repo (solo marca "dirty")
│  ├─ engine.py          # orquestador: tick snapshot 5min / seal 6h / autosnap 30min / sync
│  ├─ filefilter.py      # detección texto + tamaño
│  ├─ messages.py        # mensaje de commit de fallback (determinista)
│  ├─ ai.py              # proveedores IA (ollama/gemini) + fallback
│  ├─ events.py          # log estructurado (JSONL) para la GUI
│  ├─ log.py             # logging a fichero rotativo
│  ├─ notify.py          # notificaciones Windows (toasts)
│  ├─ convert.py         # extracción in-process de texto legible (.pptx vía python-pptx)
│  ├─ doctor.py          # chequeo de salud --doctor (git/remotos/credenciales/IA/demonio)
│  └─ gui/               # bandeja PyQt5 + panel + diálogos (add-repo, historial,
│                        #   explorador time-machine, máquinas, smart-commit, propiedades)
├─ tests/               # batería pytest (repos git desechables + Qt offscreen); `pytest`
├─ config.example.yaml
├─ pyproject.toml
└─ DISENO.md
```

**Librerías:**
- **`watchdog`** — eventos de sistema de ficheros.
- **git vía `subprocess`** (no GitPython) — control exacto de los snapshots por plumbing/`update-ref`/`rebase`, comportamiento transparente y predecible.
- **`urllib`** (stdlib) — llamadas a la IA de nube y al **Ollama** local por HTTP (sin dependencias extra).
- **`pyyaml`** — config.
- **`PyQt5`** — bandeja del sistema + panel de control (solo para `--tray`).
- **`logging`** (a fichero rotativo) + **`winotify`** — avisos (p. ej. "autosync pausado por conflicto").
- Scheduling: bucle propio con un *tick* y temporizadores por repo (sin dependencias).

**Decisión:** envolver el CLI de `git` con `subprocess` en vez de GitPython, porque las operaciones finas (snapshots por plumbing, cirugía de refs, rebase con política de conflicto) son más claras y robustas con el CLI.

---

## 8. Configuración (ejemplo)

```yaml
# config.yaml
defaults:
  snapshot_interval_sec: 300     # cada cuánto aterriza un snapshot en el ref lateral (5 min)
  debounce_sec: 25               # espera tras el último cambio antes de snapshot
  seal_interval_min: 360         # commit "real" + push cada 6h (timeline permanente)
  pull_interval_min: 10          # fetch cada 10 min; pull solo si hay algo nuevo
  autosnap: true                 # espejo en vivo del último snapshot a refs/autosnap/<user>/<host>/<rama>
  autosnap_interval_min: 30      # force-push del espejo cada 30 min (solo si cambió)
  max_file_bytes: 1048576        # 1 MB
  extra_excludes:                # además del filtro texto/tamaño
    - "**/node_modules/**"
    - "**/.venv/**"
    - "**/dist/**"

ai:
  mode: hybrid                   # hybrid | local | cloud | none
  cloud_provider: gemini         # elegido: Gemini
  cloud_model: gemini-2.5-flash-lite  # rápido y dentro del free tier
  cloud_send_content: false      # false => solo --stat + nombres (privacidad)
  # api_key vía variable de entorno SINCROGIT_GEMINI_KEY, NO en el fichero

repos:
  - path: "C:/repos/sincrogit"
    remote: origin
    branch: main
  - path: "C:/repos/foo"
    remote: origin
    branch: main
    seal_interval_min: 60        # override por repo
```

> La **API key nunca va en el YAML** → variable de entorno (`SINCROGIT_GEMINI_KEY`, etc.).
>
> *(Ejemplo abreviado — el juego completo de claves, comentado, vive en
> [config.example.yaml](config.example.yaml); el LEAME tiene la tabla de referencia.)*

---

## 9. Ejecución en background en Windows

**Sí, puede correr en segundo plano.** Opciones, de más simple a más "servicio":

| Opción | Cómo | Pros | Contras |
|--------|------|------|---------|
| **Tarea programada "al iniciar sesión"** ⭐ | Task Scheduler → trigger *At log on*, acción `pythonw.exe -m sincrogit --tray`, ventana oculta, reinicio automático | Corre **en tu sesión de usuario** → tiene acceso a tus **claves SSH / Credential Manager** para el push. Resiliente. | Solo corre con sesión iniciada (suficiente: solo editas logueado). |
| **`pythonw.exe` en carpeta Inicio** | Acceso directo en `shell:startup` | Lo más simple | Menos control de reinicio/logs. |
| **Servicio Windows real** (NSSM o `pywin32`) | NSSM envuelve el script como servicio | Arranca sin login | ⚠️ Corre como *LocalSystem* → **no ve tus claves SSH / credenciales de usuario** → el **push falla**. Habría que configurar credenciales a nivel de máquina. |

**Recomendación: Tarea programada "al iniciar sesión" con `pythonw.exe`** (sin consola). Es lo que mejor encaja porque:
- Solo necesitas que corra mientras trabajas (logueado).
- Hereda tus credenciales de git → el push funciona sin configurar nada raro.
- Task Scheduler da reinicio automático y arranque diferido.

`pythonw.exe` (en vez de `python.exe`) evita que aparezca una ventana de consola.

> **Estado:** este empaquetado es la pieza de la Fase 3 aún pendiente (§12). Hoy lanzas
> SincroGit tú mismo (doble clic / `--tray`) o con un acceso directo en `shell:startup`;
> lo de arriba es el plan para automatizarlo.

---

## 10. Recuperación ante fallos

| Escenario | Qué pasa | Cómo recupero |
|-----------|----------|---------------|
| **Corte de luz / crash de SO (disco intacto)** | Los ficheros guardados están en el disco; el último snapshot (≤5 min) está en el ref shadow `refs/sincro/wip/<rama>` | Nada que recuperar para los ficheros guardados (el disco los tiene). Para *revertir* un estado guardado malo: el historial de ficheros, o el reflog del ref shadow (resolución ≈5 min). Los buffers sin guardar son cosa de tu editor. |
| **"Quiero la versión de ayer"** | Está en los commits sellados | `git checkout`/`git restore` desde el sellado correspondiente. |
| **Borré algo hace 20 min (dentro de la ventana)** | Snapshot anterior quedó *unreachable* en reflog | `git reflog` + `git checkout`. *(Más cómodo con la rama `autosnap` opcional, §12.)* |
| **Fallo total de disco** | Lo sellado está en el remoto; el último estado (≤30 min) está en el ref `autosnap` (§4.1) | En otra máquina: *Fetch autosnaps* → restaurar (fichero o repo entero). Pérdida máx ≈ 30 min. Sin autosnap: hasta el último sellado (6 h). |
| **Conflicto al cambiar de máquina** | Rebase falla en el arranque | Autosync **se pausa** para ese repo + notificación; resuelvo a mano. Nunca se pierde nada. |

---

## 11. Casos borde y seguridad

- **Repo sin commits / sin remoto:** validar en el arranque y avisar; no romper.
- **Múltiples repos:** cada uno con su watcher/temporizadores independientes.
- **Privacidad del código en la nube:** por defecto en modo híbrido se prioriza Ollama (local); si cae a nube, `cloud_send_content: false` envía solo estadísticas. La API key vive en variable de entorno.
- **Operaciones git manuales mías** mientras corre el daemon (rebase, checkout de rama, etc.): la herramienta debe detectar `HEAD` cambiado/`rebase en curso`/índice ocupado y **ceder** (saltarse ese ciclo) en vez de pelearse. Detectar `.git/MERGE_HEAD`, `.git/rebase-*`, lock del índice. Mientras cede, las ediciones NO se están fotografiando — invisible desde el editor —, así que si la operación manual supera `BUSY_WARN_SEC` (10 min) avisa UNA vez (log + toast) de que los snapshots quedan pospuestos, y anota cuándo se reanudan. El umbral es lo bastante alto para que un merge normal — o el `index.lock` transitorio de cualquier comando git — nunca lo dispare.
- **Guarda de rama / seguir rama.** Por defecto, cuando HEAD no está en la `branch` configurada, el repo **cede** (sin snapshot/seal/autosnap/push en la rama equivocada) — `_ensure_on_branch`, rate-limited. Con **`track_current_branch: true`** en su lugar **sigue** la rama actual: cada operación con rama usa `st.active_branch` (la rama viva de HEAD) en vez de `cfg.branch`, así snapshot/autosnap/relevo/push ocurren en la rama en la que estés (cada rama tiene su `refs/autosnap/<user>/<host>/<rama>`, y el relevo solo casa la misma rama). HEAD desacoplado (detached) sigue cediendo. Se acopla con el modo purista (sin auto-seal → nada se auto-pushea donde no debe). Opt-in; el default mantiene el guard seguro.
- **El push apunta a HEAD** — seguro por construcción en el modelo shadow: la rama solo
  contiene sellados y commits del usuario (los snapshots viven en el ref lateral). Un
  commit manual del usuario es simplemente… un commit; viaja en el siguiente push como
  lo haría un sellado.
- **Las restauraciones nunca destruyen trabajo sin fotografiar.** Antes de que
  `restore_file`/`restore_repo` sobrescriban nada, las ediciones pendientes se capturan
  en un snapshot shadow — lo guardado desde el último snapshot no existe en ningún otro
  sitio. Las restauraciones escriben SOLO en el worktree (`git restore --worktree` /
  borrados planos): el índice del usuario sigue siendo suyo, la restauración aparece en
  su `git status` como ediciones normales, y un snapshot de cierre la captura. El
  contenido que esa pasada de captura *no puede* tomar (el filtro lo rechaza — excluido /
  sobre el límite de tamaño / binario — o está sin trackear y el árbol destino trae otra
  versión, `untracked_collisions`) hace que la restauración se **niegue** allí donde lo
  TOCARÍA, nombrando los ficheros a copiar a un lugar seguro primero: la misma política
  que el apply del relevo, porque ese contenido no existe en ningún sitio de git. Las
  restauraciones respetan además la guarda de rama y el chequeo de ocupado, como toda
  operación manual — fuera de rama la captura fotografiaría la cadena de otra rama, y en
  mitad de un merge/rebase pisarían un árbol en conflicto.
- **Instancia única (que dos demonios no compitan por git).** La guarda autoritativa es un
  mutex con nombre de Windows (`acquire_instance_mutex`; sin lock-huérfano —el SO lo libera
  al morir el proceso— y no se lo puede robar una app que ocupe el puerto). El puerto local
  (29677, deliberadamente por debajo del rango efímero de Windows) hace además de pequeño
  canal de comandos: `show` (traer el panel al frente), `ping` (sonda de presencia para la
  guarda de los disparos únicos) y `flushquit` (volcar todos los repos —snapshot + push de
  autosnap— y salir limpiamente; `build.ps1` lo usa para recompilar el mismo exe que está
  corriendo sin perder trabajo, con kill forzado como fallback para demonios antiguos, y lo
  relanza después). Si un ajeno ocupa el puerto, la instancia única sigue garantizada por el
  mutex (solo perdemos ese canal IPC). La guarda aplica a la bandeja **y** a `--headless`
  (un segundo demonio competiría por git en los mismos repos): un segundo lanzamiento de
  la bandeja simplemente activa el panel en marcha y sale con 0; un segundo `--headless`
  rehúsa arrancar, código de salida 2. Un demonio headless sigue respondiendo el handshake
  de activación, así un lanzamiento posterior de la bandeja lo detecta y se retira. Los
  disparos únicos de CLI consultan la misma guarda (ping de presencia sin efectos
  secundarios) y rehúsan correr
  junto a un demonio vivo — la misma carrera — salvo con `--force`.
- **Carga del watcher.** El handler de watchdog descarta eventos de los internos de `.git`
  **y** de rutas que casan los excludes del repo (`FileFilter.is_excluded`, un chequeo de
  pathspec barato, sin tocar disco) — así una ráfaga tipo `npm install` bajo `node_modules/`
  nunca despierta al motor. Complementa a *Smart Ignore* (que sugiere añadir esas carpetas a
  `extra_excludes`).
- **Sin procesos git huérfanos en timeout.** `_run` usa `Popen` + `communicate(timeout=)`, y
  ante un timeout mata el **árbol de procesos entero** (`taskkill /F /T` en Windows), no solo
  `git.exe` — si no, sus hijos (`ssh.exe`, `git-remote-https.exe`) quedarían huérfanos
  reteniendo la conexión/locks. El stdin es siempre una tubería cerrada, así que un prompt de
  credenciales colgado recibe EOF (con `GIT_TERMINAL_PROMPT=0`) en vez de bloquear.
- **Sin secretos en los logs.** La API key de la nube va en el header `x-goog-api-key`, nunca
  en la URL (un error de urllib suele serializar la URL); además el log de fallo de IA redacta
  el valor de la key por defensa en profundidad.
- **Degradación elegante sin `watchdog`.** Si falta la librería del watcher, el demonio sigue
  corriendo (GUI, snapshot/commit manual, sync, máquina del tiempo) con un aviso claro en vez
  de crashear — solo se apaga la detección automática de cambios.
- **El motor nunca muere en silencio.** Un fallo al *lanzar* git siquiera (p. ej. la carpeta
  del repo desapareció: disco USB desenchufado, carpeta de nube movida) aflora como
  `GitError`, así que el manejo por-repo salta ese repo — en el arranque y en cada ciclo —
  y los demás siguen corriendo. Si aun así el loop choca con un error inesperado, se hace
  visible en vez de dejar un icono zombi: log con traceback, evento ERROR (globo de bandeja
  + toast), `status()` reporta parado (icono gris), y `--headless` sale con código 1.
- **Auto-reparación de refs tras un corte de luz.** Un crash puede dejar `.git/HEAD` o
  `refs/heads/<rama>` a ceros (NTFS conserva el tamaño del fichero, pierde la última
  escritura pequeña) — git dice entonces "your current branch appears to be broken" y el
  repo cedería para siempre. En el setup, `GitRepo.repair_corrupt_refs` detecta una ref que
  no resuelve y la restaura desde la entrada más nueva de **su propio reflog** cuyo commit
  siga existiendo (el reflog es append-only y sobrevive al crash) — la recuperación manual,
  automatizada. Conservador: solo toca refs irresolubles, nunca adivina entre ramas, emite
  un evento "repair" de WARNING. (Nacido de un incidente real: un corte de luz zeroeó el
  `refs/heads/main` de este mismo repo.)
- **Nunca `--force`** en el flujo automático.
- **Mantenimiento:** `git gc --auto` tras cada sello **y al menos una vez al día**
  (`Engine.GC_INTERVAL_SEC`, en un worker en segundo plano), para empaquetar los objetos
  sueltos que deja la cadena de snapshots. El disparador diario está **desacoplado del sellado a
  propósito**: en modo purista (`seal_interval_min: inf`) el sello no se dispara nunca, así
  que sin él un WIP de larga vida acumularía objetos sueltos sin límite. El mismo worker
  diario también **poda los refs autosnap rancios de esta máquina** en el remoto — refs
  cuya rama ya no existe localmente y cuyo espejo tiene ≥ 7 días. Refs de escritor único,
  así que el borrado no compite con nadie; los estados de recuperación de otras máquinas
  no se tocan nunca, y la guarda de edad evita que un repo recién re-clonado (recuperación
  de desastre) pode estados que aún no ha recreado.
- **Desactivar intervalos/límites:** cualquier intervalo (`*_interval_*`, `debounce_sec`)
  o umbral de tamaño (`max_file_bytes`, `max_include_bytes`) acepta un *centinela de
  desactivación* (`inf`/`off`/`none`/`never`, también `None`/`False`), normalizado a
  `math.inf` en `RepoConfig.__post_init__`. `inf` fluye por la aritmética de plazos sin
  tocar nada (un plazo de `inf` nunca se alcanza; `min(x, inf) == x`), así que el motor no
  necesita ningún caso especial. El uso estrella es el **modo purista**
  (`seal_interval_min: inf`): sin sellado automático — el historial permanente lo
  construye solo el **Smart Commit** manual, mientras el WIP + `autosnap` siguen dando la
  red de seguridad. (YAML solo entiende `.inf` como float; un `inf`/`off` pelado llega como
  string/bool, de ahí la normalización.)
- **Recordatorio de commit purista (`suggest_commit`, activado por defecto).** La única
  trampa del modo purista es una rama que se estanca en silencio si el usuario olvida el
  Smart Commit (el trabajo está a salvo en el WIP/autosnap, pero no EN la rama — fácil de
  confundir con "ya está subido"). El motor avisa (notificación + log) cuando se cumple
  TODO: modo purista, hay trabajo sin sellar, el repo lleva ~20 min **en calma** (el proxy
  de "terminaste algo" — por estado, no una alarma de reloj), el último commit permanente
  tiene >1 día, y no se avisó en el último día. Sellar reinicia la puerta de antigüedad,
  así que commitear lo silencia solo. Constantes: `Engine.COMMIT_NUDGE_*`; no-op con
  auto-sellado activo.

---

## 12. Roadmap por fases

**✅ Fase 1 — MVP (historiador local automático) — COMPLETA:**
- Config + validación de repos.
- Watcher + debounce + snapshot (ref lateral shadow) cada 5 min (+ snapshot inicial al arrancar).
- Filtro texto/tamaño.
- Sellado cada 6 h con **mensaje de fallback**.
- Logging.
> Con esto ya tengo una máquina del tiempo versionada, que es el 80 % del valor.

**✅ Fase 2 — IA + sincronización remota — COMPLETA:**
- Generador de mensajes IA híbrido (Ollama → Gemini → fallback). Nunca bloquea el sellado.
- Push de sellados (refspec con SHA → `refs/heads/<branch>`) + reintento en cada sync.
- `fetch` + pull con rebase del WIP, solo si el remoto adelanta; sync inicial al arrancar.
- Política de conflicto: abortar rebase + pausar repo + notificar. *(Superado después:
  ambas formas de conflicto están ahora cubiertas por la batería automatizada sobre
  remotos bare desechables — ver la sección técnica pendiente abajo y
  `tests/test_multi_machine.py`.)*

**✅ Fase 4 — Interfaz de bandeja (PyQt5) — COMPLETA:**
- Icono en la bandeja del sistema (una "G" con reloj de arena, dibujado vectorial)
  cuyo **color refleja el estado** (activo/pausado/conflicto/parado).
- Menú: abrir panel, pausar/reanudar, sincronizar ahora, sellar ahora, salir.
- Panel de control con pestañas Status / Log (filtrable por repo, acción,
  nivel, texto) / Settings (un formulario sobre los defaults) / Advanced (el editor YAML crudo).
- Registro estructurado de eventos (`events.jsonl`) + notificaciones de escritorio.
- Arquitectura: motor en hilo de fondo, GUI en el hilo principal, comunicación por
  señales Qt; acciones manuales serializadas con un lock en el motor.
- Arranque: `python -m sincrogit --tray` (o `pythonw` sin consola).

**✅ Historial de fichero / restore ("máquina del tiempo") — COMPLETA:**
- Navegar las versiones pasadas de un fichero, fusionando el historial alcanzable
  (commits sellados, permanentes) y el reflog (snapshots intra-ventana, ~30 días),
  colapsando contenidos idénticos.
- Previsualizar cualquier versión y restaurarla (`git checkout <sha> -- fichero`);
  la restauración se convierte en un snapshot nuevo, así que queda versionada a su vez.
- CLI: `--history FICHERO` (interactivo) / `--history FICHERO --pick N` (no interactivo).
- GUI: panel de control → la pestaña Time machine (fija un fichero para su historial).

**Fase 3 — Despliegue (parcial):**
- ✅ `SincroGit.exe` autónomo de un solo fichero (GUI + CLI) vía PyInstaller
  (`--onefile --noconsole`); la salida CLI se engancha a la terminal que lo lanza.
- ✅ Lock de instancia única (socket localhost, sin lock huérfano): un segundo
  lanzamiento pide al que corre que muestre su panel y sale. *(Superado después: la
  guarda autoritativa es ahora un mutex con nombre de Windows, compartido por bandeja,
  headless y los disparos únicos de CLI; el puerto queda como canal de activación/ping.
  Ver §11.)*
- ✅ Resolución de config: junto al .exe → `%APPDATA%\SincroGit\` → cwd; en el primer
  arranque se crea una por defecto.
- ⏳ Pendiente: tarea programada al iniciar sesión que auto-arranque `SincroGit.exe` —
  sin ella, la promesa de "cero disciplina" depende de acordarse de lanzar la herramienta.
- ⏳ Pendiente: comando/pestaña `status` (el atajo "sellar+push ahora" ya está en el menú).
- ✅ Chequeo de salud `sincrogit doctor` (git, config, rama/remoto de cada repo,
  accesibilidad de lectura + credenciales de push, pandoc, backends de IA, demonio) —
  `--doctor`, con su propia batería (`tests/test_doctor.py`).
- ⏳ Pendiente: onboarding guiado en "Add repo" (crear/conectar un remoto privado y
  verificarlo con un push de prueba, desde la GUI) — para el público sin Git, el montaje
  de remoto/credenciales es la barrera de entrada real, no el demonio.

**Pendiente — técnico (sin feature visible para el usuario):**
- ⏳ Batería de tests automatizados — **existe** (`tests/`, pytest, 150+ tests): rechazos
  de restauración y restore seguro ante renames, restore selectivo, línea temporal,
  exportar, búsqueda en historial, cirugía de config, `--doctor`, aviso de ocupado,
  precedencia de estados, renderizado de diffs, diálogos de la GUI en offscreen — más
  los **caminos multi-máquina sobre remotos bare desechables**: clasificación de
  `work_relationship` (los cuatro veredictos), fast-forward del relevo
  (auto/ask/re-validación), el rechazo por contenido no capturable, el relevo a través
  de un rename, las dos formas de conflicto de rebase, el bucle de reconciliación tras
  un push rechazado, idempotencia de sellado/push y poda de refs autosnap. Sigue
  pendiente: CI en cada push.

**Opcional / futuro:**
- Rama `autosnap` con commits reales cada 5 min (historial intra-ventana navegable *en el remoto*) en lugar del espejo force-push del último estado.
- Variante "espejo en vivo" (force-with-lease del WIP) si el backup remoto en tiempo real se vuelve necesario.
- Tanda de IA, inspirada en aicommit2 (contratos intactos: nunca bloquear el sellado,
  privacidad por defecto, solo `urllib` estándar): **endpoint genérico
  OpenAI-compatible** (`ai.cloud_provider: compatible` + `ai.cloud_url`) que cubre
  OpenRouter/DeepSeek/LM Studio/Anthropic/… con un único cliente (las keys siguen en
  variables de entorno); **mensajes en el idioma del usuario — ya hecho como
  `ai.language`** (`en`|`es`: con `ai.language: es` los mensajes salen en español;
  falta solo generalizarlo a locales arbitrarios);
  **overrides de `ai:` por repo** (p. ej. un repo sensible fijado a `mode: local`).
  Ver el LEAME → TODO.
- Tanda inspirada en lazygit (lazygit es el complemento, no un donante — no se
  reconstruye un cliente git en el panel): **Smart Commit parcial** (lista de ficheros
  con checkboxes — commitea la selección y devuelve el resto al WIP recreado;
  `commit_prefix` opcional desde el nombre de la rama), y una **receta de convivencia**
  solo-docs (`customCommands` de lazygit invocando `--commit`/`--apply-handoff`, más el
  aviso de "no rewordees el WIP" para clientes git). Ver el LEAME → TODO. *(Superado en
  parte después: la nota de convivencia — reglas del WIP, GitButler — es el Manual §9;
  los snippets de `customCommands` siguen pendientes.)*

---

## 13. Decisiones tomadas

- ✅ Modelo **WIP+amend → seal cada 6h** + **autosnap** (espejo en vivo) cada 30 min.
  *(Superado después por el modelo SHADOW de la v0.2, abajo — mismos ritmos, los
  snapshots salen de la punta del usuario.)*
- ✅ **v0.2: snapshots shadow** (`refs/sincro/wip/<rama>` + índice privado) en vez de un
  commit WIP en HEAD. Motivación: el WIP en la punta confundía a toda herramienta git y
  secuestraba `git status`/staging. Consecuencias aceptadas: un
  `core.logAllRefUpdates=always` local por repo (los refs laterales no tienen reflog por
  defecto — y ES la ventana de recuperación), y el pull ahora autostashea el worktree
  sucio (un pop conflictivo pausa el repo con marcadores; el snapshot pre-pull garantiza
  la recuperación). Validado por adelantado con tres spikes: el gating por textconv
  funciona árbol-contra-árbol, el índice privado cuesta ~120 ms caliente con 2 000
  ficheros, y un pop de autostash conflictivo NO deja el repo mid-rebase (se detecta por
  entradas unmerged, no por is_busy).
- ✅ **Intervalos: snapshot cada 5 min, sellado cada 6 h, autosnap cada 30 min.**
- ✅ Push **solo de sellados** (los snapshots quedan en el ref lateral; pull siempre limpio; sin force-push).
- ✅ IA **híbrida** (Ollama local → nube fallback; opción de enviar solo stats).
- ✅ **Prefijos:** sellado automático `sincro:`; **commit manual (Smart Commit)** con mensaje Conventional Commits propuesto por IA (resumen acumulado desde el último manual) + reset del temporizador.
- ✅ **Proveedor de nube: Gemini** (`gemini-2.5-flash-lite`), API key en variable de entorno.
- ✅ Filtro: **solo texto < 1 MB**; binarios/grandes a mano.
- ✅ **Python**; git vía subprocess; `watchdog`; **PyQt5** para la UI de bandeja.
- ✅ Background: **Tarea programada al iniciar sesión** con `pythonw.exe`. *(Decisión
  tomada; implementarla es el punto pendiente de la Fase 3 — §9, §12.)*
- ✅ Rama de trabajo: **`main`** (confirmar por repo).
- ✅ **Sellado cada 6 h** (timeline permanente grueso); un sellado manual (*Seal now* / `--seal-once` / Smart Commit) para el handoff por la vía limpia.
- ✅ **Pull periódico cada 10 min** (`fetch` + pull solo si el remoto tiene commits nuevos), además del pull de arranque.
- ✅ **Autosnap** (espejo en vivo del **shadow tip** — el último snapshot, incl. el WIP vivo — a `refs/autosnap/<user>/<host>/<rama>`, force-push cada 30 min, solo si cambió): RPO de fallo de disco ≈ 30 min, recuperación cross-machine por fichero o repo entero (CLI `--autosnaps` + GUI). La variante "historial fino navegable en el remoto" (un commit por snapshot) sigue diferida.

## 14. Cómo configurar la API key de Gemini

1. Obtener una API key gratuita en **Google AI Studio** (`aistudio.google.com`) → *Get API key*.
2. Guardarla como **variable de entorno de usuario** en Windows (no en el YAML, no en el repo):
   ```powershell
   setx SINCROGIT_GEMINI_KEY "tu_api_key_aqui"
   ```
   (Cierra y reabre la terminal/sesión para que la variable esté disponible.)
3. La herramienta la lee de `os.environ["SINCROGIT_GEMINI_KEY"]`.
4. Recuerda: en modo híbrido se intentará **Ollama local primero**; Gemini solo entra si Ollama no está. Y con `cloud_send_content: false` a Gemini solo le llegan nombres de fichero + `--stat`, no el contenido.

## 15. Preguntas abiertas

*(Ninguna pendiente — diseño cerrado.)*
