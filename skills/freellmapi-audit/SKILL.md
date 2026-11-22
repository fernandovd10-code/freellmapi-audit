---
name: freellmapi-audit
description: >-
  Use when the user asks to audit, check, or benchmark their FreeLLMAPI
  endpoint, detect newly added or retired models, compare changes against
  previous local runs (diff), verify VIP fallback chains, re-test providers
  after rotating API keys, or check the health of FreeLLMAPI modules (chat,
  responses, messages, embeddings, decisions, search, fusion, audio, image, or
  video).
---

# FreeLLMAPI On-Demand Audit

Ejecuta auditorías bajo demanda contra la instancia FreeLLMAPI configurada localmente en cada equipo usando [freellm_audit.py](./scripts/freellm_audit.py) y las cadenas base en [vip_chains.json](./resources/vip_chains.json).

> [!IMPORTANT]
> **Responsabilidad y aislamiento por equipo:** cada usuario habilita su propia instancia de FreeLLMAPI (dirección, puerto y API key) en el archivo local de configuración del equipo. El histórico y los reportes son **estrictamente locales por máquina** y no deben compartirse entre equipos.

## 0. Verificar configuración local

Antes de correr una auditoría, resuelve la ruta absoluta de [freellm_audit.py](./scripts/freellm_audit.py) relativa a esta `SKILL.md` (sin asumir `~`, usando `python3` en Linux/macOS/WSL o `python` / `py -3` en Windows) y ejecuta:

```bash
python3 -B <RUTA_A_SKILL>/scripts/freellm_audit.py --check-config
```

- Si devuelve código **0**, continúa al Paso 1.
- Si devuelve código **2** (falta configuración o `FREELLM_KEY`), pide al usuario la dirección/puerto y API key de su servicio FreeLLMAPI o indícale que ejecute el asistente interactivo en su terminal:
  - **Linux / macOS / WSL:** `python3 <RUTA_A_SKILL>/scripts/freellm_audit.py --init` (guarda `~/.config/freellmapi.env`)
  - **Windows:** `python <RUTA_A_SKILL>\scripts\freellm_audit.py --init` (guarda `%USERPROFILE%\.config\freellmapi.env`)

## 1. Elegir el modo de ejecución

| Intención del usuario | Flags para `freellm_audit.py` |
|---|---|
| Chequeo diario / modelos nuevos / diff / salud general | `--quick` |
| Barrido completo de todos los modelos `available: true` | `--full` |
| Revalidar proveedor(es) tras renovar API keys | `--provider <prov1,prov2> --modules` |
| Solo salud de módulos sin probar lista de chat | `--modules-only` |
| Probar generación real de video (consume cuota si hay proveedor) | Añadir `--live-video` |
| Omitir pings multimedia (Imagen y Audio TTS/STT) | Añadir `--no-media` |

- **Personalización local de cadenas VIP:** si el usuario crea `<FREELLM_DATA_DIR>/vip_chains.json` en su equipo, el script lo prioriza automáticamente sobre `./resources/vip_chains.json` para no perder ajustes tras actualizaciones.

## 2. Ejecutar respetando red y sandbox

Intenta correr el comando en el entorno estándar primero; si el sandbox bloquea el host o puerto configurado para FreeLLMAPI, reintenta con `BypassSandbox: true`. Nunca imprimas ni expongas la API key completa en la respuesta.

## 3. Leer el reporte y resumir

1. Verifica código de salida `0` y lee el archivo `ultimo_reporte.md` en la ruta mostrada al final de la salida.
2. Resume de forma accionable para el usuario:
   - **Cambios locales (`Diff`):** modelos nuevos que pasaron `OK`, modelos desaparecidos/retirados y recuperados/caídos.
   - **Salud de los 8 módulos:** estado y latencia de Chat (`auto:fast`/`auto:smart`), Responses, Messages, Embeddings, Decisions, Search, Fusión, Audio TTS/STT, Imagen y Video.
   - **Acciones requeridas:** proveedores con `api_key_invalida`, `sin_credito` o `modelo_retirado`, más estado de cadenas VIP.
   - Enlaza el reporte generado con `file://`.
