# SincroGit — Documento de diseño

> Sincronización automática estilo Dropbox, pero con versionado robusto sobre Git.
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
| **Sellado (historia)** | El WIP se "congela" con un mensaje IA descriptivo y se crea un WIP nuevo encima | Cada ~2 h (o por inactividad / apagado) | Sí (commit permanente) |

```
... ── sellado_N ── WIP        ← HEAD (se amendea cada ~5 min)
                     │
       cada 2h ──────┘ se sella (reword con mensaje IA) y nace un WIP nuevo encima

resultado: ... ── sellado_N ── sellado_N+1 ── WIP(nuevo) ← HEAD
```

**Por qué funciona:**

- El estado actual queda guardado en disco cada ~5 min → **recuperación ante corte de luz** (al reiniciar, `HEAD` = último snapshot).
- Como se hace `amend`, no se acumulan cientos de commits: solo **~12 commits/día** (uno cada 2 h).
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

### 3.3 Sellado (cada 2 h)
**Único disparador automático:** temporizador de **2 h desde el último sellado**.

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
Sobremesa: trabaja → cada 2h (o `sincrogit sync` manual) sella + push  ──►  remoto al día
Portátil:  arranca → pull --rebase (limpio) → trabaja → sella + push ──► remoto al día
Sobremesa: arranca → pull --rebase (limpio) → continúa...
```

**Importante (consecuencia de sellar solo cada 2h):** entre sellados, lo último que trabajaste podría **no estar aún en el remoto** (hasta 2 h). Por eso, si voy a cambiar de equipo a media ventana, conviene lanzar **`sincrogit sync`** antes de levantarme, para que el portátil arranque con todo. Si no lo hago, el portátil tendrá el estado del último sellado (hasta 2 h atrás).

**Coste aceptado:** ante un fallo **total de disco** (no un simple corte de luz), podrías perder hasta lo no sellado (máx. ~2 h). El corte de luz / crash de SO está cubierto por el snapshot local de cada 5 min.

> *(Variante "espejo en vivo" descartada por ahora: hacía force-with-lease del WIP cada minuto para tener el remoto a <1 min. Más tráfico y complejidad; reevaluable si algún día el backup remoto en tiempo real se vuelve crítico.)*

---

## 5. Filtro de ficheros: solo código

**Criterio: se versiona automáticamente solo lo que sea TEXTO y < 1 MB.** Todo lo demás (binarios, ficheros grandes) lo gestiono **a mano**.

- **Detección de "texto"** por contenido, no por extensión: leer los primeros ~8 KB y descartar si hay bytes NUL / no es decodificable (heurística estándar de "binario"). Es más fiable que una lista de extensiones.
- **Tamaño:** descartar si > 1 MB.
- **Implementación clave:** el filtro vive en la **lógica de `git add` de la herramienta** (se hace `git add` *solo* de los ficheros que pasan el filtro; **nunca** `git add -A`).
  - Ventaja: como **no** uso `.gitignore` para esto, si algún día quiero meter un binario o un fichero grande, basta con `git add <fichero>` a mano y commitear — la herramienta no me lo impide, simplemente no lo toca por su cuenta.
- Configurable: tamaño máximo y patrones de exclusión extra (p. ej. `node_modules/`, `.venv/`, `dist/`).

---

## 6. Mensajes de commit con IA (modo híbrido)

Se generan **solo al sellar** (~12 veces/día como mucho → entra de sobra en cualquier free tier o en local).

**Estrategia híbrida (elegida):**
1. Si **Ollama** está disponible localmente → usarlo (gratis, sin cuota, **el código no sale de la máquina**).
2. Si no → caer a un proveedor de **nube** (Gemini / Groq) con API key.
3. **Modo privacidad:** opción para enviar a la nube **solo `git diff --stat` + nombres de fichero** (no el contenido), para no exponer código sensible.
4. **Fallback siempre disponible:** si la IA falla (sin red, sin cuota, timeout) → mensaje automático determinista, p. ej.:
   `auto: 4 modificados, 1 nuevo, 1 borrado (src/foo.py, ...)`.
   **El commit/sellado nunca se bloquea por culpa de la IA.**

**Entrada al modelo:** `git diff sellado_N..WIP --stat` + un diff resumido/troncado (límite de tokens) → prompt pidiendo un mensaje de commit conciso estilo *conventional commits* en una línea + cuerpo opcional.

---

## 7. Arquitectura del software (Python)

```
sincrogit/
├─ sincrogit/
│  ├─ __main__.py        # entrypoint del daemon
│  ├─ config.py          # carga/valida config YAML
│  ├─ repo.py            # wrapper de git (vía subprocess)
│  ├─ watcher.py         # watchdog + debounce por repo
│  ├─ scheduler.py       # temporizadores: snapshot 1min / seal 2h / idle
│  ├─ filter.py          # detección texto + tamaño
│  ├─ ai.py              # proveedores (ollama/gemini/groq) + fallback
│  ├─ service.py         # ciclo de vida, pull-on-start, shutdown hook
│  └─ notify.py          # notificaciones Windows (toasts) + logging
├─ config.yaml
├─ pyproject.toml
└─ DISENO.md
```

**Librerías:**
- **`watchdog`** — eventos de sistema de ficheros.
- **git vía `subprocess`** (no GitPython) — control exacto de `amend`/`push HEAD~1`/`rebase`, comportamiento transparente y predecible.
- **`httpx`** — llamadas a IA de nube; cliente de **Ollama** local por HTTP.
- **`pyyaml`** — config.
- **`logging`** (a fichero rotativo) + **`win10toast`/`winotify`** — avisos (p. ej. "autosync pausado por conflicto").
- Scheduling: bucle propio con timers (o `apscheduler` si crece).

**Decisión:** envolver el CLI de `git` con `subprocess` en vez de GitPython, porque las operaciones finas (amend continuo, push de `HEAD~1`, rebase con política de conflicto) son más claras y robustas con el CLI.

---

## 8. Configuración (ejemplo)

```yaml
# config.yaml
defaults:
  snapshot_interval_sec: 300     # cada cuánto se amendea el WIP (5 min)
  debounce_sec: 25               # espera tras el último cambio antes de snapshot
  seal_interval_min: 120         # commit "real" + push cada 2h (único disparo automático)
  pull_interval_min: 10          # fetch cada 10 min; pull solo si hay algo nuevo
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
  - path: "C:/Dropbox/mTools/sincrogit"
    remote: origin
    branch: main
  - path: "C:/Dropbox/proyectos/foo"
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
| **Fallo total de disco** | Lo sellado+pusheado está en el remoto | `git clone` en máquina nueva. Pérdida máx ≈ lo no sellado (hasta 2 h; menos si lancé `sincrogit sync`). |
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
- Sellado cada 2 h con **mensaje de fallback**.
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
- Rama `autosnap` con commits reales cada 5 min (historial intra-ventana navegable) en lugar de depender del reflog.
- Variante "espejo en vivo" (force-with-lease del WIP) si el backup remoto en tiempo real se vuelve necesario.

---

## 13. Decisiones tomadas

- ✅ Modelo **WIP+amend → seal cada 2h**.
- ✅ **Intervalos: snapshot cada 5 min, sellado cada 2 h.**
- ✅ Push **solo de sellados** (WIP local; pull siempre limpio; sin force-push).
- ✅ IA **híbrida** (Ollama local → nube fallback; opción de enviar solo stats).
- ✅ **Proveedor de nube: Gemini** (`gemini-2.5-flash-lite`), API key en variable de entorno.
- ✅ Filtro: **solo texto < 1 MB**; binarios/grandes a mano.
- ✅ **Python**; git vía subprocess; `watchdog`.
- ✅ Background: **Tarea programada al iniciar sesión** con `pythonw.exe`.
- ✅ Rama de trabajo: **`main`** (confirmar por repo).
- ✅ **Sellado solo cada 2 h** (sin sellado por inactividad ni por apagado); `sincrogit sync` manual para el handoff.
- ✅ **Pull periódico cada 10 min** (`fetch` + pull solo si el remoto tiene commits nuevos), además del pull de arranque.
- ⏳ Rama `autosnap` (historial fino navegable): **diferida a futuro**; de momento `reflog`.

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
