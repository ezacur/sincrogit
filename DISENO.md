# SincroGit — Documento de diseño

> Sincronización automática e instantánea, pero con versionado robusto sobre Git.
> Plataforma objetivo: **Windows** (uso interactivo, una sola máquina a la vez).

---

## 1. Objetivos y alcance

**Quiero dos cosas a la vez:**

1. **Versionado + autobackup local.** Volver a la versión de ayer; y no perder trabajo ante un corte de luz o crash (recuperar hasta el último minuto).
2. **Sincronización entre equipos (secuencial).** Trabajo casi siempre en el sobremesa y ocasionalmente en el portátil, **nunca a la vez**. Al cambiar de máquina, quiero que las fuentes se actualicen rápido y automáticamente.

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

- El estado actual queda guardado en disco cada ~5 min → **recuperación ante corte de luz** (al reiniciar, `HEAD` = último snapshot).
- Como se hace `amend`, no se acumulan cientos de commits: solo **~4 commits/día** (uno cada 6 h).
- El historial "limpio" (sellados) es lo único que viaja al remoto → **pull siempre limpio, sin force-push** (ver §4).

> **Red de seguridad fina:** cada `amend` deja el snapshot anterior como commit *unreachable* en el **reflog** (≈30 días por defecto). Es decir, aunque el historial visible solo tenga 1 commit por ventana, internamente puedes recuperar estados intermedios con `git reflog`. *(Opcional, ver §12: una rama `autosnap` con commits reales cada ~5 min si quieres historial intra-ventana navegable.)*

---

## 3. Flujo detallado

### 3.1 Arranque del servicio (por repo)
1. Validar: es repo git, existe el remoto y la rama configurados.
2. **`git pull --rebase --autostash`** para traer lo que dejó la otra máquina.
   - Si hay un WIP local sin pushear (caso típico tras crash) → se **rebasa** encima de lo remoto.
   - **Si hay conflicto** → `git rebase --abort`, se **pausa el autosync de ese repo**, se notifica al usuario y se registra en log. **Nunca** se resuelve de forma destructiva ni se hace force. (Esto es raro en uso secuencial, pero la política es: ante la duda, no perder datos.)
3. Asegurar que existe un WIP en la punta (si no, crear uno vacío).
4. Arrancar el watcher.

### 3.2 Ciclo de snapshot (cada ~5 min, con debounce)
- El **watcher** (eventos del sistema de ficheros) marca el repo como *dirty* y reinicia un debounce (p. ej. 20-30 s sin cambios).
- Cuando el debounce se asienta **y** ha pasado ≥5 min desde el último snapshot:
  1. Calcular ficheros candidatos y aplicar el **filtro** (§5).
  2. `git add <solo los candidatos>`.
  3. Si hay algo staged: `git commit --amend --no-edit` (mensaje WIP estático tipo `WIP: autosnapshot`).
- Sin cambios → no se hace nada.

### 3.3 Sellado (cada 6 h)
**Único disparador automático:** temporizador de **6 h desde el último sellado**.

1. Si el WIP no tiene cambios respecto a `sellado_N` → **no sellar** (no ensuciar el historial).
2. Generar mensaje con IA a partir de `git diff sellado_N..WIP` (§6).
3. `git commit --amend -m "<mensaje IA>"` → el WIP pasa a ser `sellado_N+1`.
4. Crear WIP nuevo vacío encima: `git commit --allow-empty -m "WIP: autosnapshot"`.
5. **Push** (§4).

> No hay sellado por inactividad ni por apagado. Para forzar un sellado+push puntual (p. ej. justo antes de irme al portátil) habrá un comando manual `sincrogit sync` (§12).

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

- Push: empujar **`HEAD~1`** (el último sellado), nunca el WIP vivo:
  `git push origin HEAD~1:<rama>` → así el remoto recibe historia inmutable y el WIP local se queda 1 commit por delante.
- Como los sellados son inmutables y nunca se reescriben, **el push es siempre fast-forward** y el **pull de la otra máquina es siempre limpio**. No hace falta force-push en ningún caso del flujo normal.

