# Seguridad

## Por qué importa

`rpc-server` de llama.cpp **no tiene autenticación**: cualquiera que alcance su puerto puede reservar memoria y
ejecutar operaciones en ese equipo. DLLM nunca lo expone: lo arranca solo en `127.0.0.1` y pone delante un proxy que
exige demostrar que se conoce el secreto del clúster.

## Qué protege DLLM

| Superficie | Protección |
|---|---|
| `rpc-server` | Solo escucha en `127.0.0.1`; accesible únicamente a través del proxy del agente |
| Proxy RPC del nodo (50051) | Handshake reto-respuesta **mutuo** HMAC-SHA256 antes de reenviar un solo byte |
| API del agente (8765) | Cada petición firmada: HMAC(hora, método, ruta, sha256 del cuerpo); ventana de 60 s; la firma se comprueba **antes** de leer el cuerpo; límite de 64 MiB |
| Origen de conexiones al nodo | `allow_ip`: por defecto el asistente solo admite la IP del orquestador (+ loopback) |
| Descubrimiento (UDP 50999) | Beacons firmados; los no firmados se ignoran y se registran |
| Panel/API del orquestador (8080) | Solo `127.0.0.1` por defecto; para exponerlo es **obligatoria** una API key (comparación en tiempo constante). Desde el propio equipo (127.0.0.1) no se pide. Las peticiones `POST` con cabecera `Origin` de otro sitio se rechazan (anti CSRF / DNS rebinding) |
| Emparejamiento de nodos sin código | Diffie-Hellman (grupo MODP de 2048 bits, RFC 3526) + código de verificación de 6 cifras que se compara a ojo y se acepta **en el nodo**; el secreto viaja cifrado y autenticado (HMAC-SHA256, encrypt-then-MAC). Solo mientras el nodo no tiene secreto; una solicitud a la vez, caduca en 5 min, máx. 10/min |
| Modelos | Las rutas se resuelven dentro de la carpeta de modelos (sin `../`) |
| Asistente web | Solo `127.0.0.1`, token aleatorio en la URL, comprobación de `Host` (anti DNS-rebinding) y `Origin` (anti CSRF); se cierra solo tras 45 min sin uso |
| Descargas de modelos | Solo a las URLs base fijas de Hugging Face y del registro de Ollama (el usuario da nombres, no URLs; se validan). sha256 publicado + cabecera GGUF; nada se usa hasta verificarlo |
| Token de Hugging Face | `~/.dllm/secrets.json` (600); solo se envía a huggingface.co: se quita de la petición si una redirección lleva a otro host (CDN) |
| Actualización ordenada por el orquestador | `POST /llama/install` firmado con HMAC; solo una etiqueta `bNNNN` validada; el agente descarga únicamente de las releases oficiales de ggml-org/llama.cpp con sha256. Desactivable con `--no-auto-update` (nodo) o `--no-auto-sync` (orquestador) |
| Descargas de llama.cpp | sha256 publicado por GitHub; extracción que rechaza path traversal, enlaces que escapan y ficheros especiales |
| Secretos en disco | `~/.dllm/config.json` con permisos 600; el servicio no lleva el secreto en su definición |
| Servicio | De usuario, sin privilegios; en Linux `NoNewPrivileges` y `PrivateTmp` |

## Qué NO protege (riesgos asumidos)

- **Confidencialidad**: prompts, respuestas y pesos viajan **sin cifrar** por la LAN. Alguien en la misma red puede
  leerlos.
- **Carga bajo demanda**: es opt-in (`--on-demand`) porque cualquier cliente con acceso a la API podría provocar cargas
  y expulsiones; la API key sigue siendo obligatoria desde la red. Solo se cargan ficheros de la carpeta de modelos
  (`resolve_model` rechaza rutas fuera de ella) y nunca se expulsan los modelos `pinned`.
- **`--allow-ip` en el orquestador**: filtra por IP de origen antes de la autenticación (loopback siempre permitido).
- **Slots**: el número de slot (0-7) forma parte de la etiqueta del handshake; cada `rpc-server` sigue escuchando solo
  en 127.0.0.1 y solo es alcanzable a través del proxy autenticado del agente.
