# ⏳g SincroGit — para quien sabe git y no lo usa

Sabes clonar, ramificar y commitear. Ese nunca fue el problema. El problema es que
te levantas al final del día, cierras el portátil, y el commit que ibas a hacer no
se hace — y mañana tampoco.

No pasa nada por eso. Hasta que pasa: se muere la máquina, o simplemente quieres
la versión de un fichero del martes pasado, y no hay dónde volver porque las
últimas tres semanas de trabajo son un montón indiferenciado en el disco.

SincroGit es la respuesta a *"luego commiteo"*. Vive en la bandeja y mantiene un
rastro recuperable de tu trabajo, escribas `git` o no lo escribas nunca.

> ¿Quieres los **comandos y todas las opciones**? [Manual de usuario](MANUAL_ES.md).
> ¿La ingeniería? [DISENO.md](DISENO.md). En inglés: [GUIDE.md](GUIDE.md).

## ¿Te suena?

- Te has encontrado con **semanas o meses de trabajo sin commitear** — no porque
  sea difícil, sino porque nunca hay un momento que parezca el adecuado.
- Trabajas **solo**, sobre `main`, y las ramas son ceremonia que no necesitas.
- Te gustaría la versión de **hace una hora**, y el `Ctrl+Z` se fue hace mucho.
- Usaste un sincronizador de ficheros (Dropbox, OneDrive) y te gustaba que
  *ocurriera* sin más — pero "la copia de hace 30 días, de un fichero a la vez" no
  es control de versiones.
- Tienes **dos máquinas** y el relevo siempre es un push manual que olvidas.

Si tres de esas son verdad, esto se construyó para ti. Literalmente: lo construyó
alguien a quien describe la lista.

## Lo que hace, en una frase

**Cada pocos minutos registra el estado de tus ficheros guardados, así que
cualquier momento del último mes está en algún sitio al que puedes volver — sin
que tú ejecutes `git` jamás.**

Y lo hace *al lado* de tu historial, no dentro. Tu `git log` sigue siendo tuyo, tu
área de staging no se toca nunca, y `git status` sigue diciendo la verdad. Si lo
desinstalas mañana, tu repo es un repo de Git absolutamente normal.

## Los tres ritmos

Tres relojes por repo. Puedes cambiar o apagar cualquiera.

| | Cada | Qué pasa | Dónde acaba |
|---|---|---|---|
| 🖊️ **Snapshot** | ~5 min | Tus ficheros guardados quedan registrados, invisiblemente | Un ref lateral, solo local |
| ☁️ **Espejo** | ~30 min | Ese estado se copia fuera de la máquina | `refs/autosnap/…` en tu remoto |
| 📦 **Sello** | ~6 h | El trabajo acumulado se convierte en UN commit real, con mensaje escrito por IA | Tu rama, pusheada |

Si no has tocado nada, no pasa nada — un repo en reposo no cuesta.

El sello es el único que escribe en tu rama, y es el que genera debate. Dos
respuestas honestas: déjalo puesto y tu historial gana un checkpoint `sincro:`
ordenado cada pocas horas (trivial de aplastar antes de una PR), o pon
`seal_interval_min: inf` y tu rama sigue siendo **100 % tuya** mientras los
snapshots y el espejo siguen funcionando por debajo. Las dos están soportadas a
propósito.

## Recuperar tu código

Esta es la parte por la que de verdad lo instalaste.

1. Abre el panel → la pestaña **Time machine**.
2. Elige el día a la izquierda y luego el momento. Están todos los snapshots,
   incluidos los de hace 20 minutos que no commiteaste nunca.
3. Doble clic en un fichero para fijarlo: tienes todas sus versiones, un diff
   rojo/verde contra cómo está *ahora mismo*, y un buscador sobre todas ellas.
4. Restaura el fichero, una selección de ficheros, solo algunos **bloques** de un
   fichero, o el repo entero. O **"Save a copy…"** si prefieres no sobrescribir.

Dos cosas que conviene saber. Primera, **el restore está a su vez versionado** —
se convierte en un snapshot nuevo, así que deshacer un deshacer siempre está
disponible; nada de lo que hagas aquí es de ida sin vuelta. Segunda, **se niega**
a sobrescribir contenido que no pudo capturar (un fichero excluido, algo
demasiado grande) en lugar de destruirlo en silencio, y te dice qué ficheros
mover antes.

## Por qué esto empeoró cuando la IA empezó a escribir

El modo de fallo antiguo era *"he perdido una tarde"*. Ha cambiado de forma:

- **El volumen subió.** Un agente reescribe doce ficheros en noventa segundos. Lo
  que se rompió está ahí dentro, y no estaba en ningún commit.
- **La revisión es a posteriori.** Lees el resultado, no las pulsaciones — así que
  "vuelve a antes de eso" es ya una necesidad diaria normal, no una emergencia.
- **El undo de tu agente es más estrecho de lo que crees.** Los checkpoints de los
  agentes de código de hoy cubren las ediciones hechas con sus propias
  herramientas de ficheros, dentro de una sesión. Lo que el agente hizo por la
  shell, lo que hizo un segundo agente, lo que cambiaste tú a mano en el editor
  mientras tanto — fuera de la red.

