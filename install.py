#!/usr/bin/env python3
"""
install.py — Instalador multiplataforma (Linux, macOS, WSL, Windows) para freellmapi-audit.

Uso:
  python install.py                         # Instala plugin global en ~/.gemini/config/plugins/freellmapi-audit
  python install.py --init                  # Instala y ejecuta el asistente de configuración local (.env)
  python install.py --mode skill            # Instala solo la skill global en ~/.gemini/config/skills/freellmapi-audit
  python install.py --workspace /ruta/proj  # Instala dentro del proyecto (.agents/plugins o .agents/skills)
  python install.py --target /ruta/exacta   # Instala en un directorio personalizado
"""
import argparse, os, shutil, subprocess, sys
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
PLUGIN_NAME = "freellmapi-audit"
IGNORE_PATTERNS = shutil.ignore_patterns(
    "__pycache__", "*.pyc", ".env", "*.env", ".git", "historico", "history",
    "corrida_*.json", "reporte_*.md", "ultimo_*.json", "ultimo_reporte.md", "*.csv"
)


def configure_utf8():
    for s in (sys.stdout, sys.stderr):
        try:
            if hasattr(s, "reconfigure"):
                s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def resolve_dest(mode: str, workspace: str, target: str) -> Path:
    if target:
        return Path(target).expanduser().resolve()
    if workspace:
        root = Path(workspace).expanduser().resolve() / ".agents"
    else:
        root = Path.home() / ".gemini" / "config"
    sub = "plugins" if mode == "plugin" else "skills"
    return root / sub / PLUGIN_NAME


def copy_clean(src: Path, dst: Path):
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=IGNORE_PATTERNS)


def main():
    configure_utf8()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=("plugin", "skill"), default="plugin",
                    help="plugin (default: bundle completo) o skill (solo la skill)")
    ap.add_argument("--workspace", default="", help="Instalar en .agents/ de un proyecto concreto")
    ap.add_argument("--target", default="", help="Ruta destino exacta (sobrescribe --mode/--workspace)")
    ap.add_argument("--init", action="store_true", help="Ejecutar freellm_audit.py --init tras instalar")
    a = ap.parse_args()

    dest = resolve_dest(a.mode, a.workspace, a.target)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if a.mode == "plugin":
        copy_clean(PKG_DIR, dest)
        # No copiar el propio instalador ni el README al plugin activo si no hace falta, aunque no estorban;
        # dejar plugin.json + skills/ (+ README.md + .env.example como referencia).
        audit_script = dest / "skills" / PLUGIN_NAME / "scripts" / "freellm_audit.py"
    else:
        src_skill = PKG_DIR / "skills" / PLUGIN_NAME
        copy_clean(src_skill, dest)
        audit_script = dest / "scripts" / "freellm_audit.py"

    if os.name != "nt" and audit_script.is_file():
        try:
            os.chmod(audit_script, 0o755)
        except OSError:
            pass

    print(f"✅ Instalado ({a.mode}) en: {dest}")
    print(f"Script auditor: {audit_script}")

    if a.init:
        print("\nIniciando configuración local (.env)...")
        rc = subprocess.call([sys.executable, str(audit_script), "--init"])
        if rc != 0:
            sys.exit(rc)
    else:
        py_cmd = "python" if sys.platform == "win32" else "python3"
        print("\nSiguiente paso (configurar tu instancia FreeLLMAPI en este equipo):")
        print(f'  {py_cmd} "{audit_script}" --init')
        print("\nLuego puedes pedir en Antigravity: 'Audita mi FreeLLMAPI hoy' o ejecutar:")
        print(f'  {py_cmd} "{audit_script}" --quick')


if __name__ == "__main__":
    main()
