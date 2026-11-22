# Arquitectura técnica — `freellmapi-audit`

Este documento detalla el diseño interno de `freellm_audit.py`, el manejo de límites del gateway, la clasificación de errores, el sistema de histórico aislado por equipo y las pruebas multimodales.

---

## 1. Estructura de almacenamiento local (aislada por máquina)

El script **nunca comparte ni sincroniza** el histórico entre equipos. Según el sistema operativo, `default_data_dir()` asigna un directorio local fuera de carpetas en la nube (por ejemplo evitando `Roaming` en Windows):

| Sistema operativo | Ruta por defecto |
|---|---|
| **Windows** | `%LOCALAPPDATA%\freellmapi-audit\history` |
| **macOS** | `~/Library/Application Support/freellmapi-audit/history` |
| **Linux / WSL** | `${XDG_STATE_HOME:-~/.local/state}/freellmapi-audit/history` |

### Archivos generados en `data_dir`

| Archivo | Propósito |
|---|---|
| `ultimo_catalogo.json` | Snapshot compacto del último `/v1/models` junto con `hostname`, `platform` y `updated_at`. |
| `estado_acumulado.json` | Último resultado observado por modelo (se actualiza incrementalmente en cada corrida). |
| `corrida_<modo>_<YYYYMMDD_HHMM>.json` | Registro estructurado de la corrida (`meta`, `diff`, `probe_diff`, `modules`, `models`). |
| `corrida_<modo>_<YYYYMMDD_HHMM>.csv` | Tabla exportable de modelos probados en esa corrida (si se ejecutaron pruebas de chat). |
| `reporte_<modo>_<YYYYMMDD_HHMM>.md` | Reporte legible en Markdown con el resultado de la auditoría. |
| `ultimo_reporte.md` | Copia siempre actualizada del último reporte generado (leída por la skill de Antigravity). |
| `vip_chains.json` *(opcional)* | Override local de cadenas VIP por equipo; si existe, prevalece sobre `resources/vip_chains.json`. |

Si `ultimo_catalogo.json` registra un `hostname` distinto al del equipo actual, el script emite una advertencia en consola recordando usar un `FREELLM_DATA_DIR` estrictamente local.

---

## 2. Control de concurrencia y límites (`RateGate`)

FreeLLMAPI aplica un límite global por ventana (habitualmente `120` peticiones por ventana en `X-RateLimit-Limit`) además de cooldowns independientes por proveedor externo.

`RateGate` sincroniza todos los hilos del `ThreadPoolExecutor`:
1. Antes de cada petición HTTP, el hilo invoca `gate.wait()`.
2. Cuando cualquier respuesta devuelve `X-RateLimit-Remaining <= 4`, se calcula la espera desde `X-RateLimit-Reset` (epoch UNIX en segundos) o `Retry-After` (topado a `90 s`) y se pausa el `gate` para todos los hilos.
3. Ante HTTP `429`, `probe_chat_model()` distingue dos escenarios:
   - **Rate limit del propio gateway** (`X-RateLimit-Remaining: 0` o ausencia de señales de proveedor como `x-fallback-trail`, `routing_exhausted`, `out_of_credits`, `reset ~`): pausa el `gate` global (mín. `5 s`, máx. `90 s`) y reintenta hasta 3 veces sin consumir los reintentos de proveedor.
   - **Cooldown corto de proveedor** (`reset ~Ns` dentro de `--max-wait`, sin `out_of_credits`): espera `N + 1` segundos solo en ese hilo y reintenta según `--retries`.

---

## 3. Clasificación de resultados, proveedor y redirecciones

### `classify(status, body_txt)`
Evalúa en orden estricto de prioridad:
1. `status is None`: `"timeout"` si aparece `timed out`; si no, `"conexion"`.
2. `status == 200`: `"ok"`.
3. `"api_key_invalida"`: HTTP `401`/`403` o cadenas `invalid_api_key`, `invalid api key`, `unauthorized`, `authentication`, `no available key`, `no_key`, `forbidden`.
4. `"sin_credito"`: HTTP `402` o `out_of_credits`, `out of credit`, `insufficient_balance`, `insufficient quota`, `quota exceeded`, `billing`, `payment required`.
5. `"deshabilitado"`: `disabled`.
6. `"modelo_retirado"`: HTTP `404`/`410` o `model_not_found`, `not found`, `removed`, `deprecated`, `stale`.
7. `"sin_capacidad"`: `lacks vision`, `unsupported`.
8. `"rate_limit"`: HTTP `429` o `rate_limit`, `cooldown`, `too many requests`, `routing_exhausted`, `exhausted`.
9. `"error_proveedor"`: HTTP `500`/`502`/`503`/`504` o `upstream`, `high demand`, `empty_completion`, `empty completion`, `provider_error`, `bad_request`.
10. Otro caso: `http_<status>`.

