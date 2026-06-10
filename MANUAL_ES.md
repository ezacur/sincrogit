# SincroGit — Manual de usuario

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
demonios amendando los WIPs de los mismos repos competirían por git. Una segunda instancia
rehúsa arrancar (código de salida 2).

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

Una restauración la captura el siguiente snapshot, así que sigue siendo reversible.

### Recuperación entre máquinas — `--autosnaps` y `--apply-handoff REPO`

```powershell
python -m sincrogit -c config.yaml --autosnaps            # baja + lista el último espejo de cada máquina
python -m sincrogit -c config.yaml --apply-handoff mirepo # trae a mirepo el WIP vivo de tu otra máquina
```

`--autosnaps` es la lista de recuperación ante desastre (úsala en otra máquina tras un disco
muerto; los estados traídos aparecen luego también en `--history`). `--apply-handoff` es el
disparador manual del relevo entre máquinas (útil con `live_handoff: ask`, o para forzarlo ya).

---

## 4. El panel de control (GUI)

Ábrelo desde el icono de bandeja (doble clic) o *Open control panel*.

- **Status** — la tabla de repos (rama, estado, tiempo desde el último sellado, última acción)
  con botones por repo:
  - **Pause / Resume** — parar/reanudar el autosync de ese repo.
  - **Commit…** — Smart Commit (diálogo con mensaje propuesto por IA).
  - **Seal+Push** — sellar el WIP actual ya y pushear.
  - **Fetch+Pull** — traer y rebasar del remoto ahora.
  - **Apply handoff** — aparece (azul) solo cuando tu otra máquina tiene trabajo esperando (en
    modo `live_handoff: ask`).
  - Barra superior: **File history…** (explorar/previsualizar/restaurar un fichero o el repo
    entero) y **Add repo…** (opcionalmente deja un `.gitattributes` `* text=auto`).
- **Log** — eventos, filtrables por repo / acción / nivel / texto.
- **Configuration** — editar `config.yaml`; *Save* o *Save and restart* para aplicar.
- **About**.

El **color del icono de bandeja** refleja el estado: verde = activo, ámbar = pausado, rojo =
conflicto (te necesita), gris = parado. El menú de bandeja tiene además Pause/Resume, Sync now,
Seal now, Quit.

---

## 5. Tareas comunes (recetas)

| Quiero… | Haz esto |
|---------|----------|
| **Añadir un repo** | Panel → Status → *Add repo…* (o edita `repos:` en la config y *Save and restart*). |
| **Recuperar una versión anterior de un fichero** | Panel → *File history…* → elige el fichero → elige versión → *Restore*. O `--history FILE`. |
| **Revertir el repo entero** | Panel → *File history…* → *Restore whole repo* a un punto elegido. |
| **Hacer un commit limpio y documentado ya** | Botón **Commit…** del repo, o `--commit REPO`. |
| **Llevar mi trabajo a otra máquina** | Solo **bloquea la pantalla / cierra la tapa** — SincroGit vuelca; en la otra, desbloquea y sincroniza. O **Smart Commit** antes de irte para un relevo instantáneo. |
| **Recuperar tras un disco muerto** | En otra máquina: `--autosnaps` (o panel → *Fetch autosnaps*), luego *File history* / *Restore*. Pierdes ≤30 min. |
| **Dejar de escribir commits automáticos (purista)** | Pon `seal_interval_min: inf`; commitea a mano con Smart Commit. |
| **Trabajar en una feature branch (equipo)** | Pon `track_current_branch: true`, trabaja en tu rama, Smart Commit → Pull Request. Ver [LEAME → Usar en equipo](LEAME.md#usar-sincrogit-en-equipo-repos-compartidos). |
| **Sincronizar un repo más agresivo** | Sobreescribe sus intervalos por repo — ver [LEAME → Repo "en caliente"](LEAME.md#afinar-un-repo-en-caliente). |
| **Pausar todo un momento** | Bandeja → *Pause* (o *Pause* por repo). |

---

## 6. Configuración (lo esencial)

La configuración es un YAML: un bloque `defaults:` + una lista `repos:`, donde **cualquier
default se puede sobreescribir por repo**. Las claves más usadas:

| Clave | Por defecto | Significado |
|-------|-------------|-------------|
| `snapshot_interval_sec` | 300 | Cada cuánto se amendea el WIP (la granularidad de la máquina del tiempo). |
| `seal_interval_min` | 360 | Cada cuánto se sella un commit permanente (`inf` = purista: nunca auto-sella). |
| `autosnap` / `autosnap_interval_min` | true / 30 | Espejo en vivo al remoto (recuperación de disco + relevo). |
| `live_handoff` | auto | Recoger el WIP de tu otra máquina: `auto` / `ask` / `off`. |
| `track_current_branch` | false | Seguir la rama actual en vez de pausar fuera de `branch`. |
| `push` / `pull` | true / true | Pushear sellados / pull periódico. |
| `extra_excludes` / `extra_includes` | — | Rutas a saltar / binarios a versionar igualmente (p. ej. `**/*.docx`). |
| `max_file_bytes` | 1048576 | Mayor fichero auto-versionado (1 MB). |
| `suggest_excludes` | true | Sugerir excluir una carpeta ruidosa (Smart Ignore). |
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