**Handoff entre máquinas (uso secuencial):**

```
Sobremesa: trabaja → cada 6h (o `sincrogit sync` manual) sella + push  ──►  remoto al día
Portátil:  arranca → pull --rebase (limpio) → trabaja → sella + push ──► remoto al día
Sobremesa: arranca → pull --rebase (limpio) → continúa...
```

**Handoff normal (rama limpia):** el portátil hace `pull --rebase` de la rama y arranca con lo **sellado** (hasta 6 h atrás). Para un handoff a media ventana, lanza **`sincrogit sync`** antes de levantarte (sella + push) y el portátil arrancará con todo por la vía limpia.

### 4.1 Autosnap (espejo en vivo) — recuperación ante desastre

Como sellar cada 6 h dejaría hasta 6 h de trabajo fuera del remoto, **autosnap** desacopla el *backup remoto* del *historial*: cada **30 min** (y solo si hubo cambios) se hace `push --force` de `HEAD` (sellados **+ el WIP vivo**) a un ref lateral **por máquina** `refs/autosnap/<host>/<rama>`.

- **No ensucia la rama:** nadie pullea ese ref para trabajar; la rama `main` sigue recibiendo solo sellados → pull siempre limpio. Es la excepción deliberada a "el WIP no sale de la máquina", acotada a un ref de backup.
- **RPO ante fallo total de disco ≈ 30 min** (en vez de 6 h). En la otra máquina: *Fetch autosnaps* → explorar/restaurar el último estado (fichero o repo entero).
- **Coste:** hasta ~48 push/día/repo en trabajo activo (force-push barato; **nada** en repos inactivos, porque solo sube si HEAD cambió). Objetos huérfanos en el remoto hasta su GC.
- **Corte de luz / crash de SO** sigue cubierto por el snapshot local de cada 5 min (`HEAD` en disco) y el `reflog`.

---

## 5. Filtro de ficheros: solo código

**Criterio: se versiona automáticamente solo lo que sea TEXTO y < 1 MB.** Todo lo demás (binarios, ficheros grandes) lo gestiono **a mano**.

- **Detección de "texto"** por contenido, no por extensión (más fiable que una lista de extensiones). Como el filtro de tamaño ya garantiza que el fichero es ≤ 1 MB, se inspecciona un prefijo grande (hasta ~1 MB, no solo unos KB) y se clasifica por capas: vacío → texto; **BOM** Unicode (UTF‑8/16/32) → texto; un byte **NUL** → binario; si no, se decide por la **proporción de bytes de control** (los < 0x20 que no son espacios/tab/saltos, + DEL): muy pocos → texto legible (incluye UTF‑8 con acentos/emoji/CJK y Latin‑1); muchos → binario. *(Limitación: UTF‑16/32 **sin** BOM contiene NUL → se trata como binario, igual que git.)*
- **Tamaño:** descartar si > 1 MB.
- **Implementación clave:** el filtro vive en la **lógica de `git add` de la herramienta** (se hace `git add` *solo* de los ficheros que pasan el filtro; **nunca** `git add -A`).
  - Ventaja: como **no** uso `.gitignore` para esto, si algún día quiero meter un binario o un fichero grande, basta con `git add <fichero>` a mano y commitear — la herramienta no me lo impide, simplemente no lo toca por su cuenta.
- Configurable: tamaño máximo y patrones de exclusión extra (p. ej. `node_modules/`, `.venv/`, `dist/`).
- **Lista de inclusión (`extra_includes`)**: patrones que se versionan **aunque sean binarios** (p. ej. `**/*.docx`), bajo un tope de tamaño aparte (`max_include_bytes`, 25 MB). Para `.docx` y similares, SincroGit mapea el fichero a un **driver de diff `textconv` con pandoc** en `.gitattributes` (versionado, viaja) e inyecta el comando textconv **en línea** (`git -c diff.pandoc.textconv=…`) en cada diff → diffs legibles (markdown) sin `git config` por máquina; alimenta los mensajes de IA y la time-machine. El `.docx` es la fuente de verdad; el markdown es una vista *lossy*. La ruta de pandoc es configurable (`pandoc_path`, por máquina); sin pandoc, degrada a versionar el blob opaco.

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
  autosnap: true                 # espejo en vivo de HEAD a refs/autosnap/<host>/<rama>
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

