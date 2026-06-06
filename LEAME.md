# SincroGit

Sincronización automática estilo Dropbox, pero con **versionado robusto sobre Git**.
Hace *snapshots* automáticos de tus repos cada pocos minutos (auto-backup ante cortes
de luz) y "sella" commits con historial limpio cada 2 horas.

> Diseño completo y decisiones en **[DISENO.md](DISENO.md)**.

## Estado: Fases 1 y 2 completas

**Fase 1 (núcleo local):**

- ✅ Watcher del sistema de ficheros (`watchdog`) + *debounce*.
- ✅ **Snapshot** cada 5 min: `git commit --amend` sobre un commit WIP (no acumula commits).
- ✅ Snapshot inicial al arrancar (captura cambios previos, p. ej. tras un reinicio).
- ✅ **Sellado** cada 2 h: convierte el WIP en commit permanente + crea un WIP nuevo.
- ✅ **Filtro**: solo versiona automáticamente texto < 1 MB; binarios/grandes a mano.
- ✅ Mensaje de commit de *fallback* (determinista) al sellar.
- ✅ Apagado limpio con snapshot local final.
- ✅ Logging a fichero rotativo + consola.

**Fase 2 (IA + sincronización remota):**

- ✅ **Mensajes con IA** al sellar, modo híbrido: Ollama (local) → Gemini (nube) →
  fallback determinista. Nunca bloquea el commit si la IA falla.
- ✅ Privacidad: a la nube solo se manda el contenido si `cloud_send_content: true`.
- ✅ **Push** de los commits sellados (nunca el WIP) tras sellar + reintento en cada sync.
- ✅ **Pull periódico** (cada 10 min): `fetch` + rebase del WIP solo si el remoto adelanta.
- ✅ **Conflictos**: el rebase se aborta, el repo se pausa y se notifica. Nunca force,
  nunca pérdida de datos.

**Fase 4 (interfaz de bandeja):**

- ✅ **Icono en la bandeja del sistema** con la marca de SincroGit (una "G" con
  reloj de arena). El **color refleja el estado**: verde=activo, ámbar=pausado,
  rojo=conflicto, gris=parado.
- ✅ **Menú** de bandeja: abrir panel, pausar/reanudar, sincronizar ahora, sellar
  ahora, salir.
- ✅ **Panel de control** con pestañas:
  - *Estado*: tabla de repos (rama, estado, último snapshot/sellado, última acción)
    y botones de acción.
  - *Registro*: eventos **filtrables por repo, acción, nivel y texto**.
  - *Configuración*: editor del `config.yaml` (guardar / guardar y reiniciar).
  - *Acerca de*.
- ✅ **Notificaciones** de escritorio (vía Qt) ante conflictos/errores.

Pendiente (Fase 3): despliegue como tarea programada de Windows (`pythonw.exe`)
para arrancar `--tray` al iniciar sesión, y comando `sincrogit status`.

## Instalación

```powershell
pip install -r requirements.txt
# o, como paquete:  pip install -e .
```

## Uso

1. Copia la configuración de ejemplo y edítala con tus repos:
   ```powershell
   copy config.example.yaml config.yaml
   ```
2. Arranca SincroGit:
   ```powershell
   # Con icono de bandeja y panel de control (recomendado):
   python -m sincrogit --tray --config config.yaml
   # …o sin ventana de consola:
   pythonw -m sincrogit --tray --config config.yaml

   # Modo headless (sin GUI), para servidores o tareas automáticas:
   python -m sincrogit --config config.yaml
   ```

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

### Modos de prueba (una pasada y salir)

```powershell
python -m sincrogit -c config.yaml --snapshot-once   # un snapshot y sale
python -m sincrogit -c config.yaml --seal-once       # fuerza un sellado (+push) y sale
python -m sincrogit -c config.yaml --sync-once       # un pull+push y sale
```

## Cómo funciona (resumen)

```
... ── sellado_N ── WIP        ← HEAD, se amendea cada 5 min (snapshot)
cada 2h: el WIP se sella (mensaje descriptivo) y nace un WIP nuevo encima
resultado: ... ── sellado_N ── sellado_N+1 ── WIP(nuevo)
```

- **Recuperar trabajo reciente** (corte de luz): el último snapshot está en `HEAD`.
  Estados intermedios de la ventana, en `git reflog`.
- **Volver a ayer**: `git checkout`/`restore` desde el commit sellado correspondiente.

## Configuración

Ver [config.example.yaml](config.example.yaml). Claves principales (`defaults`,
sobreescribibles por repo):

| Clave | Por defecto | Significado |
|-------|-------------|-------------|
| `snapshot_interval_sec` | 300 | Cada cuánto se amendea el WIP (5 min) |
| `debounce_sec` | 25 | Espera tras el último cambio antes del snapshot |
| `seal_interval_min` | 120 | Cada cuánto se sella un commit permanente (2 h) |
| `max_file_bytes` | 1048576 | Tamaño máximo de fichero a versionar (1 MB) |
| `extra_excludes` | — | Patrones estilo `.gitignore` a excluir |
