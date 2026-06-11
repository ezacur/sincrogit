# SincroGit — Documento de diseño

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

## 2. Modelo conceptual: dos niveles

El truco para conciliar *"snapshot casi instantáneo"* con *"no quiero miles de commits"* es separar dos niveles:

| Nivel | Qué es | Frecuencia | Visible en historial |
|-------|--------|-----------|----------------------|
| **WIP (snapshot)** | Un **único** commit en la punta (`HEAD`) que se **amend**ea con el estado actual | Cada ~5 min (con debounce) | No (es transitorio, se sella o se reescribe) |
| **Sellado (historia)** | El WIP se "congela" con un mensaje IA descriptivo y se crea un WIP nuevo encima | Cada ~6 h | Sí (commit permanente) |

```
... ── sellado_N ── WIP        ← HEAD (se amendea cada ~5 min)
                     │
       cada 6h ──────┘ se sella (reword con mensaje IA) y nace un WIP nuevo encima

resultado: ... ── sellado_N ── sellado_N+1 ── WIP(nuevo) ← HEAD
```

**Por qué funciona:**

- El estado guardado actual se commitea cada ~5 min → un **punto de rollback** con resolución ~5 min (`HEAD` = último snapshot, los anteriores en el reflog). OJO: esto *no* es protección ante cortes de luz — los ficheros guardados sobreviven al corte en el disco igualmente, y los buffers no guardados nunca se capturan; el valor es la máquina del tiempo.
- Como se hace `amend`, no se acumulan cientos de commits: solo **~4 commits/día** (uno cada 6 h).
- El historial "limpio" (sellados) es lo único que viaja al remoto → **pull siempre limpio, sin force-push** (ver §4).

> **Red de seguridad fina:** cada `amend` deja el snapshot anterior como commit *unreachable* en el **reflog** (≈30 días por defecto). Es decir, aunque el historial visible solo tenga 1 commit por ventana, internamente puedes recuperar estados intermedios con `git reflog`. *(Opcional, ver §12: una rama `autosnap` con commits reales cada ~5 min si quieres historial intra-ventana navegable.)*

---

## 3. Flujo detallado

### 3.1 Arranque del servicio (por repo)
1. Validar que es repo git (una carpeta ausente o inválida salta ese repo; los demás
   siguen). El remoto se comprueba perezosamente en cada sync (`has_remote`); estar en la
   rama configurada es trabajo de la guarda de rama (§11).
2. Asegurar que existe un **WIP** en la punta (si no, crear uno vacío) y registrar el
   repo en el watcher.
3. **Snapshot inicial**, antes de tocar la red: captura los cambios previos a esta
   ejecución (p. ej. ediciones hechas con SincroGit apagado, o tras un reinicio).
4. **Sync inicial en un hilo de fondo** — una red lenta nunca retrasa la red de
   seguridad local: `fetch` + (solo si el remoto adelanta) rebase del WIP encima
   (`--autostash`), exactamente como el pull periódico (§3.4).
   - **Si hay conflicto** → `git rebase --abort`, se **pausa el autosync de ese repo**, se notifica al usuario y se registra en log. **Nunca** se resuelve de forma destructiva ni se hace force. (Esto es raro en uso secuencial, pero la política es: ante la duda, no perder datos.)

### 3.2 Ciclo de snapshot (cada ~5 min, con debounce)
- El **watcher** (eventos del sistema de ficheros) marca el repo como *dirty* y reinicia un debounce (p. ej. 20-30 s sin cambios).
- Cuando el debounce se asienta **y** ha pasado ≥5 min desde el último snapshot:
  1. Calcular ficheros candidatos y aplicar el **filtro** (§5).
  2. `git add <solo los candidatos>`.
  3. Si hay algo staged: `git commit --amend --no-edit` (mensaje WIP estático tipo `sincro: WIP autosnapshot`).
- Sin cambios → no se hace nada.
- **Anti-inanición:** una fuente que nunca se asienta (un build largo, un log que escribe
  dentro del repo) reinicia el debounce sin parar — así que pasadas **2× el intervalo de
  snapshot** desde el último, se toma uno igualmente, con o sin debounce
  (`Engine.SNAPSHOT_STARVATION_FACTOR`). `debounce_sec: inf` conserva su significado de
  "no disparar nunca".

