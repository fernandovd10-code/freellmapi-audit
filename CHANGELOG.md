# Changelog

Todos los cambios notables de este proyecto se documentan en este archivo.

## [v1.0.0] — 2026-11-22

### Lanzamiento inicial público
- **Compatibilidad 100% multiplataforma (Python 3.9+ stdlib pura):** funciona en Windows nativo (`cp1252`-safe UTF-8), macOS, Linux y WSL sin dependencias externas (`pip install` no requerido).
- **Bundle Antigravity (Plugin + Skill):** incluye `plugin.json`, `skills/freellmapi-audit/SKILL.md` e instalador multiplataforma `install.py` (`--mode plugin | skill`, `--workspace`, `--target`, `--init`).
- **Modos de auditoría on-demand (`freellm_audit.py`):**
  - `--quick`: prueba modelos nuevos en `/v1/models`, cadenas VIP (`rapido`, `codigo`, `inteligente`, `largo`, `vision`), `auto` y la suite de módulos.
  - `--full`: barrido completo de todos los modelos `available: true` + módulos.
  - `--provider <lista>` y `--filter <subcadenas>`: revalidación selectiva tras rotar claves o habilitar proveedores.
  - `--modules-only`, `--no-modules`, `--no-media`, `--live-video` y `--seed-from`.
- **Ping integral de los 8 módulos / 16 endpoints de FreeLLMAPI:**
  - Chat (`auto:fast`, `auto:smart`), Responses API (`/v1/responses`), Anthropic Messages (`/v1/messages` y `count_tokens`), Embeddings (`auto`, `bge-m3`, `codestral-embed`), Decisions (`choice`, `noul`), Web Search (`/v1/search`), Fusión (`fusion` con `k=2`, `strategy="best_of"`), Audio TTS (`/v1/audio/speech`), Audio STT (`/v1/audio/transcriptions` con WAV sintetizado en memoria), Imagen (`/v1/images/generations`) y Video (`/v1/videos/generations` con validación segura de esquema por defecto o `--live-video`).
- **Configuración local por equipo (`--init` y `--check-config`):**
  - Cada usuario habilita su propia instancia de FreeLLMAPI (URL/puerto y API key) en `~/.config/freellmapi.env` (o `%USERPROFILE%\.config\freellmapi.env`), con normalización automática de `/v1` y soporte para `FREELLM_HOST` + `FREELLM_PORT`.
- **Histórico aislado por máquina y `--diff` automático:**
  - Almacena snapshots en rutas locales del sistema operativo (`%LOCALAPPDATA%` en Windows, `~/Library/Application Support` en macOS, `~/.local/state` en Linux/WSL) con validación de `hostname`, generando reporte Markdown (`ultimo_reporte.md`), JSON y CSV.
  - Override local opcional de cadenas en `<FREELLM_DATA_DIR>/vip_chains.json`.