---

## 10. Recuperación ante fallos

| Escenario | Qué pasa | Cómo recupero |
|-----------|----------|---------------|
| **Corte de luz / crash de SO** | El último snapshot (≤5 min) está commiteado en `HEAD` (WIP) | Al reiniciar, el trabajo está ahí. `git reflog` para estados intermedios de la ventana. |
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
- **El push solo empuja `HEAD~1`**: garantiza que nunca subo el WIP transitorio.
- **Nunca `--force`** en el flujo automático.

---

## 12. Roadmap por fases

**✅ Fase 1 — MVP (historiador local automático) — COMPLETA:**
- Config + validación de repos.
- Watcher + debounce + snapshot (amend) cada 5 min (+ snapshot inicial al arrancar).
- Filtro texto/tamaño.
- Sellado cada 6 h con **mensaje de fallback**.
- Logging.
> Con esto ya tengo autobackup + versionado, que es el 80 % del valor.

**✅ Fase 2 — IA + sincronización remota — COMPLETA:**
- Generador de mensajes IA híbrido (Ollama → Gemini → fallback). Nunca bloquea el sellado.
- Push de sellados (refspec con SHA → `refs/heads/<branch>`) + reintento en cada sync.
- `fetch` + pull con rebase del WIP, solo si el remoto adelanta; sync inicial al arrancar.
- Política de conflicto: abortar rebase + pausar repo + notificar (verificado en pruebas).

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

**Fase 3 — Despliegue (pendiente):**
- Tarea programada al inicio de sesión (`pythonw.exe -m sincrogit --tray`).
- Comando/pestaña **`sincrogit status`** y atajo de "sellar+push ahora" (ya en el menú).

**Opcional / futuro:**
- Rama `autosnap` con commits reales cada 5 min (historial intra-ventana navegable *en el remoto*) en lugar del espejo force-push del último estado.
- Variante "espejo en vivo" (force-with-lease del WIP) si el backup remoto en tiempo real se vuelve necesario.

---

## 13. Decisiones tomadas

- ✅ Modelo **WIP+amend → seal cada 6h** + **autosnap** (espejo en vivo) cada 30 min.
- ✅ **Intervalos: snapshot cada 5 min, sellado cada 6 h, autosnap cada 30 min.**
- ✅ Push **solo de sellados** (WIP local; pull siempre limpio; sin force-push).
- ✅ IA **híbrida** (Ollama local → nube fallback; opción de enviar solo stats).
- ✅ **Prefijos:** sellado automático `sincro:`; **commit manual (Smart Commit)** con mensaje Conventional Commits propuesto por IA (resumen acumulado desde el último manual) + reset del temporizador.
- ✅ **Proveedor de nube: Gemini** (`gemini-2.5-flash-lite`), API key en variable de entorno.
- ✅ Filtro: **solo texto < 1 MB**; binarios/grandes a mano.
- ✅ **Python**; git vía subprocess; `watchdog`.
- ✅ Background: **Tarea programada al iniciar sesión** con `pythonw.exe`.
- ✅ Rama de trabajo: **`main`** (confirmar por repo).
- ✅ **Sellado cada 6 h** (timeline permanente grueso); `sincrogit sync` manual para el handoff por la vía limpia.
- ✅ **Pull periódico cada 10 min** (`fetch` + pull solo si el remoto tiene commits nuevos), además del pull de arranque.
- ✅ **Autosnap** (espejo en vivo de `HEAD` a `refs/autosnap/<host>/<rama>`, force-push cada 30 min, solo si cambió): RPO de fallo de disco ≈ 30 min, recuperación cross-machine por fichero o repo entero (CLI `--autosnaps` + GUI). La variante "historial fino navegable en el remoto" (un commit por snapshot) sigue diferida.

## 14. Cómo configurar la API key de Gemini (pendiente al llegar a Fase 2)

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
