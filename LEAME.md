# SincroGit

SincroGit le da a cualquier repo una **máquina del tiempo** automática y versionada — sin
que ejecutes `git` jamás. Cada pocos minutos fotografía tus ficheros **guardados**, cada
~6 h "sella" un commit permanente limpio, y espeja tu último estado al remoto para que tu
trabajo te siga entre máquinas con esfuerzo casi nulo.

**Para qué sirve de verdad** (sin vender humo):

- **Una máquina del tiempo para quien (o lo que) no va a commitear a mano.** ¿Rompiste o
  borraste algo hace horas y te das cuenta ahora? Vuelve a cualquier estado *guardado*
  anterior — sin disciplina, sin un solo `git add`. Ideal para el desarrollador
  ocupado/olvidadizo/que está aprendiendo.
- **Repos de prueba y experimentales.** Código que no merece un historial curado pero cuyo
  *rastro* odiarías perder — spikes, pruebas, desechables. Historial recuperable completo,
  cero ceremonia. (Posiblemente su punto más fuerte.)
- **Continuidad multi-máquina de bajo esfuerzo.** Cambias entre el ordenador de la oficina
  y el de casa y tu trabajo te sigue — *con un retardo de minutos, no instantáneo* (ver
  [relevo](#relevo-entre-máquinas-wip-vivo)); un **Smart Commit** antes de irte hace el
  relevo pronto.
- **(Bonus raro) sobrevive a un disco muerto.** El espejo remoto recupera tu último estado
  (de hace ≤~30 min) si la máquina entera muere. **No** rescata buffers no guardados del
  editor, y un corte de luz con el disco intacto no pierde nada de todos modos — tus
  ficheros guardados ya están en disco; el valor de SincroGit ahí es el *rollback*, no la
  supervivencia.

> **Cómo manejarlo** (comandos CLI, acciones del panel, recetas): el
> **[Manual de usuario](MANUAL_ES.md)** (English: [MANUAL.md](MANUAL.md)). ¿No eres experto
> en Git o quieres el *cuándo/por qué* en lenguaje llano? La **[guía para humanos](GUIA.md)**.
> Diseño y decisiones: **[DISENO.md](DISENO.md)**.

## ¿Ya dominas Git? El minuto del escéptico

Un demonio que amendea commits y hace force-push de refs *suena* a algo que mantener
lejos de tus repos — así que aquí va, por delante, exactamente qué toca y qué no toca
nunca:

- **El único commit que reescribe es el suyo.** El único commit `WIP: autosnapshot` de
  la punta es el que se amendea; tus commits no se amendean, rebasan ni descartan nunca,
  y cada snapshot reemplazado sigue recuperable en el reflog (≈30 días).
- **Tu rama nunca recibe un force-push.** Solo recibe commits sellados, siempre como
  fast-forward. `--force` se usa únicamente sobre los refs laterales por máquina de
  SincroGit (`refs/autosnap/<user>/<host>/<rama>`), donde cada máquina es la única que
  escribe.
- **Nunca fusiona ni resuelve nada por su cuenta.** ¿Trabajo divergente o conflicto de
  rebase? Se detiene, pausa ese repo y te avisa; ambos estados quedan intactos (ver
  [Relevo entre máquinas](#relevo-entre-máquinas-wip-vivo)).
- **Los commits de la máquina van etiquetados.** Todo sellado automático lleva el
  prefijo `sincro:` — trivial de localizar, aplastar o descartar antes de un PR.
- **Puedes tener cero commits de máquina.** El modo purista (`seal_interval_min: inf`)
  deja la rama 100 % tuya — solo aterrizan tus Smart Commits — mientras el WIP, el
  espejo autosnap y el relevo entre máquinas siguen funcionando por debajo (ver
  [Pragmática vs purista](#pragmática-vs-purista-tú-decides-qué-significa-un-commit)).

Cómo se implementa cada garantía está documentado en [DISENO.md](DISENO.md) §11. Para
ver cómo se relaciona SincroGit con jj, GitButler, dura y compañía, ver
[Cómo se compara](#cómo-se-compara-con-las-herramientas-vecinas).

## Estado: Fases 1, 2 y 4 completas (Fase 3, despliegue: parcial)

**Fase 1 (núcleo local):**

- ✅ Watcher del sistema de ficheros (`watchdog`) + *debounce*.
- ✅ **Snapshot** cada 5 min: `git commit --amend` sobre un commit WIP (no acumula commits).
- ✅ Snapshot inicial al arrancar (captura cambios previos, p. ej. tras un reinicio).
- ✅ **Sellado** cada 6 h: convierte el WIP en commit permanente + crea un WIP nuevo.
- ✅ **Filtro**: solo versiona automáticamente texto < 1 MB; binarios/grandes a mano.
- ✅ **Versionado opcional de binarios** (`extra_includes`, p. ej. `**/*.docx`): versiona
  también los binarios que elijas — con **diff legible para documentos Word** vía un driver
  textconv de pandoc (sin `git config` por máquina), así los mensajes de IA y la
  time-machine muestran *qué cambió en el documento*.
- ✅ Mensaje de commit de *fallback* (determinista) al sellar.
- ✅ Apagado limpio con snapshot local final.
- ✅ Logging a fichero rotativo + consola.

**Fase 2 (IA + sincronización remota):**

- ✅ **Mensajes con IA** al sellar, modo híbrido: Ollama (local) → Gemini (nube) →
  fallback determinista. Nunca bloquea el commit si la IA falla. Los sellados
  automáticos llevan el prefijo **`sincro:`** para distinguir los commits de la máquina
  de los tuyos.
- ✅ **"Smart Commit" manual**: commitea tu trabajo actual ahora con un mensaje
  **Conventional Commits propuesto por IA** (`feat:`/`fix:`/…) que puedes editar. La
  propuesta resume todo lo hecho desde tu **último commit manual** (saltando los sellados
  `sincro:`) y reinicia el temporizador de 6 h. Desde el panel (botón **"Commit…"** por
  repo).
- ✅ Privacidad: a la nube solo se manda el contenido si `cloud_send_content: true`.
- ✅ **Push** de los commits sellados (nunca el WIP) tras sellar + reintento en cada sync.
- ✅ **Pull periódico** (cada 10 min): `fetch` + rebase del WIP solo si el remoto adelanta.
- ✅ **Conflictos**: el rebase se aborta, el repo se pausa y se notifica. Nunca force,
  nunca pérdida de datos.
- ✅ **Autosnap** (espejo en vivo, cada 30 min, solo si hubo cambios): hace `push --force`
  de `HEAD` (incl. el WIP) a un ref lateral por usuario/máquina `refs/autosnap/<user>/<host>/<rama>`,
  de modo que un fallo total de disco pierde como mucho ~30 min. No toca la rama limpia;
  recuperación entre máquinas desde la CLI (`--autosnaps`) y el panel de control.
- ✅ **Relevo entre máquinas** (WIP vivo, desacoplado del sellado): cada sync recoge el
  trabajo vivo de tu *otra* máquina y hace fast-forward si es sin pérdida; ante divergencia
  nunca auto-fusiona — avisa y deja ambos estados intactos. Ver
  [Relevo entre máquinas](#relevo-entre-máquinas-wip-vivo). Interruptor: `live_handoff`.
- ✅ **Relevo por eventos del SO** (Windows): bloquear/suspender **vuelca** tu último estado
  al remoto, desbloquear/reanudar lo **sincroniza** — así "bloqueo aquí → desbloqueo allá"
  releva en segundos en vez de esperar los ~30 min del intervalo del espejo (+ un detector de
  salto de reloj que también funciona en headless).
- ✅ **Guarda de rama**: si haces `git checkout` a otra rama, SincroGit se inhibe en ese
  repo (no snapshot/seal/push en la rama equivocada) hasta que vuelvas. O pon
  `track_current_branch: true` para **seguir** la rama actual (flujo de feature branches;
  snapshots/autosnap/relevo/push en la rama en la que estés).
- ✅ **Smart Ignore**: si una carpeta no para de generar ficheros filtrados (salida de
  build, cachés), SincroGit sugiere una vez —una notificación— añadirla a `extra_excludes`.
  Nunca auto-edita. Interruptor `suggest_excludes`.

**Fase 4 (interfaz de bandeja):**

- ✅ **Icono en la bandeja del sistema** con la marca de SincroGit (una "G" con
  reloj de arena). El **color refleja el estado**: verde=activo, ámbar=pausado,
  rojo=conflicto, gris=parado.
- ✅ **Menú** de bandeja: abrir panel, pausar/reanudar, sincronizar ahora, sellar
  ahora, salir.
- ✅ **Panel de control** con pestañas:
  - *Estado*: tabla de repos (rama, estado, tiempo desde el último sellado, última
    acción) con **botones por repo** (Pausar/Reanudar, Sellar+Push, Fetch+Pull) y un
    botón **"Add repo…"** (que opcionalmente crea un `.gitattributes` `* text=auto` para
    que los finales de línea sean consistentes entre máquinas). Los repos se añaden en
    caliente, sin reiniciar.
  - *Registro*: eventos **filtrables por repo, acción, nivel y texto**.
  - *Configuración*: editor del `config.yaml` (guardar / guardar y reiniciar).
  - *Acerca de*.
- ✅ **Notificaciones** de escritorio (vía Qt) ante conflictos/errores.
- ✅ **Historial / restauración de ficheros** ("máquina del tiempo"): explora las
  versiones pasadas de un fichero (commits sellados + snapshots del reflog + estados
  autosnap fetcheados), con **diff coloreado** frente al fichero actual, y restaura
  **solo ese fichero o el repo entero** — desde la CLI (`--history`, `--autosnaps`) y
  el panel de control.

Pendiente (Fase 3): ver el [TODO](#todo) de abajo.

## TODO

Por orden de prioridad:

1. **Onboarding sin fricción para quien no sabe Git.** El público que más necesita
   SincroGit es el menos preparado para crear un remoto y configurar credenciales — hoy
   ese montaje es la barrera de entrada real, no el demonio. Plan: un flujo guiado en
   "Add repo…" que cree/conecte un remoto privado (GitHub/GitLab), lo verifique con un
   push de prueba y aplique defaults sensatos — sin que el usuario tenga que saber qué
   es un remoto.
2. **Arranque automático al iniciar sesión** (la pieza de la Fase 3 que falta). La
   promesa de "cero disciplina" se rompe si hay que acordarse de lanzar la red de
   seguridad: un aviso en el primer arranque (o un paso del instalador) debería
   registrar la tarea programada de Windows (`SincroGit.exe --tray` /
   `pythonw.exe -m sincrogit --tray` al iniciar sesión — ver [DISENO.md §9](DISENO.md)).
3. **Comando `sincrogit status`** (el menú de bandeja ya cubre las acciones comunes).

### TODO — técnico (para desarrolladores)

- **Batería de tests automatizados — hoy no hay ninguna.** Toda afirmación de seguridad
  (clasificación del relevo, caminos de rechazo, pausa por conflicto) se ha verificado
  solo a mano; para una herramienta cuya promesa es "nunca pierde datos", es la pieza
  ausente más importante. Orden de prioridad: clasificación de `work_relationship`; los
  rechazos del fast-forward (`untracked_collisions`, `modified_unstaged`); aborto +
  pausa en conflicto de rebase; idempotencia de sellado/push — todo ejecutable contra
  repos locales desechables. Después, CI.

## Instalación

```powershell
pip install -r requirements.txt
# o, como paquete:  pip install -e .
```

## Uso

1. Configuración: en el primer arranque SincroGit crea `sincrogit.config.yaml` junto al
   ejecutable (con la lista de repos vacía) y abre la pestaña Configuración. Luego
   **añade los repos desde la GUI** (Estado → "Add repo…"). Para partir de una plantilla
   a mano:
   ```powershell
   copy config.example.yaml sincrogit.config.yaml
   ```
2. Arranca SincroGit:
   ```powershell
   # App de bandeja + demonio (sin argumentos):
   python -m sincrogit

   # …o apuntando a una config concreta:
   python -m sincrogit --tray --config config.yaml

   # Demonio headless (sin GUI), para servidores o tareas automáticas:
   python -m sincrogit --headless --config config.yaml
   ```

**Modelo de lanzamiento** (igual para el script y el `.exe` autónomo):

| Invocación | Comportamiento |
|------------|----------------|
| *(sin argumentos)* | App de bandeja + demonio (**instancia única**; un segundo lanzamiento solo muestra el panel) |
| `--tray [--config X]` | App de bandeja + demonio |
| `--headless [--config X]` | demonio sin GUI |
| `--snapshot-once` / `--seal-once` / `--sync-once` | una pasada por CLI y salir |
| `--history FICHERO [--pick N]` | explorar/restaurar versiones de un fichero |
| `--autosnaps` | fetch + listado de puntos de recuperación autosnap (por máquina) |
| `--commit REPO [-m MSG \| -y]` | commit manual de REPO: edita el mensaje propuesto por IA en `$EDITOR` y sella+pushea |
| `--apply-handoff REPO` | aplica a REPO el trabajo vivo pendiente de tu otra máquina (relevo) |
| `--force` | ejecuta un disparo único aunque el demonio esté corriendo (por defecto rehúsan, para no competir con su git — ver el [Manual](MANUAL_ES.md)) |

### Mensajes con IA (opcional)

- **Ollama (local, recomendado):** instala [Ollama](https://ollama.com), descarga un
  modelo (`ollama pull llama3.2`) y SincroGit lo usará automáticamente. Tu código no
  sale de la máquina.
- **Gemini (nube):** consigue una API key en Google AI Studio y expórtala:
  ```powershell
  setx SINCROGIT_GEMINI_KEY "tu_api_key"
  ```
  Con `cloud_send_content: false` (por defecto) a Gemini solo le llegan nombres de
  fichero y `--stat`, no el contenido.
- Si no configuras ninguno, se usa un **mensaje de fallback** determinista.

### Versionar documentos Word (.docx)

Por defecto los binarios no se versionan solos. Para seguir un `.docx` (sincronizado +
restaurable) con **diff legible**:

1. Instala [pandoc](https://pandoc.org); si no está en el PATH, pon `pandoc_path` en la
   config (p. ej. `pandoc_path: C:/tools/pandoc.exe`).
2. Añade el patrón a los includes del repo:
   ```yaml
   defaults:
     extra_includes:
       - "**/*.docx"
   ```

SincroGit versiona el `.docx` y lo mapea a un driver de diff de pandoc en `.gitattributes`
(versionado, así viaja), pasando el textconv **en línea en cada llamada — sin `git config`
en ninguna máquina**. Entonces `git diff`, los mensajes de IA y la time-machine muestran
los cambios del documento como markdown. El `.docx` sigue siendo la fuente de verdad; la
vista markdown es *lossy* (sin formato/imágenes), y sin pandoc degrada a versionar el
fichero como blob opaco.

**Qué cuenta como cambio.** Como la detección de cambios pasa por pandoc, un `.docx` se
versiona/sincroniza **solo cuando cambia su markdown** — ediciones de texto y formato
estructural (negrita, cursiva, encabezados, listas, tablas, enlaces) cuentan; la
maquetación puramente visual (fuente, color, tamaño, alineación, layout) y el ruido
interno de Word al reguardar (timestamps, IDs de revisión) **no**, así que no se versionan
ni respaldan hasta que un cambio de contenido los arrastre. Tras una sesión de solo
maquetar, fuerza una versión con un **Smart Commit** manual. (Sin pandoc, la detección
vuelve a bytes y cada guardado es una versión.)

> 📌 **Posible, aún no implementado:** el mismo mecanismo de textconv inline podría dar
> diffs legibles para otros formatos — notebooks Jupyter (`.ipynb` vía `jupytext`/`nbconvert`),
> hojas de cálculo (`.xlsx` vía `in2csv`), etc. — mediante un mapa configurable
> `patrón → comando` en vez del driver actual solo-`.docx`. Es una extensión limpia que no
> hemos construido. Aviso por si la hacemos: textconv arregla el *diff legible*, no el *peso
> del repo* — un `.ipynb` seguiría guardando el JSON completo (outputs); la higiene real de
> notebooks necesita además un *clean filter* (p. ej. `nbstripout`).

### Restaurar una versión pasada (máquina del tiempo)

Explora y restaura versiones previas de un fichero, combinando commits sellados
(permanentes), snapshots intra-ventana (del reflog, ~30 días) y estados autosnap de
otras máquinas (tras "Fetch autosnaps"):

```powershell
# Interactivo: lista las versiones y pregunta cuál restaurar
python -m sincrogit -c config.yaml --history ruta\a\fichero.py

# No interactivo: restaura directamente la versión N
python -m sincrogit -c config.yaml --history ruta\a\fichero.py --pick 3

# Recuperación ante desastre: trae y lista los autosnap de cada máquina
python -m sincrogit -c config.yaml --autosnaps
```

En la app de bandeja, lo mismo está en el panel de control:
**Estado → "File history…"** (explorar, ver un diff de cualquier versión y restaurar
un fichero o el repo entero).

### Commit manual (Smart Commit)

Sella tu trabajo actual ahora con un mensaje curado, en vez de esperar al sellado
automático de 6 h. SincroGit propone un mensaje Conventional Commits (que cubre tu
trabajo desde el último commit manual) y lo abre en tu editor:

```powershell
python -m sincrogit -c config.yaml --commit mirepo                    # edita la propuesta en $EDITOR
python -m sincrogit -c config.yaml --commit mirepo -y                 # acepta la propuesta tal cual
python -m sincrogit -c config.yaml --commit mirepo -m "feat: añade X" # tu propio mensaje
```

En la app de bandeja, el botón **"Commit…"** por repo hace lo mismo.

### Modos de prueba (una pasada y salir)

```powershell
python -m sincrogit -c config.yaml --snapshot-once   # un snapshot y sale
python -m sincrogit -c config.yaml --seal-once       # fuerza un sellado (+push) y sale
python -m sincrogit -c config.yaml --sync-once       # un pull+push y sale
```

## Cómo funciona (resumen)

```
... ── sellado_N ── WIP        ← HEAD, se amendea cada 5 min (snapshot)
cada 6h: el WIP se sella (mensaje descriptivo) y nace un WIP nuevo encima
resultado: ... ── sellado_N ── sellado_N+1 ── WIP(nuevo)
```

- **Deshacer un error reciente**: el último snapshot está en `HEAD`; los estados guardados
  anteriores de la ventana, en `git reflog` (resolución ≈5 min).
- **Volver a ayer**: `git checkout`/`restore` desde el commit sellado correspondiente.
- **Fallo total de disco**: recupera en otra máquina desde el ref `autosnap` (≤30 min).

## Notas de diseño y compromisos

SincroGit, a propósito, no es Git "puro". Git asume que cada commit es un cambio
lógico curado; SincroGit, en cambio, optimiza para un único desarrollador que se
olvida de commitear y cambia de máquina. Los compromisos deliberados:

- **Sellados por tiempo, no por unidad lógica.** Los sellados automáticos (`sincro:`)
  agrupan lo que haya cambiado en una ventana de ~6 h — una línea temporal, no commits
  atómicos. Cuando quieras un commit curado, usa **Smart Commit** (mensaje Conventional
  Commits propuesto por IA). El prefijo `sincro:` distingue los commits de la máquina de
  los tuyos.
- **El WIP es un "botón de guardar" continuo — para ficheros *guardados*.** Un único commit
  se amendea cada ~5 min, así que cualquier estado guardado anterior es recuperable del
  reflog (resolución ≈5 min). Fotografía lo que está en disco, **no** el buffer no guardado
  de tu editor — así que su valor es el *rollback*, no sobrevivir a un crash (un corte de
  luz con el disco intacto no pierde nada de todos modos; los ficheros guardados ya están
  en disco).
- **El backup está desacoplado del historial.** `autosnap` hace force-push del estado vivo
  a un ref lateral por máquina cada ~30 min, mientras `main` se mantiene limpia (solo
  sellados) → el pull de la otra máquina es siempre un fast-forward limpio. Sirve tanto a la
  rara recuperación ante fallo de disco como al relevo entre máquinas.

El coste que aceptamos: el historial se lee como bloques de tiempo en vez de commits
perfectamente atómicos; la resolución de rollback es ~5 min (la cadencia de snapshot); y un
fallo total de disco puede perder hasta ~30 min (la cadencia de autosnap) — a cambio de una
máquina del tiempo versionada sin esfuerzo y continuidad multi-máquina de bajo esfuerzo.

### Pragmática vs purista: tú decides qué significa un commit

La filosofía purista de Git dice que un commit narra una **unidad lógica de trabajo**
(un feature, un fix), **no el paso del tiempo**. El historial permanente está para que
un humano lo lea después y entienda *por qué* cambió el código. SincroGit viene en modo
*pragmático* por defecto —sella por reloj para que el olvidadizo tenga gratis un
historial decente— pero puedes pasarlo a *purista* manteniendo intacta la misma red de
seguridad por debajo:

- **Pragmática (por defecto).** Auto-sella cada 6 h: la máquina escribe tu línea
  temporal, tú no haces nada. Ideal para proyectos personales donde no te importan los
  commits atómicos.
- **Purista.** Pon `seal_interval_min: inf` (ver *[Desactivar un intervalo o límite](#desactivar-un-intervalo-o-límite)*).
  El sellado automático no se dispara nunca, así que la rama queda **inmaculada** — cada
  commit permanente es uno que hiciste *tú*, cuando una tarea está de verdad terminada,
  vía **Smart Commit** (Conventional Commits propuesto por IA). El WIP y el `autosnap`
  siguen funcionando por debajo, así que conservas la máquina del tiempo y la recuperación
  ante fallo de disco. Es "Git casi puro" con una red de seguridad invisible — un historial
  presentable incluso junto a un equipo.

  > **Nota:** incluso en modo purista sigues teniendo continuidad automática entre
  > máquinas, porque el **[relevo en vivo](#relevo-entre-máquinas-wip-vivo)** funciona sobre
  > el WIP, no sobre el sello. Así la rama queda inmaculada *y* tu portátil sigue recogiendo
  > solo el último trabajo del sobremesa.

### Relevo entre máquinas (WIP vivo)

Tus máquinas se pasan el trabajo **automáticamente**, desacoplado del sellado: cada una
sube su WIP vivo a un ref lateral personal `refs/autosnap/<tú>/<host>/<rama>` (el `<tú>`
sale de tu `git config user.email`, así una máquina reconoce a sus *propias* otras máquinas
frente a las de un compañero). En cada sync, SincroGit baja los espejos de tus otras
máquinas y:

- **Fast-forward seguro (el caso común).** Si el trabajo de la otra máquina *contiene todo
  el tuyo* (típico: aquí no tocaste nada desde que te fuiste), es un fast-forward sin pérdida
  (solo descarta un WIP vacío, reversible vía el reflog) — y se **omite** (te avisa) si fuera
  a sobrescribir un fichero untracked **o** si tienes ediciones locales que el auto-snapshot
  no puede capturar (un fichero rastreado que ahora es demasiado grande / binario /
  excluido): esas no existen en ningún sitio de git, así que aplicarlo las destruiría —
  commitéalas a mano primero. En caso contrario, qué pasa depende de `live_handoff`:
  - **`auto`** (por defecto): se aplica solo — te sientas y sigues. **Nunca en silencio**:
    recibes una notificación de bandeja ("te puse al día con *Desktop-PC*"), así siempre
    sabes que tus ficheros se movieron.
  - **`ask`**: no toca nada. Recibes una notificación y el panel muestra un botón **Apply
    handoff** (o ejecutas `--apply-handoff REPO`); revalida que sigue siendo seguro y
    entonces hace el fast-forward. Ideal si prefieres confirmar antes de que cambie tu
    working tree.
  - **`off`**/`false`: el relevo se desactiva (solo manual, como abajo).
- **Divergencia → decides tú (sin auto-merge, en cualquier modo).** Si *las dos* máquinas
  cambiaron trabajo que la otra no tiene, SincroGit **no las fusiona**. Es deliberado:
  fusionar en 3-way y en silencio dos montones de trabajo en curso sin revisar es justo cómo
  acabarías con un árbol sutilmente roto. Te avisa (una vez) y deja **ambos** estados intactos.

> ⚠️ **Lo que falta a propósito: el merge automático.** Ante divergencia, lo resuelves tú,
> a tu manera:
>
> **Lo más fácil (recomendado) — sella un lado y deja que el otro rebase:**
> 1. En la máquina cuyo trabajo quieres como base, haz un **Smart Commit** (convierte su WIP
>    en un commit de verdad y lo sube).
> 2. El pull normal de la otra máquina rebasa tu WIP encima. Sin solape → limpio y
>    automático; con solape → conflicto de rebase normal (SincroGit se pausa; lo resuelves en
>    tu editor y pulsas **Reanudar**).
>
> **Control total — inspeccionar/fusionar a mano** (el estado vivo del peer está en el ref
> lateral; los nombres exactos, en *Fetch autosnaps* del panel o `--autosnaps`):
> ```bash
> git log  --oneline  refs/autosnap/<tú>/<otro-host>/<rama>   # qué tienen
> git diff HEAD       refs/autosnap/<tú>/<otro-host>/<rama>   # comparar
> git reset --hard    refs/autosnap/<tú>/<otro-host>/<rama>   # quedarte con lo suyo (lo tuyo -> reflog)
> git merge           refs/autosnap/<tú>/<otro-host>/<rama>   # o fusionar ambos, resolver conflictos
> ```

Pon `live_handoff` por repo en `auto` (por defecto), `ask` u `off`. Necesita `autosnap`
activo (es lo que publica el espejo que lee tu otra máquina).

**Acelerado por eventos del SO (Windows).** En vez de esperar los intervalos, SincroGit
engancha la sesión: **bloquear la pantalla o suspender** (te vas) **vuelca** tu último estado
al remoto al instante, y **desbloquear o reanudar** (has llegado) lo **sincroniza** al
instante. Así "bloqueo aquí → desbloqueo allá" releva en segundos. (Una suspensión larga que
corta la red a mitad del volcado cae al siguiente autosnap; un detector de salto de reloj
también fuerza un sync tras cualquier reanudación, así que también funciona en headless para
el lado del despertar.)

### Usar SincroGit en equipo (repos compartidos)

SincroGit es, por defecto, una herramienta **personal y de una sola rama**. Apuntada directa
a una rama **compartida** (todos pusheando a `main`/`develop`), el auto-rebase periódico choca
en cuanto el push de un compañero toca tus ficheros — y SincroGit, que nunca es destructivo,
**se pausa y te pide resolver en la terminal**. Esa interrupción recurrente mata la promesa de
"olvídate de la terminal", así que **no lo pongas en una rama donde otros pushean.** El montaje
para equipo es una sola opción:

1. **`track_current_branch: true`** — SincroGit sigue la rama en la que estés en vez de pausarse
   fuera de la configurada.
2. Trabaja en **tu propia rama** (`feature/login-pepe`). SincroGit la respalda y la relevará
   entre *tus* máquinas de forma invisible: los espejos del WIP vivo van namespaced **por
   usuario** (`refs/autosnap/<tú>/<host>/<rama>`), así que nunca tocan las ramas de tus
   compañeros ni sus borradores, ni los suyos los tuyos (**team-safe**).
3. Cuando una unidad de trabajo está lista, pulsa **Smart Commit** (mensaje Conventional Commits
   propuesto por IA) y abres un **Pull Request** a la rama compartida — un merge normal, revisado.

> **La combinación más limpia:** añade **`seal_interval_min: inf`** (modo purista). Entonces
> SincroGit **no** hace ningún commit automático en tu rama — cada commit permanente es uno que
> hiciste *tú* vía Smart Commit — mientras el WIP + autosnap te protegen y sincronizan por
> debajo. El historial de la rama parece hecho a mano; nadie nota que había una red de seguridad.

**Nunca pierdes la capacidad de commitear por unidades lógicas.** El "cubo temporal" es solo el
*suelo automático*, no un techo: en cualquier momento puedes sellar tú una tarea terminada con
**Smart Commit** (la herramienta te redacta el mensaje). Y los commits automáticos llevan siempre
el prefijo **`sincro:`**, así que los snapshots de la máquina y tus commits de verdad quedan
trivialmente distinguibles — aplasta o descarta los `sincro:` antes de un PR, o corre en purista
y no habrá ninguno.

Sigue siendo secuencial **por rama** (una máquina a la vez en una rama dada); no fusiona a dos
personas editando la *misma* rama a la vez.

## Cómo se compara con las herramientas vecinas

Hay muchas herramientas que auto-commitean un repo; ninguna combina las piezas de
SincroGit. Panorama **a junio de 2026** (la actividad cambia — toma las notas como una
foto). Leyenda: ✅ sí · ➖ parcial · ❌ no.

| Herramienta | Snapshots sin acumular commits | Demonio "configura y olvida" | Mensajes de commit con IA | Relevo de WIP entre máquinas | Nunca auto-fusiona / force-pushea tu rama | GUI de máquina del tiempo |
|------|------|------|------|------|------|------|
| **SincroGit** | ✅ un único WIP amendado | ✅ demonio de bandeja | ✅ auto-sellado + Smart Commit (Ollama → Gemini → fallback) | ✅ refs por máquina + eventos de bloqueo/desbloqueo | ✅ | ✅ por fichero, app de bandeja |
| [jujutsu (jj)](https://github.com/jj-vcs/jj) | ✅ mismo modelo (working copy = un commit amendado) | ➖ al guardar, vía watchman | ❌ (herramientas externas) | ❌ solo local | ✅ | ❌ CLI (`jj op restore`) |
| [GitButler](https://github.com/gitbutlerapp/gitbutler) | ➖ snapshots del oplog alrededor de operaciones | ➖ app de escritorio | ✅ interactivo (Ollama/OpenAI/Anthropic) | ❌ | ➖ force-pushea sus ramas virtuales | ➖ restauración a nivel de proyecto, GUI de escritorio |
| [dura](https://github.com/tkellogg/dura) | ❌ un commit por cambio (ramas sombra) | ✅ demonio | ❌ | ❌ (sin remoto) | ✅ (nunca pushea) | ❌ |
| [gitwatch](https://github.com/gitwatch/gitwatch) | ❌ un commit por cambio, en tu rama | ✅ demonio | ❌ | ➖ pushea tu rama | ❌ | ❌ |
| [git-wip](https://github.com/bartman/git-wip) | ❌ commits apilados en `refs/wip/*` | ❌ hooks de guardado del editor | ❌ | ❌ | ✅ | ❌ |
| [GitDoc](https://github.com/lostintangent/gitdoc) | ➖ commits por intervalo (squash opcional), en tu rama | ➖ atado a VS Code | ➖ solo Copilot | ➖ auto-push/pull de la misma rama | ❌ (auto-pullea) | ❌ |
| [aicommit2](https://github.com/tak-bro/aicommit2) | — | ❌ (CLI interactiva) | ✅ multi-proveedor incl. Ollama | ❌ | — | ❌ |
| [git-annex assistant](https://git-annex.branchable.com/) | ❌ un commit por cambio | ✅ demonio | ❌ | ➖ refs `synced/*` compartidos, auto-merge | ❌ (auto-fusiona) | ➖ webapp |
| [SparkleShare](https://github.com/hbons/SparkleShare) | ❌ un commit por cambio | ✅ demonio de bandeja | ❌ | ➖ rama compartida, auto-merge | ❌ | ➖ bandeja + restauración (build de Windows abandonado hace años) |
| [Obsidian Git](https://github.com/Vinzent03/obsidian-git) | ❌ commits por intervalo | ➖ solo vaults de Obsidian | ❌ (plantillas) | ➖ rama compartida, auto-pull/merge | ❌ | ➖ historial de ficheros dentro de la app |
| [git-sync (simonthum)](https://github.com/simonthum/git-sync) | ❌ un commit por ejecución | ❌ script/hook | ❌ | ➖ rama compartida, auto-rebase | ❌ | ❌ |

Lectura del panorama: cada columna tiene al menos un precedente parcial en alguna parte,
pero ninguna herramienta las combina — y dos piezas no tienen **equivalente en nada de lo
encontrado**: los refs de relevo por usuario/máquina con semántica de nunca-auto-fusionar,
y la sincronización disparada por eventos de bloqueo/desbloqueo/suspensión del SO. Los
espacios concurridos son el auto-commit por intervalo (muchas herramientas, casi todas
estancadas) y los mensajes IA interactivos (muchos, muy activos); el espacio vacío es la
mecánica del relevo.

### jj (jujutsu): el pariente más cercano

[jj](https://github.com/jj-vcs/jj) merece nota propia: es la única otra herramienta
construida sobre la idea central de SincroGit — la working copy **es** un único commit,
amendado en cada snapshot, sin acumular commits — y su `jj op log` / `jj op restore` es
una verdadera máquina del tiempo local. La diferencia es de alcance y dirección:

- **jj es un VCS que adoptas.** Una CLI nueva y un modelo mental nuevo (convive con
  remotos git, pero *tú* dejas de teclear `git`). Su red de seguridad es solo local: sin
  espejo remoto, sin relevo entre máquinas, sin mensajes IA, sin GUI; los snapshots se
  disparan con comandos de jj / eventos de watchman, no con un reloj ni con eventos de
  sesión.
- **SincroGit es una capa que no tienes que aprender.** Tu repo sigue siendo git normal y
  tus hábitos quedan intactos; lo que añade es justo lo que jj no lleva — el espejo
  autosnap remoto, el relevo entre máquinas (incl. los disparadores de
  bloqueo/desbloqueo), los mensajes IA de sellado y la UI de bandeja/máquina del tiempo.

Si te apetece cambiar de herramienta, jj es excelente y está más integrado. Si quieres
seguir en git normal — o necesitas la continuidad multi-máquina — ese es el carril de
SincroGit.

## Limitaciones

SincroGit tiene un alcance deliberadamente acotado. Lo que **no** hace:

- **Versiona ficheros *guardados*, no buffers sin guardar.** Un corte de luz/crash con el
  disco intacto no pierde nada de todos modos (tus ficheros guardados están en disco); el
  valor de SincroGit ahí es el *rollback* a un estado guardado anterior, no sobrevivir al
  crash. **No** rescata trabajo que nunca guardaste — eso es el autosave de tu editor.
- **La sincro multi-máquina no es en tiempo real, pero suele ser de segundos.** En Windows,
  bloquear la pantalla / cerrar la tapa **vuelca** tu último estado al remoto al instante, y
  desbloquear / despertar la otra máquina lo **sincroniza** al instante — así el flujo normal
  "bloqueo aquí, desbloqueo allá" releva en **segundos** (ver [Relevo entre máquinas](#relevo-entre-máquinas-wip-vivo)).
  Si te vas **sin** bloquear, cae al espejo periódico (`autosnap_interval_min` ~30 min) + pull
  (`pull_interval_min` ~10 min) — hasta ~40 min. Un **Smart Commit** antes de cambiar es
  siempre instantáneo (va por la rama). Nunca es una sincro en tiempo real al nivel de tecla.
- **Secuencial, no simultáneo.** Asume una máquina a la vez. No fusiona ediciones
  simultáneas en dos máquinas — el rebase se aborta y el repo se pausa para que resuelvas
  a mano. Es una herramienta personal; para repos de equipo usa tu propia rama (ver
  [Usar SincroGit en equipo](#usar-sincrogit-en-equipo-repos-compartidos)), no una compartida.
- **Solo texto, < 1 MB.** Los binarios y ficheros grandes nunca se commitean
  automáticamente; esos van a mano. No es un backup total de la carpeta.
- **Historial por bloques de tiempo.** Los sellados `sincro:` agrupan ~6 h de cambios
  inconexos, así que un `git bisect`/`revert` de un cambio lógico es más difícil que sobre
  un historial curado (usa **Smart Commit** cuando quieras un commit limpio).
- **La resolución de rollback / la ventana de fallo de disco no son cero.** Puedes volver
  con resolución ~5 min (cadencia de snapshot); un **fallo total de disco** pierde hasta
  ~30 min (el último autosnap en el remoto) — un evento raro, y el único caso donde "los
  ficheros en disco" no te cubre ya.
- **Los conflictos los resuelves tú.** Ante conflicto nunca fuerza — pausa y avisa; lo
  arreglas en la terminal y reanudas.
- **Necesita tus credenciales de Git.** Corre en tu sesión de usuario y pushea con tu
  configuración SSH/credenciales; sin acceso de push conserva el trabajo en local y
  reintenta.
- **Los mensajes de IA necesitan un modelo y son aproximados.** Sin Ollama ni API key de
  Gemini usa un mensaje determinista; el resumen de una ventana de 6 h es grueso por
  naturaleza.
- **Pensado para Windows.** Diseñado para uso interactivo en Windows; en Linux/macOS
  haces el pull a mano.
- **No metas el repo dentro de la carpeta de otra herramienta de sync** (Dropbox/OneDrive/
  Drive) — el sincronizador externo puede corromper `.git`. Deja que SincroGit gestione
  Git y la otra herramienta gestione otros ficheros.

## Compilar un .exe autónomo

Un único `SincroGit.exe` autocontenido (GUI + CLI, sin necesidad de Python) se
genera con PyInstaller:

```powershell
python -m pip install pyinstaller pillow
.\build.ps1
```

Esto genera `app.ico` a partir del icono vectorial y produce `dist\SincroGit.exe`.

- **Doble clic** (sin argumentos) → app de bandeja + demonio, **instancia única** (un
  segundo lanzamiento solo trae al frente el panel).
- **Desde una terminal con argumentos** → CLI (la salida se engancha a esa terminal):
  `SincroGit.exe --history fichero.py`, `SincroGit.exe --headless`, etc.
- **Ubicación de la config:** el exe busca `sincrogit.config.yaml` junto a sí mismo,
  luego en `%APPDATA%\SincroGit\`. En el primer arranque sin ninguna, crea una por
  defecto junto al exe (o en `%APPDATA%\SincroGit\` si esa carpeta no es escribible) y
  abre la pestaña Configuración. `sincrogit.log` se escribe junto a la config.

Notas: es un build `--onefile --noconsole` (~55 MB); el primer arranque se descomprime
en un temporal (~1–2 s). Para redirigir salida o automatizar, prefiere `python -m sincrogit`.

## Configuración

Ver [config.example.yaml](config.example.yaml). Claves principales (`defaults`,
sobreescribibles por repo):

| Clave | Por defecto | Significado |
|-------|-------------|-------------|
| `snapshot_interval_sec` | 300 | Cada cuánto se amendea el WIP (5 min) |
| `debounce_sec` | 25 | Espera tras el último cambio antes del snapshot |
| `seal_interval_min` | 360 | Cada cuánto se sella un commit permanente (6 h) |
| `autosnap` | true | Espejo en vivo de HEAD a `refs/autosnap/<user>/<host>/<rama>` (recuperación ante fallo de disco + relevo) |
| `autosnap_interval_min` | 30 | Cada cuánto se hace force-push del espejo (solo si cambió) |
| `live_handoff` | auto | Recoger el WIP vivo de tu otra máquina: `auto` (fast-forward + notifica), `ask` (aplicar a un clic), `off`. Ver [Relevo entre máquinas](#relevo-entre-máquinas-wip-vivo) |
| `track_current_branch` | false | Seguir la rama **actual** en vez de pausar fuera de `branch` (flujo de feature branches; se acopla con el modo purista). Opt-in |
| `suggest_excludes` | true | Sugerir (una vez, notificación) añadir una carpeta ruidosa a `extra_excludes` — nunca auto-edita |
| `max_file_bytes` | 1048576 | Tamaño máximo de fichero a versionar (1 MB) |
| `extra_excludes` | — | Patrones estilo `.gitignore` a excluir |
| `extra_includes` | — | patrones versionados aunque sean binarios (p. ej. `**/*.docx`) |
| `max_include_bytes` | 26214400 | tope de tamaño (25 MB) para `extra_includes` |
| `pandoc_path` | `pandoc` | **(top-level)** ruta a pandoc para diffs legibles de `.docx` |

Los valores se **validan al cargar**: los campos numéricos aceptan números o strings
numéricos (`"300"`), y un booleano, un negativo o un valor sin sentido fallan al arrancar
con un error claro por campo — nunca como un crash dentro del motor horas después.

### Desactivar un intervalo o límite

Cualquier intervalo o umbral de tamaño se puede **apagar** poniéndolo en `inf` (o `off`,
`none`, `never`): la acción **no se dispara nunca** y el límite pasa a ser **ilimitado**.
Funciona para `snapshot_interval_sec`, `seal_interval_min`, `pull_interval_min`,
`autosnap_interval_min`, `debounce_sec`, `max_file_bytes` y `max_include_bytes`. El uso
estrella es el **modo purista**: `seal_interval_min: inf` (sin sellado automático —
commiteas a mano). Por ejemplo:

```yaml
defaults:
  seal_interval_min: inf     # modo purista: nunca auto-sella (commit manual)
  pull_interval_min: off     # no hacer pull automático
```

> ⚠️ **Desactivar un límite de *tamaño* es peligroso.** `max_file_bytes: inf` (o
> `max_include_bytes: inf`) elimina por completo la guarda de tamaño, así que SincroGit
> podría auto-commitear **ficheros enormes** — binarios de varios GB, salidas de build,
> datasets — y Git guarda **cada versión para siempre**, hinchando el repo de forma
> irreversible. Prefiere un número alto explícito (p. ej. `max_file_bytes: 10485760` para
> 10 MB) antes que `inf`, salvo que de verdad quieras decir "sin límite alguno".

### Afinar un repo "en caliente"

Cada clave de `defaults:` se puede **sobreescribir por repo**, así que puedes mantener
defaults relajados en todo y poner solo un repo "en caliente" —versionado más fino y espejado
más a menudo— sin martillear la red con todos:

```yaml
defaults:
  snapshot_interval_sec: 300      # 5 min — relajado para la mayoría
  autosnap_interval_min: 30       # espejo cada 30 min

repos:
  - path: "C:/trabajo/deadline-gordo"  # el caliente
    snapshot_interval_sec: 120         # máquina del tiempo más fina (2 min)
    autosnap_interval_min: 10          # ventana de fallo de disco menor (10 min)
  - path: "C:/trabajo/proyecto-lateral"  # se queda con los defaults relajados
```

Qué te da "en caliente": una **máquina del tiempo más fina** (`snapshot_interval_sec` menor,
local y barato) y una **ventana de fallo de disco menor** (`autosnap_interval_min` menor). Qué
cuesta: más force-pushes (y objetos huérfanos en el remoto) para *ese* repo mientras lo editas
activamente — el loop está inactivo cuando nada cambia, así que un repo caliente no cuesta nada
cuando no lo tocas. Ojo: **no** lo necesitas para el relevo entre máquinas: el
[relevo por eventos de SO](#relevo-entre-máquinas-wip-vivo) ya lo hace pronto sea cual sea el
intervalo — "en caliente" es para un undo más fino y un RPO de disco más ajustado.