A SincroGit le da igual quién lo escribió. Fotografía el árbol de trabajo por
reloj, así que el rastro cubre al agente, a la shell, al otro agente y a ti. Nada
que instalar en el agente, nada que él tenga que hacer distinto.

Para un repo en el que trabaja un agente, sube la resolución — un punto de
retorno por ráfaga en vez de cada cinco minutos:

```yaml
repos:
  - path: "C:/work/agent-playground"
    snapshot_interval_sec: 30   # un punto recuperable cada ~30-60 s
    debounce_sec: 5             # los agentes escriben a ráfagas; asienta rápido
```

## Un día normal

**Por la mañana, en el sobremesa.** SincroGit arrancó con tu sesión de Windows.
Programas tres horas. Nada te pregunta nada.

**Te levantas a comer** y bloqueas la pantalla. Ese bloqueo es una señal: tu
último estado se va al remoto en ese momento. Si tardas más de ~20 minutos en
volver, decide que te has ido de verdad, convierte el trabajo pendiente en un
commit real y lo pushea.

**Por la tarde, en el portátil.** Lo desbloqueas y *ya* tiene el trabajo de esta
mañana — se dio cuenta de que el sobremesa iba por delante y te adelantó hasta
ahí. Sin pull, sin commit, sin pensar. Te avisa con una notificación, así que
nunca es silencioso.

**¿Has terminado algo de verdad?** No esperes al reloj de 6 h. Dale a **"Commit…"**
en ese repo: la IA propone un mensaje en Conventional Commits con todo lo hecho
desde tu último commit manual, lo editas si quieres, y se pushea.

## Cuatro reglas y listo

1. **Los ficheros grandes y los binarios siguen siendo manuales.** Solo se
   versiona automáticamente texto por debajo de 1 MB. ¿Quieres un `.jpg` o una
   `.dll` ahí? Hazle `git add` a mano una vez y SincroGit lo llevará desde
   entonces — nunca revertirá ni dejará caer un fichero que commiteaste tú.
   (Los ficheros de Word y PowerPoint *sí* se pueden versionar con diffs
   legibles — mira `extra_includes` en el [Manual](MANUAL_ES.md).)
2. **Nunca resuelve un conflicto por ti.** ¿Editaste en las dos máquinas sin
   sincronizar? Se detiene, deja los dos estados intactos, pone el icono en rojo y
   pregunta. Lo arreglas en tu editor y pulsas Resume.
3. **Cambiar de rama lo pausa.** `git checkout experiment` y se aparta en vez de
   contaminar tu experimento; de vuelta en `main`, sigue. Si de verdad vives en
   ramas de feature, `track_current_branch: true` hace que te siga.
4. **No tengas el repo dentro de Dropbox / OneDrive / Drive.** Esas herramientas
   corrompen `.git` cuando dos cosas escriben a la vez. Deja que SincroGit lleve
   Git y que ellas lleven todo lo demás.

## Lo que NO hace

- No rescata lo que **no guardaste nunca** — versiona ficheros del disco. Ese
  hueco es del autoguardado de tu editor.
- No es **tiempo real** entre máquinas. Bloquea la pantalla y el relevo tarda
  segundos; vete sin bloquear y son hasta ~40 minutos.
- No **fusiona dos máquinas a la vez**. Es por turnos, por diseño.
- No es un **backup completo**: guarda tu texto, no tu carpeta de compilación.
- No hará bonito tu historial. Los commits `sincro:` por franjas son un rastro, no
  historial curado — usa **"Commit…"** cuando quieras uno de verdad.
- Es **Windows primero**. Fuera de ahí, haces pull a mano.

## Lo que cuesta tenerlo puesto

Medido en una instalación real con cinco repos, tras siete semanas:
unos 90 MB de RAM, unos pocos segundos de CPU al día, y del orden de 7 MB extra de
`.git` en un repo de código mediano. Los snapshots son locales y no cuestan red;
el `git gc` diario mantiene el almacén empaquetado. No lo vas a notar.

## Empezar en cinco minutos

1. Consigue `SincroGit.exe` (o `pip install -e .` desde fuente — mira el
   [README](LEAME.md#instalación)).
2. Ejecútalo. Aparece el icono en la bandeja; la carpeta donde esté el exe pasa a
   ser la instalación, y ahí se crea un fichero de configuración con todas las
   opciones comentadas.
3. **"Add repo…"** → elige la carpeta. Si aún no tiene remoto, pega una URL y dale
   a Verify: comprueba que se alcanza *y* que tienes permiso de escritura antes de
   añadir nada.
4. Marca **"Start SincroGit when I sign in to Windows"** en Settings. Una vez.
5. Vuelve a trabajar y olvida que está. De eso se trata.

¿Te preocupa que algo no esté bien? `sincrogit --doctor` comprueba git, tus
remotos, las credenciales, los backends de IA y el demonio, y te dice qué
arreglar. `sincrogit status` muestra todos los repos de un vistazo.
