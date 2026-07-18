# ⏳g SincroGit — Manual de usuario

Una guía práctica, de referencia, para **operar** SincroGit: cómo arrancarlo, todos los
comandos de la CLI, las acciones del panel de control, recetas de tareas comunes, y dónde
viven sus ficheros.

> Este manual es el **cómo**. Para el **cuándo/por qué** (en qué escenarios conviene, en
> lenguaje llano) lee la **[GUIA.md](GUIA.md)**; para los internos y el porqué del diseño,
> **[DISENO.md](DISENO.md)**; para la referencia completa de configuración, el
> **[LEAME](LEAME.md#configuración)**. (English version: **[MANUAL.md](MANUAL.md)**.)

En los ejemplos, `python -m sincrogit …` y el ejecutable `SincroGit.exe …` son
intercambiables.

---

## 1. Instalación y primer arranque

```powershell
pip install -r requirements.txt        # o, como paquete:  pip install -e .
```

En el **primer arranque sin argumentos**, SincroGit crea `sincrogit.config.yaml` junto al
ejecutable y abre la pestaña Configuración — luego añades repos desde la GUI (Status →
*Add repo…*). Para partir de una plantilla a mano:

```powershell
copy config.example.yaml sincrogit.config.yaml
```

---

## 2. Arrancar SincroGit

Hay cuatro formas; el binario y `python -m sincrogit` se comportan igual.

| Invocación | Qué hace |
|------------|----------|
| *(sin argumentos)* | App de bandeja **+** demonio de fondo. **Instancia única**: un segundo lanzamiento solo trae al frente el panel ya abierto y sale. |
| `--tray [--config X]` | Lo mismo, explícito (y permite apuntar a una config concreta). |
| `--headless [--config X]` | Demonio de fondo **sin** GUI — para servidores / automatización. |
| *(un flag de un disparo)* | Ejecuta un comando de CLI y sale (ver §3). La salida va a la terminal que lo lanzó. |

La instancia única se garantiza con un mutex con nombre en Windows (más un handshake por
puerto local en otros sistemas), y aplica tanto a la bandeja **como** a `--headless` — dos
demonios fotografiando los mismos repos competirían por git. Un segundo lanzamiento
de la bandeja simplemente trae el panel en marcha al frente y sale con 0; un segundo
`--headless` rehúsa arrancar (código de salida 2).

---

## 3. Referencia de comandos CLI

Cualquier cosa distinta de `--tray`/sin-args es un **disparo único**: se ejecuta, imprime en la
terminal y sale. Todo disparo necesita una config (autodetectada, o `--config PATH`).

> Si el demonio (bandeja o headless) está corriendo, los disparos únicos **rehúsan arrancar** —
> un segundo proceso competiría con el demonio por git en los mismos repos. Usa las acciones del
> panel/bandeja, para o pausa el demonio, o pasa `--force` si sabes que es seguro.

| Comando | Para qué |
|---------|----------|
| `--config`, `-c PATH` | Usar un `config.yaml` concreto (si no, autodetectado, ver §7). |
| `--headless` | Correr el demonio sin GUI. |
| `--tray` | Lanzar la app de bandeja. |
| `--snapshot-once` | Un snapshot de todos los repos y salir. |
| `--seal-once` | Forzar un sellado (+push) de todos los repos y salir. |
| `--sync-once` | Un ciclo de sync (fetch + pull + push) de todos los repos y salir. |
| `--commit REPO` | "Smart Commit" manual de REPO (ver abajo). |
| `--message`, `-m MSG` | Con `--commit`: usar MSG directamente (salta la propuesta/IA/editor). |
| `--yes`, `-y` | Con `--commit`: aceptar el mensaje propuesto por la IA sin editar. |
| `--history FILE` | Mostrar el historial de versiones de FILE y restaurar una. |
| `--pick N` | Con `--history`: restaurar la versión N sin interacción. |
| `--autosnaps` | Bajar + listar los puntos de recuperación autosnap (por máquina) de cada repo. |
| `--apply-handoff REPO` | Aplicar a REPO el trabajo vivo pendiente de tu otra máquina. |
| `--doctor` | Chequeo de salud: git, config, rama/remoto/credenciales de cada repo (push --dry-run), pandoc, backends de IA, demonio. Exit 0 = sano. |
| `--force` | Ejecutar un disparo único aunque el demonio esté corriendo (salta el rechazo de seguridad). |
| `--help`, `-h` | Mostrar el uso y salir. |

### Commit manual — `--commit REPO`

Sella tu trabajo actual con un mensaje curado en vez de esperar al sellado automático.
SincroGit propone un mensaje **Conventional Commits** (cubre todo desde tu último commit
manual) y lo abre en tu editor; al guardar, sella y pushea.

```powershell
python -m sincrogit -c config.yaml --commit mirepo                  # edita la propuesta en $EDITOR
python -m sincrogit -c config.yaml --commit mirepo -y               # acepta la propuesta tal cual
python -m sincrogit -c config.yaml --commit mirepo -m "feat: add X" # usa tu propio mensaje
```

El editor se resuelve estilo git: `GIT_EDITOR` → `VISUAL` → `EDITOR` → `core.editor` de git →
Notepad. La propuesta necesita un backend de IA (Ollama o una key de Gemini); sin él cae a un
mensaje determinista.

### Máquina del tiempo — `--history FILE [--pick N]`

Lista las versiones pasadas de un fichero (sellados + snapshots de ~5 min del reflog + estados
autosnap traídos) y restaura la que elijas.

```powershell
python -m sincrogit -c config.yaml --history src\app.py            # interactivo: lista y pregunta
python -m sincrogit -c config.yaml --history src\app.py --pick 3   # restaura la versión 3 directa
```

Las ediciones pendientes se fotografían (en la cadena shadow) *antes* de que la restauración toque
nada, y la restauración a su vez queda capturada — así que restaurar es siempre
reversible, hasta el momento justo anterior.

### Recuperación entre máquinas — `--autosnaps` y `--apply-handoff REPO`

```powershell
python -m sincrogit -c config.yaml --autosnaps            # baja + lista el último espejo de cada máquina
python -m sincrogit -c config.yaml --apply-handoff mirepo # trae a mirepo el trabajo vivo de tu otra máquina
```

`--autosnaps` es la lista de recuperación ante desastre (úsala en otra máquina tras un disco
muerto; los estados traídos aparecen luego también en `--history`). `--apply-handoff` es el
disparador manual del relevo entre máquinas (útil con `live_handoff: ask`, o para forzarlo ya).

---

## 4. El panel de control (GUI)

Ábrelo desde el icono de bandeja (doble clic) o *Open control panel*.

- **Status** — la tabla de repos (rama, estado, tiempo desde el último sellado, última acción)
  con botones por repo. Al pasar el ratón por la celda **State** se explica el estado (por qué
  un conflicto pausó el repo, qué es un relevo pendiente, por qué un merge/rebase muestra
  *Busy*). Botones:
  - **Pause / Resume** — parar/reanudar el autosync de ese repo.
  - **Properties…** — la configuración de ese repo como formulario (rama, remoto, ritmos,
    sync, modo de relevo, filtros de ficheros) en vez de YAML. Solo se escriben los campos
    que cambies; el resto sigue heredando los defaults. Incluye **Remove repo…** (solo de la
    config — el repo git del disco no se toca). Se aplica al reiniciar.
  - **Commit…** — Smart Commit (diálogo con mensaje propuesto por IA).
  - **Seal+Push** — sellar ya los snapshots pendientes en un commit real y pushear.
  - **Fetch+Pull** — traer y rebasar del remoto ahora.
  - Mientras uno de estos (o un sync del motor) está en marcha, la barra dice *working…* y
    los botones se desactivan — el resultado (incluidos "nothing to seal" o un rechazo)
    aparece en el Log.
  - **Apply handoff** — aparece (azul) solo cuando tu otra máquina tiene trabajo esperando (en
    modo `live_handoff: ask`). Muestra qué va a hacer (qué máquina, de hace cuánto) antes
    de aplicar.
  - **How to fix…** — aparece cuando un repo está pausado por conflicto: explica qué pasó
    (el rebase se abortó; tus ficheros están intactos) y qué hacer, con botón *Open folder*.
  - Barra superior: **Time machine…** (salta a la pestaña Time machine enfocada en este
    repo — todo el pasado del repo vive ahí), **Machines…** (el último espejo autosnap de cada máquina,
    con la frescura por color — detecta una máquina que dejó de respaldarse, y *Fetch
    latest* para refrescar) y **Add repo…** (opcionalmente deja un `.gitattributes`
    `* text=auto`; también puedes pegar una **URL de remoto** y **Verify**-icarla
    —accesibilidad más un push --dry-run para el acceso de escritura— antes de añadir, para
    que push/pull/sync funcionen desde el principio).
  - Clic derecho en una fila: **Open folder / Time machine / Properties**.
  - Una línea de **resumen de actividad** bajo la barra de acciones: los snapshots /
    seals / pushes / pulls de hoy (el detalle está en el Log; esto es el vistazo).
- **Time machine** — todas las vistas del pasado del repo, en una sola rejilla. El raíl
  izquierdo lista los estados día a día — cada snapshot (~5 min), cada sellado y (tras
  *Fetch autosnaps*) los espejos de tus otras máquinas, con color por tipo — y se
  refresca solo según llegan snapshots nuevos (*Seals only* deja solo los commits
  permanentes). El conmutador **Compare** decide la pregunta que responde la derecha:
  - *what changed then* (por defecto): los ficheros que capturó ese estado (estado y
    contadores +/−) y, por fichero, el **diff coloreado** de exactamente lo que guardó.
  - *vs today*: **todos los ficheros que difieren del presente** en ese estado, cada uno
    con su checkbox y su acción (*revert* / *delete* / *recreate*), más el diff del
    fichero pulsado (**unificado o side-by-side**, con resaltado intra-línea).
    **Restore selected (N)** recupera el conjunto marcado en UN paso atómico, capturado
    como un único snapshot (reversible, como siempre); los ficheros cuyo contenido
    actual los snapshots no pueden capturar aparecen con ⚠ y no se pueden seleccionar.
    **Restore ENTIRE repo…** calcula primero una **vista previa** de qué cambiaría
    exactamente (cuántos ficheros vuelven atrás / desaparecen / regresan, la lista
    completa en Details, los ficheros en riesgo marcados) para que confirmes con datos,
    no a ciegas.

  **Fija un fichero** (doble clic en él, o *Pin a file…*) para seguir UN fichero a
  través del tiempo: el raíl pasa a ser sus versiones (tiempos relativos, tipos con
  color: sellado / snapshot / autosnap — el tooltip explica cada uno). El campo de
  búsqueda cuenta un texto en todas las versiones y resalta dónde apareció, cambió o
  desapareció ("¿cuándo cambió esta función?"). **Save a copy…** escribe la versión
  elegida en un fichero NUEVO (sugerido `nombre (fecha).ext`) — recuperar una versión
  vieja con otro nombre, sin sobrescribir nada. **Restore file** revierte el fichero
  entero; **Restore hunks…** abre un selector donde marcas solo los bloques cambiados
  que quieres recuperar (solo ficheros de texto), conservando el resto de tus ediciones
  actuales — la restauración parcial también queda capturada como snapshot.
- **Log** — eventos, lo más nuevo arriba y actualizándose en vivo (sin refresco manual);
  filtrables por repo / acción / nivel / texto, incluido el detalle DEBUG del log de fichero.
- **Settings** — el formulario amable: ritmos (cadencia de snapshot, más un selector de
  **Historia permanente**: *Checkpoints automáticos* — el auto-sellado recomendado — o
  *Solo mis propios commits*, es decir modo purista, con un **recordatorio de commit**
  opcional, como mucho una vez al día, cuando se acumula trabajo), backup y sync (autosnap,
  modo de relevo, seguir-rama), mensajes de IA, tema (claro/oscuro/auto), ruta de pandoc,
  nivel de log. Edita los defaults globales; *Save and restart* para aplicar.
- **Advanced (YAML)** — el editor del `config.yaml` crudo, para overrides por repo y comentarios.

El **color del icono de bandeja** refleja el estado: verde = activo, ámbar = pausado, rojo =
conflicto (te necesita), gris = parado. El menú de bandeja tiene además Pause/Resume, Sync now,
Seal now, Quit.

---

## 5. Tareas comunes (recetas)

| Quiero… | Haz esto |
|---------|----------|
| **Añadir un repo** | Panel → Status → *Add repo…* (o edita `repos:` en la config y *Save and restart*). |
| **Cambiar la config de UN repo** | Selecciónalo → *Properties…* (rama, ritmos, sync, filtros como formulario). O edita su entrada en Advanced (YAML). |
| **Quitar un repo de SincroGit** | *Properties…* → *Remove repo…* (solo la config; el repo git del disco no se toca). |
| **Recuperar una versión anterior de un fichero** | Panel → *Time machine* → fija el fichero (doble clic) → elige versión → *Restore file*. O `--history FILE`. |
| **Recuperar una versión vieja SIN sobrescribir** | *Time machine* → elige la versión → *Save a copy…* → dale otro nombre. |
| **Saber cuándo apareció/desapareció un texto** | *Time machine* → fija el fichero → escribe el texto → *Find* (las transiciones se resaltan). |
| **Recuperar VARIOS ficheros a la vez** | Panel → *Time machine* → *vs today* → elige un estado → marca los ficheros → *Restore selected*. |
| **Comprobar que todo el montaje está sano** | `python -m sincrogit --doctor` (git, remotos, credenciales, pandoc, IA, demonio). |
| **Ver si mis otras máquinas se respaldan** | Panel → *Machines…* (los espejos rancios salen en rojo; *Fetch latest* refresca). |
| **Revertir el repo entero** | Panel → *Time machine* → elige un estado → *Restore ENTIRE repo…* (con vista previa de qué cambia). |
| **Hacer un commit limpio y documentado ya** | Botón **Commit…** del repo, o `--commit REPO`. |
| **Llevar mi trabajo a otra máquina** | Solo **bloquea la pantalla / cierra la tapa** — SincroGit vuelca; en la otra, desbloquea y sincroniza. O **Smart Commit** antes de irte para un relevo instantáneo. |
| **Recuperar tras un disco muerto** | En otra máquina: `--autosnaps` (o panel → *Time machine* → *Fetch autosnaps*), luego restaura. Pierdes ≤30 min. |
| **Un corte de luz dejó git diciendo "branch broken"** | Nada — arranca SincroGit. Detecta la ref zeroed y la restaura desde el reflog al arrancar (verás un aviso "repair" en el Log). |
| **Dejar de escribir commits automáticos (purista)** | Pon `seal_interval_min: inf`; commitea a mano con Smart Commit. |
| **Trabajar en una feature branch (equipo)** | Pon `track_current_branch: true`, trabaja en tu rama, Smart Commit → Pull Request. Ver [LEAME → Usar en equipo](LEAME.md#usar-sincrogit-en-equipo-repos-compartidos). |
| **Sincronizar un repo más agresivo** | Sobreescribe sus intervalos por repo — ver [LEAME → Repo "en caliente"](LEAME.md#afinar-un-repo-en-caliente). |
| **Pausar todo un momento** | Bandeja → *Pause* (o *Pause* por repo). |

> Una restauración nunca destruye trabajo sin guardar: las ediciones pendientes se
> fotografían primero, y si algún contenido actual es algo que los snapshots *no pueden*
> capturar (excluido, sobre el límite de tamaño, binario) la restauración se **niega** y
> nombra los ficheros a copiar antes a un lugar seguro.

---

## 6. Configuración (lo esencial)

La configuración es un YAML: un bloque `defaults:` + una lista `repos:`, donde **cualquier
default se puede sobreescribir por repo**. Las claves más usadas:

| Clave | Por defecto | Significado |
|-------|-------------|-------------|
| `snapshot_interval_sec` | 300 | Cada cuánto aterriza un snapshot en el ref lateral (la granularidad de la máquina del tiempo). |
| `seal_interval_min` | 360 | Cada cuánto se sella un commit permanente (`inf` = purista: nunca auto-sella). |
| `autosnap` / `autosnap_interval_min` | true / 30 | Espejo en vivo al remoto (recuperación de disco + relevo). |
| `live_handoff` | auto | Recoger el trabajo vivo de tu otra máquina: `auto` / `ask` / `off`. |
| `track_current_branch` | false | Seguir la rama actual en vez de pausar fuera de `branch`. |
| `push` / `pull` | true / true | Pushear sellados / pull periódico. |
| `extra_excludes` / `extra_includes` | — | Rutas a saltar / binarios a versionar igualmente (p. ej. `**/*.docx`, `**/*.pptx`). |
| `max_file_bytes` | 1048576 | Mayor fichero auto-versionado (1 MB). |
| `suggest_excludes` | true | Sugerir excluir una carpeta ruidosa (Smart Ignore). |
| `suggest_commit` | true | Solo modo purista: recordar (una vez/día, en un momento de calma) hacer Smart Commit cuando se acumula trabajo sin sellar. |
| `pandoc_path` | `pandoc` | (top-level) pandoc para diffs legibles de `.docx`. |
| `ai.*` | — | Backend de IA para mensajes de commit (Ollama / Gemini / none). |

Dos idiomas: cualquier **intervalo/umbral** se desactiva con `inf` (u `off`/`none`/`never`);
y un repo **"en caliente"** aprieta sus propios intervalos mientras los demás siguen relajados.
Ver la tabla completa y ambos idiomas en el **[LEAME → Configuración](LEAME.md#configuración)**.

---

## 7. Ficheros y ubicaciones

- **Config:** `sincrogit.config.yaml`, buscado junto al ejecutable, luego en
  `%APPDATA%\SincroGit\`, luego el directorio de trabajo; sobreescribe con `--config PATH`.
- **Log:** `sincrogit.log` (rotativo) junto a la config; eventos estructurados en `events.jsonl`
  (también rotativo, un backup `.1`).
- **`.gitattributes`:** SincroGit puede añadir `* text=auto` (finales de línea consistentes
  entre máquinas) y, en repos con `.docx`, `*.docx -text diff=pandoc` — ambos commiteados, así
  que viajan con el repo.
- **Refs autosnap (en el remoto):** `refs/autosnap/<user>/<host>/<rama>` — espejos vivos por
  usuario y por máquina; nunca tu rama de trabajo.

---

## 8. Códigos de salida (para scripts)

Los comandos de un disparo son scriptables:

- **0** — éxito.
- **1** — el comando corrió pero no pudo completar (p. ej. nada que restaurar, relevo ya no
  seguro; también un demonio `--headless` que paró por un error inesperado del motor).
- **2** — problema de arranque (no se encontró config, config inválida, se pidió la GUI sin
  PyQt5 instalado, o una segunda instancia con SincroGit ya corriendo).

Ejemplo (un sellado nocturno programado):

```powershell
python -m sincrogit -c C:\repos\config.yaml --seal-once
```

---

## 9. Compartir el repo con otras herramientas git

SincroGit está diseñado para convivir con el resto de tus herramientas git — se inhibe
mientras *tú* operas (un merge/rebase en curso, el índice bloqueado, otra rama
checkouteada) y se reanuda después. Mientras se inhibe, tus ediciones no se están
fotografiando; si la operación manual se alarga (10+ min) el Log y un toast te avisan
una vez de que los snapshots quedan pospuestos, y el panel muestra el repo como
*Busy (merge/rebase)*. Si el *Busy* no se despeja nunca y no hay ningún comando git
corriendo de verdad, lo probable es que un crash dejara atrás un `.git/index.lock`
huérfano: el aviso lo dice, y `--doctor` señala el fichero exacto — bórralo y la
sincronización se reanuda. Las reglas de tráfico:

- **SincroGit nunca ocupa tu punta.** Los snapshots viven en un ref lateral privado
  (`refs/sincro/wip/<rama>`) construido con un índice privado: tu `git log` muestra solo
  tus commits y los sellados, tu staging es tuyo, y `git status` dice la verdad.
  Commitea, crea ramas, etiqueta o rebasa con libertad — un commit manual ya ni siquiera
  es un caso especial. (Los repos que vienen de versiones antiguas de SincroGit se
  migran solos al arrancar: el WIP legado de la punta se mueve al ref lateral y tus
  ediciones sin sellar reaparecen como cambios sin commitear normales.)
- **Clientes git (lazygit, Fork, GitKraken, VS Code, …):** sin problema en paralelo —
  ven un repositorio completamente normal.
- **GitButler (`but`):** *toma el control* del repo (hace checkout de su propia rama
  `gitbutler/workspace` y bloquea commits directos con un hook). Con la guarda de rama
  por defecto de SincroGit esto está cubierto: SincroGit **se inhibe** mientras GitButler
  está activo y se reanuda cuando sales del modo workspace (`but teardown` / checkout de
  tu rama). **No** lo combines con `track_current_branch: true` en ese repo — SincroGit
  seguiría (y pushearia) la rama workspace de GitButler. En general: una sola herramienta
  *gestionando* un repo dado a la vez.
- **Dropbox / OneDrive / Drive:** nunca metas el repo dentro de la carpeta de otra
  herramienta de sincronización — puede corromper `.git` (ver
  [LEAME → Limitaciones](LEAME.md#limitaciones)).