### 3.3 Sellado (cada 6 h)
**Único disparador automático:** temporizador de **6 h desde el último sellado**.

1. Si el WIP no tiene cambios respecto a `sellado_N` → **no sellar** (no ensuciar el historial).
2. Generar mensaje con IA a partir de `git diff sellado_N..WIP` (§6).
3. `git commit --amend -m "<mensaje IA>"` → el WIP pasa a ser `sellado_N+1`.
4. Crear WIP nuevo vacío encima: `git commit --allow-empty -m "sincro: WIP autosnapshot"`.
5. **Push** (§4).

> No hay sellado por inactividad ni por apagado. Para forzar un sellado+push puntual (p. ej. justo antes de irme al portátil): *Seal now* / *Seal+Push* por repo en la bandeja, `--seal-once` desde la CLI, o un Smart Commit.

### 3.4 Pull periódico (cada 10 min)
Además del pull de arranque (§3.1), el demonio comprueba el remoto cada **10 min** para traer lo que dejó la otra máquina, **sin** que tenga que reiniciar sesión ni pullear a mano.

1. **`git fetch`** (barato; no toca el árbol de trabajo).
2. Comprobar si el remoto tiene commits nuevos:
   `git rev-list --count HEAD..<remote>/<branch>`.
   - Si es **0** → no hay nada que traer → **no se hace nada** (caso habitual mientras trabajo en esta máquina).
   - Si es **> 0** → **`git pull --rebase --autostash`** (rebasa mi WIP local encima de lo nuevo).
3. **Conflicto en el rebase** → `git rebase --abort`, **pausar autosync de ese repo + notificar**; resolver a mano. Nunca force, nunca pérdida de datos.

> Como el uso es **secuencial** (nunca las dos máquinas a la vez), mientras trabajo en una, la otra no pushea → el paso 2 da 0 y el pull no se dispara. Al sentarme en la otra máquina, en ≤10 min coge sola lo que sellé en la primera.

---

## 4. Push y multi-máquina

**Regla de oro: solo se pushean commits sellados; el WIP nunca sale de la máquina.**

- Push: empujar el **último commit sellado** — el commit no-WIP más reciente, resuelto por
  mensaje y no por el `HEAD~1` posicional (ver §11) — nunca el WIP vivo:
  `git push origin <sha-sellado>:refs/heads/<rama>` → así el remoto recibe historia inmutable y el WIP local se queda por delante.
- Como los sellados son inmutables y nunca se reescriben, **el push es siempre fast-forward** y el **pull de la otra máquina es siempre limpio**. No hace falta force-push en ningún caso del flujo normal.

**Handoff entre máquinas (uso secuencial):**

```
Sobremesa: trabaja → cada 6h (o un Seal now / Smart Commit manual) sella + push  ──►  remoto al día
Portátil:  arranca → pull --rebase (limpio) → trabaja → sella + push ──► remoto al día
Sobremesa: arranca → pull --rebase (limpio) → continúa...
```

**Handoff normal (rama limpia):** el portátil hace `pull --rebase` de la rama y arranca con lo **sellado** (hasta 6 h atrás). Para un handoff a media ventana, lanza un sellado manual (**Seal now** / `--seal-once` / Smart Commit) antes de levantarte y el portátil arrancará con todo por la vía limpia.

### 4.1 Autosnap (espejo en vivo) — recuperación ante desastre

Como sellar cada 6 h dejaría hasta 6 h de trabajo fuera del remoto, **autosnap** desacopla el *backup remoto* del *historial*: cada **30 min** (y solo si hubo cambios) se hace `push --force` de `HEAD` (sellados **+ el WIP vivo**) a un ref lateral **por usuario y máquina** `refs/autosnap/<user>/<host>/<rama>` (el namespace se detalla en §4.2).

