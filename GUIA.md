# ⏳g SincroGit para perezosos y olvidadizos 🦥

Seamos honestos: Git es increíble, pero pide disciplina. Y a veces simplemente **no
tenemos ganas** de hacer `git add`, pensar el mensaje perfecto y `git push` cada vez que
nos levantamos a por un café. Si alguna vez sobrescribiste una versión buena con una mala y
deseaste un *deshacer*, o tu historial está lleno de mensajes tipo `asdffdsa` y `ahora sí
funciona`, estás en el sitio correcto.

SincroGit tiene **una sola regla**: *tú céntrate en programar; él mantiene una **máquina
del tiempo** versionada y silenciosa de tus ficheros guardados, para que siempre puedas
volver atrás.*

> También es perfecto para **repos de prueba y experimentales** — código que no merece un
> historial hecho a mano, pero cuyo rastro odiarías perder. Déjalo correr; no pierdas un
> spike.

> ¿Necesitas los **comandos y opciones** exactos (el *cómo*)? El [Manual de usuario](MANUAL_ES.md).
> ¿Quieres el detalle técnico? [DISENO.md](DISENO.md). (English version: [GUIDE.md](GUIDE.md).)
> Aquí vamos a lo práctico.

## 🪄 La magia de fondo: tres ritmos

Olvídate de la terminal. SincroGit te cubre las espaldas con tres ritmos automáticos:

- **🖊️ El borrador — cada ~5 min.** Mientras picas código (o miras memes mientras
  compila), toma una "foto" invisible de tus archivos **guardados**. Así, si borras una
  función, rompes algo, o solo quieres cómo estaba hace una hora, puedes volver atrás —
  aunque nunca commitearas. *Si no tocaste nada, no hace nada.* (Fotografía lo que guardaste
  en disco, no el buffer sin guardar de tu editor; de eso se encarga el autosave del editor.)
- **☁️ La copia en la nube — cada ~30 min.** Sube tu último estado a un rincón privado
  del remoto. Es tu red ante un **desastre de disco** (no la del día a día).
- **📦 El sellado — cada ~6 h.** Coge todos esos borradores invisibles, los empaqueta en
  un commit "de verdad", la **IA le redacta un resumen** decente y lo sube a tu rama.

Resultado: un historial limpio, hecho solo. Te llevas la fama de desarrollador
disciplinado… siendo el más perezoso. 😎

## 💻↔💻 Un día normal: del sobremesa al portátil

SincroGit brilla si usas más de un ordenador (y eres de los que cierran la tapa del
portátil sin hacer `push`).

**Por la mañana, en el sobremesa:**
1. Te sientas. SincroGit arranca solo y **baja en silencio** lo último que sincronizaste.
2. Programas tres horas. Ni rastro de la consola.
3. Te llaman a comer. Te levantas y te vas **sin tocar nada**.

**Antes de cambiar de máquina:** nada que *tengas* que hacer — tu trabajo se replica solo.
Lo mejor: cuando **bloqueas la pantalla o cierras la tapa**, SincroGit sube tu último estado
al remoto justo en ese momento. Así que si te vas como sueles, el relevo es de **segundos**,
no de minutos. (Si te vas sin bloquear, igual se pone al día solo en ~30 min. Y un **Smart
Commit** antes de irte es siempre instantáneo.)

**Por la tarde, en el portátil:**
- Lo abres (lo desbloqueas / despiertas) y SincroGit detecta **al instante** el trabajo más
  nuevo del sobremesa y **te adelanta hasta él** — sigues donde lo dejaste. Sin commit, sin
  pull, sin nada. (Recibes un pequeño aviso, así que nunca es silencioso. ¿Prefieres pulsar
  un botón tú antes de que cambien tus ficheros? Pon `live_handoff: ask`.)

