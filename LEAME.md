# SincroGit

Sincronización automática e instantánea, pero con **versionado robusto sobre Git**.
Hace *snapshots* automáticos de tus repos cada pocos minutos (auto-backup ante cortes
de luz), espeja el último estado al remoto cada ~30 min (**autosnap**, para recuperación
ante fallo de disco) y "sella" commits con historial limpio cada 6 horas.

> Diseño completo y decisiones en **[DISENO.md](DISENO.md)**.

## Estado: Fases 1 y 2 completas

**Fase 1 (núcleo local):**

- ✅ Watcher del sistema de ficheros (`watchdog`) + *debounce*.
- ✅ **Snapshot** cada 5 min: `git commit --amend` sobre un commit WIP (no acumula commits).
- ✅ Snapshot inicial al arrancar (captura cambios previos, p. ej. tras un reinicio).
- ✅ **Sellado** cada 6 h: convierte el WIP en commit permanente + crea un WIP nuevo.
- ✅ **Filtro**: solo versiona automáticamente texto < 1 MB; binarios/grandes a mano.
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
  de `HEAD` (incl. el WIP) a un ref lateral por máquina `refs/autosnap/<host>/<rama>`, de
  modo que un fallo total de disco pierde como mucho ~30 min. No toca la rama limpia;
  recuperación entre máquinas desde la CLI (`--autosnaps`) y el panel de control.
- ✅ **Guarda de rama**: si haces `git checkout` a otra rama, SincroGit se inhibe en ese
  repo (no snapshot/seal/push en la rama equivocada) hasta que vuelvas.

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

Pendiente (Fase 3): despliegue como tarea programada de Windows (`pythonw.exe`)
para arrancar `--tray` al iniciar sesión, y comando `sincrogit status`.

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

- **Recuperar trabajo reciente** (corte de luz): el último snapshot está en `HEAD`.
  Estados intermedios de la ventana, en `git reflog`.
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
- **El WIP es un "botón de guardar" continuo.** Un único commit se amendea cada ~5 min,
  así que un corte de luz no pierde nada; los estados intra-ventana quedan en el reflog.
- **El backup está desacoplado del historial.** `autosnap` hace force-push del estado
  vivo a un ref lateral por máquina cada ~30 min para recuperación ante fallo de disco,
  mientras `main` se mantiene limpia (solo sellados) → el pull de la otra máquina es
  siempre un fast-forward limpio.

El coste que aceptamos: el historial se lee como bloques de tiempo en vez de commits
perfectamente atómicos, y un fallo total de disco puede perder hasta ~30 min — a cambio
de backup versionado sin esfuerzo y sincronización secuencial entre máquinas.

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
| `autosnap` | true | Espejo en vivo de HEAD a `refs/autosnap/<host>/<rama>` (recuperación ante fallo de disco) |
| `autosnap_interval_min` | 30 | Cada cuánto se hace force-push del espejo (solo si cambió) |
| `max_file_bytes` | 1048576 | Tamaño máximo de fichero a versionar (1 MB) |
| `extra_excludes` | — | Patrones estilo `.gitignore` a excluir |