- **Sonda de enlace**: el handshake del puerto RPC admite un segundo modo («probe», etiqueta HMAC distinta) que solo
  permite intercambiar bloques de hasta 8 MiB y 32 peticiones por conexión; requiere el mismo secreto, respeta
  `allow_ip` y no llega a `rpc-server`.
- **Repetición**: una petición firmada capturada es reutilizable durante 60 s (sin *nonce*). El handshake RPC sí usa
  retos aleatorios.
- **Un nodo comprometido** conoce el secreto: puede hacerse pasar por otro nodo o por el orquestador ante los demás
  nodos (si no tienen `allow_ip`). Rota el secreto si pierdes el control de un equipo.
- El **código de unión** lleva el secreto en claro (base64). Quien lo tenga puede unirse al clúster.
- Sin autenticación de usuarios en el panel más allá de la API key compartida. Cualquier proceso del propio
  orquestador accede al panel sin clave (como con cualquier servicio en 127.0.0.1); un proxy inverso local que lo
  publique en la red heredaría ese acceso: ponle su propia autenticación.
- El enlace «para otros equipos» (`…/#key=…`) contiene la API key: trátalo como una contraseña. La clave va tras `#`
  (no viaja al servidor ni queda en registros) y el panel la borra de la barra de direcciones al guardarla.
- Emparejamiento sin código: la seguridad depende de **comparar el código** en el panel y en el nodo. Si se acepta sin
  mirarlo, un equipo malicioso de la red que reclame antes podría quedarse con el nodo (no con el secreto del
  clúster). Los anuncios de nodos sin emparejar no van firmados (solo revelan nombre y puerto).
- `llama-server` y `rpc-server` son software de terceros; mantenlos actualizados.

## Emparejamiento sin código

1. El nodo sin secreto se anuncia por broadcast UDP (`DLLMPAIR1`, nombre y puerto). Sus únicas rutas activas son
   `/pair/*`; el proxy RPC y la API normal responden 503/cierran.
2. Al reclamarlo, orquestador y nodo intercambian claves públicas DH efímeras (256 bits de exponente sobre el grupo
   de 2048 bits) y derivan `K = SHA-256(contexto ‖ g^xy)`. El código de verificación es `HMAC(K, claves públicas)`
   reducido a 6 cifras: un intermediario tendría otra `K` y, con altísima probabilidad, otro código.
3. Solo cuando se acepta en el nodo (`/pair/local`, accesible únicamente desde 127.0.0.1), el orquestador manda el
   secreto con cifrado de flujo HMAC-SHA256 en modo contador y MAC sobre nonce + texto cifrado.
4. El nodo guarda el secreto, restringe `allow_ip` a la IP del orquestador y desactiva el emparejamiento.

## Recomendaciones de despliegue

1. **Red dedicada**: VLAN para el clúster, sin acceso desde Internet ni desde la Wi-Fi de invitados.
2. **Cifrado**: si la red no es de confianza, une los equipos con **WireGuard** y usa las IP del túnel: en el
   asistente del orquestador, opción avanzada *Dirección del orquestador para los nodos* (`--advertise-host 10.8.0.1`,
   o `dllm join-code --advertise-host 10.8.0.1`), y reinstala los nodos con ese código.
3. **Cortafuegos** en los nodos: 8765 y 50051 solo desde la IP del orquestador. En Windows el asistente crea esas
   reglas (por programa, puerto e IP remota); en Linux/macOS muestra los comandos (ver
   [INSTALACION](INSTALACION.md#cortafuegos)). En el rol *Ambos* el agente solo escucha en `127.0.0.1`.
4. No expongas el puerto 8080 fuera de la LAN; si lo necesitas, ponlo detrás de un proxy inverso con TLS y
   autenticación.
5. **Rotar el secreto**: el asistente conserva el secreto al reinstalar el orquestador. Para generar uno nuevo, borra
   la línea `"secret"` de `~/.dllm/config.json` del orquestador, vuelve a ejecutar el asistente y reinstala cada nodo
   con el código nuevo.
6. Trata la API key y el código de unión como contraseñas (gestor de contraseñas, no chats ni tickets).

## Notificar un problema de seguridad

Contacta directamente con el propietario del repositorio; no publiques detalles explotables en un *issue*.