- **No ensucia la rama:** nadie pullea ese ref para trabajar; la rama `main` sigue recibiendo solo sellados → pull siempre limpio. Es la excepción deliberada a "el WIP no sale de la máquina", acotada a un ref de backup.
- **RPO ante fallo total de disco ≈ 30 min** (en vez de 6 h). En la otra máquina: *Fetch autosnaps* → explorar/restaurar el último estado (fichero o repo entero).
- **Coste:** hasta ~48 push/día/repo en trabajo activo (force-push barato; **nada** en repos inactivos, porque solo sube si HEAD cambió). Objetos huérfanos en el remoto hasta su GC.
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
- **Comparar por CONTENIDO de trabajo, no por ancestría.** Sutileza clave: el WIP se
  *amenda* continuamente, así que en cuanto una máquina adopta el WIP de otra y edita, su
  nuevo WIP es *hermano* del del peer (mismo padre = el sello base), nunca descendiente — la
  ancestría reportaría divergencia constantemente. En su lugar
  `GitRepo.work_relationship(mine, theirs)` compara, respecto al merge base, las *rutas que
  cambió cada lado*: si `theirs` coincide con `mine` en toda ruta que yo cambié (y tiene
  más) es `theirs_contains` → seguro adoptar; si no, se clasifica `equal` / `mine_contains`
  / `diverged`.

Comportamiento — `live_handoff` es un mando de 3 estados (`auto` por defecto | `ask` | `off`):
- **`theirs_contains` → fast-forward seguro** (`git reset --hard` al peer): demostrablemente
  sin pérdida (el peer tiene todo mi contenido de las rutas que cambié; solo se descarta un
  WIP vacío), reversible vía reflog, y se **rechaza (con notificación)** si pisara un fichero
  untracked (`untracked_collisions`) **o** si hay ediciones locales que el snapshot no pudo
  capturar (`modified_unstaged`: un fichero rastreado que creció más del límite, se volvió
  binario o casa un exclude — esas ediciones no existen en ningún sitio de git, ni siquiera
  el reflog, así que el reset las destruiría). En `auto` se aplica al momento **y se lanza una notificación de
  bandeja** (el nivel b nunca es *silencioso* — que el working tree cambie bajo tus pies
  sorprende aunque no se pierda nada). En `ask` NO se aplica: se registra el candidato
  (`pending_handoff`, expuesto en `status()` y el panel), se notifica, y un **Apply** de un
  clic (`Engine.apply_handoff` / `--apply-handoff`) revalida desde cero (re-fetch +
  re-clasificar + re-chequear colisiones, porque el peer pudo moverse) antes del
  fast-forward (nivel a / consentimiento).
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
- *Sellado automático:* `git diff sellado_N..WIP --stat` + diff troncado → mensaje `sincro:` conciso.
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
│  └─ gui/               # bandeja PyQt5 + panel + diálogos (add-repo, historial)
├─ config.example.yaml
├─ pyproject.toml
└─ DISENO.md
```

**Librerías:**
- **`watchdog`** — eventos de sistema de ficheros.
- **git vía `subprocess`** (no GitPython) — control exacto de `amend`/`push HEAD~1`/`rebase`, comportamiento transparente y predecible.
- **`urllib`** (stdlib) — llamadas a la IA de nube y al **Ollama** local por HTTP (sin dependencias extra).
- **`pyyaml`** — config.
- **`PyQt5`** — bandeja del sistema + panel de control (solo para `--tray`).
- **`logging`** (a fichero rotativo) + **`winotify`** — avisos (p. ej. "autosync pausado por conflicto").
- Scheduling: bucle propio con un *tick* y temporizadores por repo (sin dependencias).

**Decisión:** envolver el CLI de `git` con `subprocess` en vez de GitPython, porque las operaciones finas (amend continuo, push de `HEAD~1`, rebase con política de conflicto) son más claras y robustas con el CLI.

---

## 8. Configuración (ejemplo)

```yaml
# config.yaml
defaults:
  snapshot_interval_sec: 300     # cada cuánto se amendea el WIP (5 min)
  debounce_sec: 25               # espera tras el último cambio antes de snapshot
  seal_interval_min: 360         # commit "real" + push cada 6h (timeline permanente)
  pull_interval_min: 10          # fetch cada 10 min; pull solo si hay algo nuevo
  autosnap: true                 # espejo en vivo de HEAD a refs/autosnap/<user>/<host>/<rama>
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
| **Tarea programada "al iniciar sesión"** ⭐ | Task Scheduler → trigger *At log on*, acción `pythonw.exe -m sincrogit`, ventana oculta, reinicio automático | Corre **en tu sesión de usuario** → tiene acceso a tus **claves SSH / Credential Manager** para el push. Resiliente. | Solo corre con sesión iniciada (suficiente: solo editas logueado). |
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
| **Corte de luz / crash de SO (disco intacto)** | Los ficheros guardados están en el disco; el último snapshot (≤5 min) está en `HEAD` (WIP) | Nada que recuperar para los ficheros guardados (el disco los tiene). Para *revertir* un estado guardado malo: `git reflog` (resolución ≈5 min). Los buffers sin guardar son cosa de tu editor. |
| **"Quiero la versión de ayer"** | Está en los commits sellados | `git checkout`/`git restore` desde el sellado correspondiente. |
| **Borré algo hace 20 min (dentro de la ventana)** | Snapshot anterior quedó *unreachable* en reflog | `git reflog` + `git checkout`. *(Más cómodo con la rama `autosnap` opcional, §12.)* |
| **Fallo total de disco** | Lo sellado está en el remoto; el último estado (≤30 min) está en el ref `autosnap` (§4.1) | En otra máquina: *Fetch autosnaps* → restaurar (fichero o repo entero). Pérdida máx ≈ 30 min. Sin autosnap: hasta el último sellado (6 h). |
| **Conflicto al cambiar de máquina** | Rebase falla en el arranque | Autosync **se pausa** para ese repo + notificación; resuelvo a mano. Nunca se pierde nada. |