### `extract_provider(routed_via, fallback_trail, err_txt)`
- Si `X-Routed-Via` empieza por `"fusion"`, devuelve `"fusion"`; si contiene `/`, toma el prefijo del proveedor (ej. `google/gemini-3.5-flash` → `google`, `cloudflare/@cf/...` → `cloudflare`).
- En fallos sin `X-Routed-Via`, inspecciona `X-Fallback-Trail` y luego el mensaje de error buscando `\b([a-z0-9_-]+)/[@a-z0-9_.:-]+` (ignorando `http`, `https` y `v1`).

### `is_redirected(requested, routed_via, resp_model)`
- Marca siempre como redirigidos los alias conocidos (`auto`, `fusion`, `default`, `claude-opus-4-5`, `claude-sonnet-4-5`, `claude-haiku-4-5` y cualquier `auto:*`).
- Para modelos nominales, elimina sufijos de cuantización/capa (`:free`, `:fp8`, `:bf16`, `:nvfp4`) y normaliza caracteres no alfanuméricos antes de comparar, evitando falsos positivos por formato (`qwen-3.8-27b` vs `qwen3.8-27b`, `gpt-oss-120b` vs `GPT OSS 120B`).
- Protege contra falsos negativos cuando un modelo base cae en una variante `mini`, `lite` o `nano`.

---

## 4. Algoritmo de `Diff`

En cada ejecución se calculan dos capas de cambios:

1. **`compute_catalog_diff` (compara `/v1/models` actual vs `ultimo_catalogo.json`):**
   - `nuevos_en_catalogo`: IDs que aparecen por primera vez.
   - `eliminados_del_catalogo`: IDs que ya no figuran en `/v1/models`.
   - `nuevos_disponibles`: IDs que ahora tienen `available: true` y antes no existían o no estaban disponibles.
   - `recien_no_disponibles`: IDs que pasaron de `available: true` a `false`.
   - `nuevos_no_key` / `recuperaron_key`: entradas/salidas de `unavailable_reason == "no_key"`.

2. **`compute_probe_diff` (compara modelos probados vs `estado_acumulado.json`):**
   - `recuperados`: modelos cuyo estado previo era distinto de `ok` y ahora respondieron `ok`.
   - `caidos`: modelos que antes estaban `ok` y ahora fallaron (con causa y proveedor).
   - `nuevos_probados`: modelos probados por primera vez en el equipo.

---

## 5. Suite de ping multimodal (`ping_modules`)

Se ejecuta en paralelo (`max_workers=6`) respetando `RateGate`:

| Módulo | Endpoint y método | Payload / Comprobación |
|---|---|---|
| `chat_auto_fast` / `chat_auto_smart` | `POST /v1/chat/completions` | Prompt `PONG` con `model="auto:fast"` y `model="auto:smart"`; registra `X-Routed-Via`. |
| `responses` | `POST /v1/responses` | `instructions`, `input="ping"`, `max_output_tokens=16`; valida `output_text`. |
| `messages_anthropic` | `POST /v1/messages` | Headers `x-api-key` + `anthropic-version: 2023-06-01`; valida bloque `text`. |
| `messages_count_tokens` | `POST /v1/messages/count_tokens` | Mismos headers Anthropic; valida `input_tokens`. |
| `embeddings_auto` / `bge-m3` / `codestral` | `POST /v1/embeddings` | Verifica respuesta `200`, modelo/proveedor asignado y longitud del vector. |
| `decisions_choice` / `decisions_noul` | `POST /v1/decisions` | Valida clasificación `choice` (confianza) y predicado `noul` (probabilidad). |
| `search` | `POST /v1/search` | Consulta mínima (`max_results=1`); registra proveedor, motor y conteo de resultados. |
| `fusion_fast` | `POST /v1/chat/completions` | `model="fusion"` con `{"k": 2, "strategy": "best_of", "expose_panel": true}`; inspecciona `x_fusion.panel`. |
| `audio_tts` | `POST /v1/audio/speech` | Texto `"ok"`; verifica `200`, `Content-Type` y tamaño en bytes (omitible con `--no-media`). |
| `audio_stt` | `POST /v1/audio/transcriptions` | Genera un WAV mono en memoria (0,4 s, 16 kHz, 16-bit, tono 440 Hz) en `multipart/form-data`; valida campo `text` (omitible con `--no-media`). |
| `imagen` | `POST /v1/images/generations` | Prompt mínimo `256x256`; valida `b64_json` o `url` (omitible con `--no-media`). |
| `video` | `POST /v1/videos/generations` | **Modo seguro (default):** envía `{}` y valida HTTP `400` con `prompt` requerido (`ok_esquema`, 0 cuota). Con `--live-video`: envía prompt real (timeout `60 s`). |