> 🤝 **¿"Tus máquinas han divergido"?** Eso solo pasa si cambiaste cosas en **las dos**
> máquinas sin sincronizar entre medias. SincroGit no adivina cómo mezclar dos montones de
> trabajo a medias, así que deja **ambos** intactos y te pregunta. Lo más fácil: **Smart
> Commit** en una máquina y la otra sincroniza normal (receta completa en el
> [README](README.md#cross-machine-handoff-live-wip)).

> 🔥 **¿Y si el sobremesa se muere de verdad?** Para eso está la copia en la nube: en el
> portátil, *File history… → "Fetch autosnaps (other machines)…"* recupera tu último estado (de hace ≤30 min).

## ✨ Tomando el control: el commit manual (Smart Commit)

Que seas perezoso no significa que no hagas cosas importantes. Imagina que acabas de
terminar algo gordo (p. ej. *la pasarela de pagos*) y quieres dejarlo **cerrado y
documentado ya**, sin esperar 6 h.

1. En el panel, pulsa **"Commit…"** en ese repo.
2. SincroGit mira todo lo que has tocado **desde tu último commit manual** y la IA te
   propone un **título impecable + una lista con viñetas** de los cambios.
3. Lo lees, asientes (la IA escribe mejor que tú un viernes a las 18:00) y, si quieres,
   lo editas. Aceptas.
4. Ese paquete se sube y el **contador de las 6 h se reinicia**. A seguir procrastinando.

> ¿Sin ratón? Desde la terminal: `python -m sincrogit -c config.yaml --commit mirepo`
> (te abre el mensaje propuesto en tu editor para que lo retoques).

## ⏪ ¡Socorro, he roto algo! La máquina del tiempo

Has borrado una función vital, guardaste por reflejo (`Ctrl+S`)… y te das cuenta del
desastre. Que no cunda el pánico.

1. Abre el panel → **"File history…"**.
2. Elige el archivo que te cargaste.
3. Verás **todas** sus versiones (incluidos los borradores secretos de hace 15 min), con
   un **diff en rojo/verde** frente a cómo está ahora.
4. Eliges la que funcionaba, **Restaurar**, y SincroGit te devuelve el archivo a la vida.
   (Si la liaste a lo grande, también puedes restaurar el **repo entero**.)

## ⚠️ Reglas de oro para la paz mental

Solo tienes que recordar cuatro cosas:

1. **Archivos gigantes y fotos:** SincroGit ignora imágenes pesadas y binarios (solo
   versiona texto de menos de 1 MB). Si quieres subir una imagen, un `git add foto.jpg` a
   mano y listo; él dirá "ah, vale" y la incluirá en el siguiente paquete.
   *(¿Documentos Word? Sí se pueden versionar con diff legible: añade `**/*.docx` a
   `extra_includes` en la config — necesita [pandoc](https://pandoc.org). Ver el
   [README](README.md). Se versiona cuando cambias texto o estructura — la maquetación
   puramente visual, como fuente o color, no cuenta.)*
2. **Conflictos ("me he pisado a mí mismo"):** si trabajaste en las dos máquinas sin
   sincronizar, SincroGit no adivina qué versión gana. Como **nunca** es destructivo, se
   pausa (icono rojo) y te pide ayuda. Lo arreglas en tu editor y le das a **"Reanudar"**.
3. **Cambio de rama:** si te vas a otra rama desde la terminal (`git checkout pruebas`),
   SincroGit es lo bastante listo para **pausarse** y no ensuciar tus experimentos. Cuando
   vuelves a tu rama de siempre, retoma el trabajo. *(¿Trabajas con feature branches? Pon
   `track_current_branch: true` y **seguirá** cada rama en vez de pausarse.)*
4. **No metas el repo dentro de Dropbox / OneDrive / Drive.** Esas herramientas pueden
   **corromper** el `.git` al sincronizar a la vez. Deja que SincroGit gestione el Git, y
   que la otra herramienta gestione *otros* ficheros.

## 🚫 Lo que NO hace (para que no te lleves sorpresas)

- No fusiona el trabajo de **dos máquinas a la vez** (es de uso por turnos).
- No sincroniza **al instante** entre máquinas — es un relevo de unos minutos (ver arriba).
- No rescata trabajo **sin guardar** — versiona lo que **guardaste** en disco (del resto se
  encarga el autosave de tu editor). Un corte de luz con el disco intacto no pierde nada.
- No versiona **binarios ni ficheros > 1 MB** automáticamente (esos, a mano).
- No es un **backup total**: guarda tu código de texto, no toda la carpeta.
- No resuelve conflictos por ti: te avisa y los resuelves tú.
- Ante un fallo **total** de disco (raro) puedes perder **hasta ~30 min** (no es cero).

---

Y eso es todo. Cierra esta guía, abre tu editor, y relájate: del trabajo sucio —guardar,
sincronizar, etiquetar— se encarga él. 🦥