---

## 11. Casos borde y seguridad

- **Repo sin commits / sin remoto:** validar en el arranque y avisar; no romper.
- **Múltiples repos:** cada uno con su watcher/temporizadores independientes.
- **Privacidad del código en la nube:** por defecto en modo híbrido se prioriza Ollama (local); si cae a nube, `cloud_send_content: false` envía solo estadísticas. La API key vive en variable de entorno.
- **Operaciones git manuales mías** mientras corre el daemon (rebase, checkout de rama, etc.): la herramienta debe detectar `HEAD` cambiado/`rebase en curso`/índice ocupado y **ceder** (saltarse ese ciclo) en vez de pelearse. Detectar `.git/MERGE_HEAD`, `.git/rebase-*`, lock del índice.
- **Guarda de rama / seguir rama.** Por defecto, cuando HEAD no está en la `branch` configurada, el repo **cede** (sin snapshot/seal/autosnap/push en la rama equivocada) — `_ensure_on_branch`, rate-limited. Con **`track_current_branch: true`** en su lugar **sigue** la rama actual: cada operación con rama usa `st.active_branch` (la rama viva de HEAD) en vez de `cfg.branch`, así snapshot/autosnap/relevo/push ocurren en la rama en la que estés (cada rama tiene su `refs/autosnap/<user>/<host>/<rama>`, y el relevo solo casa la misma rama). HEAD desacoplado (detached) sigue cediendo. Se acopla con el modo purista (sin auto-seal → nada se auto-pushea donde no debe). Opt-in; el default mantiene el guard seguro.
- **El push apunta al último commit no-WIP** (resuelto por mensaje, no el `HEAD~1`
  posicional): si el usuario commitea a mano encima del WIP, su commit es lo que se sube —
  nunca el WIP transitorio. El reloj de sellado también se resetea al detectar un commit
  externo (se respeta como sello manual).
- **Las restauraciones nunca destruyen trabajo sin fotografiar.** Antes de que
  `restore_file`/`restore_repo` sobrescriban nada, las ediciones pendientes se capturan
  en el WIP (el mismo stage+amend que hace el relevo) — lo guardado desde el último
  snapshot no existe en ningún otro sitio, ni siquiera el reflog. El amend del WIP es
  `--allow-empty`: revertir a mano al contenido sellado, o restaurar el repo entero al
  último sellado, vacía legítimamente el WIP y no debe fallar. Las restauraciones
  respetan además la guarda de rama y el chequeo de ocupado, como toda operación
  manual — fuera de rama la captura amendearía el WIP de la rama equivocada, y en
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
  huérfanos que dejan los amends. El disparador diario está **desacoplado del sellado a
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

---

## 12. Roadmap por fases

**✅ Fase 1 — MVP (historiador local automático) — COMPLETA:**
- Config + validación de repos.
- Watcher + debounce + snapshot (amend) cada 5 min (+ snapshot inicial al arrancar).
- Filtro texto/tamaño.
- Sellado cada 6 h con **mensaje de fallback**.
- Logging.
> Con esto ya tengo una máquina del tiempo versionada, que es el 80 % del valor.

**✅ Fase 2 — IA + sincronización remota — COMPLETA:**
- Generador de mensajes IA híbrido (Ollama → Gemini → fallback). Nunca bloquea el sellado.
- Push de sellados (refspec con SHA → `refs/heads/<branch>`) + reintento en cada sync.
- `fetch` + pull con rebase del WIP, solo si el remoto adelanta; sync inicial al arrancar.
- Política de conflicto: abortar rebase + pausar repo + notificar *(verificado a mano —
  una batería de tests automatizados sigue pendiente; ver el TODO técnico abajo)*.

**✅ Fase 4 — Interfaz de bandeja (PyQt5) — COMPLETA:**
- Icono en la bandeja del sistema (una "G" con reloj de arena, dibujado vectorial)
  cuyo **color refleja el estado** (activo/pausado/conflicto/parado).
- Menú: abrir panel, pausar/reanudar, sincronizar ahora, sellar ahora, salir.
- Panel de control con pestañas Estado / Registro (filtrable por repo, acción,
  nivel, texto) / Configuración (editor YAML) / Acerca de.
- Registro estructurado de eventos (`events.jsonl`) + notificaciones de escritorio.
- Arquitectura: motor en hilo de fondo, GUI en el hilo principal, comunicación por
  señales Qt; acciones manuales serializadas con un lock en el motor.
- Arranque: `python -m sincrogit --tray` (o `pythonw` sin consola).

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
- ⏳ Pendiente: onboarding guiado en "Add repo" (crear/conectar un remoto privado y
  verificarlo con un push de prueba, desde la GUI), más un chequeo de salud
  `sincrogit doctor` (git, acceso al remoto, credenciales, pandoc, Ollama) — para el
  público sin Git, el montaje de remoto/credenciales es la barrera de entrada real, no
  el demonio.

**Pendiente — técnico (sin feature visible para el usuario):**
- ⏳ Batería de tests automatizados — **hoy no existe ninguna**; todos los caminos de
  seguridad se han verificado a mano. Prioridad: clasificación de `work_relationship`,
  los rechazos del fast-forward (`untracked_collisions`, `modified_unstaged`), aborto +
  pausa en conflicto de rebase, e idempotencia de sellado/push — todo contra repos
  locales desechables; después, CI.

**Opcional / futuro:**
- Rama `autosnap` con commits reales cada 5 min (historial intra-ventana navegable *en el remoto*) en lugar del espejo force-push del último estado.
- Variante "espejo en vivo" (force-with-lease del WIP) si el backup remoto en tiempo real se vuelve necesario.
- Tanda de IA, inspirada en aicommit2 (contratos intactos: nunca bloquear el sellado,
  privacidad por defecto, solo `urllib` estándar): **endpoint genérico
  OpenAI-compatible** (`ai.cloud_provider: compatible` + `ai.cloud_url`) que cubre
  OpenRouter/DeepSeek/LM Studio/Anthropic/… con un único cliente (las keys siguen en
  variables de entorno); **`ai.locale`** para mensajes en el idioma del usuario;
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
- ✅ **Intervalos: snapshot cada 5 min, sellado cada 6 h, autosnap cada 30 min.**
- ✅ Push **solo de sellados** (WIP local; pull siempre limpio; sin force-push).
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
- ✅ **Autosnap** (espejo en vivo de `HEAD` a `refs/autosnap/<user>/<host>/<rama>`, force-push cada 30 min, solo si cambió): RPO de fallo de disco ≈ 30 min, recuperación cross-machine por fichero o repo entero (CLI `--autosnaps` + GUI). La variante "historial fino navegable en el remoto" (un commit por snapshot) sigue diferida.

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

*(Ninguna pendiente — diseño cerrado. Listo para empezar la Fase 1.)*
