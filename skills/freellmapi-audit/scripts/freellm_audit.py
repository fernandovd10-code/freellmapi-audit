#!/usr/bin/env python3
"""
freellm_audit.py — Auditoría on-demand multiplataforma para FreeLLMAPI.

Cada usuario habilita su propia instancia (dirección, puerto y API key).
El histórico y los reportes son estrictamente locales por equipo.

Modos:
  --check-config   Verifica la configuración local (exit 0 si ok, 2 si falta key)
  --init           Asistente para configurar URL/puerto y API key en .env local
  --quick          (por defecto) Modelos nuevos + cadenas VIP + ping de módulos
  --full           Todos los modelos available=true + ping de módulos
  --provider P     Re-prueba modelos de proveedor(es) concreto(s) (ej: custom,waterfall)
  --filter S       Re-prueba modelos cuyo ID contenga subcadena(s)
  --modules-only   Solo ejecuta el ping a todos los módulos/endpoints
  --seed-from JSON Importa un barrido previo como línea base en estado_acumulado

Prioridad de configuración:
  1. Flags CLI (--base, --key, --data-dir, --vip-file, --env-file)
  2. Variables de entorno (FREELLM_BASE | FREELLM_HOST+FREELLM_PORT, FREELLM_KEY, FREELLM_DATA_DIR)
  3. Archivo ~/.config/freellmapi.env (en Windows también revisa %APPDATA%\\freellmapi\\freellmapi.env)
"""
import argparse, csv, io, json, math, os, re, socket, struct, sys, threading, time
import urllib.error, urllib.request, uuid, wave
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path


def configure_utf8_stdio():
    for stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


configure_utf8_stdio()

PROMPT_PING = "Responde exactamente con la palabra: PONG"
KNOWN_ALIASES = {"auto", "fusion", "default", "claude-opus-4-5", "claude-sonnet-4-5", "claude-haiku-4-5"}
SKILL_DIR = Path(__file__).resolve().parent.parent
VIP_PATH_DEFAULT = SKILL_DIR / "resources" / "vip_chains.json"
DEFAULT_BASE = "http://localhost:3001"
PLACEHOLDER_KEYS = {"", "freellmapi-reemplaza-con-tu-clave", "tu_api_key", "changeme"}


def default_data_dir() -> Path:
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(root) / "freellmapi-audit" / "history"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "freellmapi-audit" / "history"
    root = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(root) / "freellmapi-audit" / "history"


def resolve_env_path(cli_env: str = "") -> tuple:
    """Devuelve (ruta_lectura_existente_o_None, ruta_destino_escritura)."""
    default_write = Path.home() / ".config" / "freellmapi.env"
    if cli_env:
        p = Path(cli_env).expanduser()
        return (p if p.is_file() else None), p
    env_var = os.environ.get("FREELLM_ENV_FILE", "").strip()
    if env_var:
        p = Path(env_var).expanduser()
        return (p if p.is_file() else None), p
    if default_write.is_file():
        return default_write, default_write
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        win_alt = Path(os.environ["APPDATA"]) / "freellmapi" / "freellmapi.env"
        if win_alt.is_file():
            return win_alt, win_alt
    return None, default_write


def load_env_file(path) -> dict:
    cfg = {}
    if not path or not Path(path).is_file():
        return cfg
    for raw in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
            v = v[1:-1]
        if k:
            cfg[k] = v
    return cfg


def normalize_base(raw_base: str) -> str:
    url = (raw_base or "").strip()
    if not url:
        return DEFAULT_BASE
    if "://" not in url:
        url = "http://" + url
    url = url.rstrip("/")
    url = re.sub(r"/v1/?$", "", url, flags=re.I)
    return url


def resolve_base(cli_base: str, env_file: dict) -> str:
    if cli_base:
        return normalize_base(cli_base)
    direct = os.environ.get("FREELLM_BASE") or env_file.get("FREELLM_BASE")
    if direct:
        return normalize_base(direct)
    host = os.environ.get("FREELLM_HOST") or env_file.get("FREELLM_HOST")
    port = os.environ.get("FREELLM_PORT") or env_file.get("FREELLM_PORT")
    if host or port:
        return normalize_base(f"http://{host or 'localhost'}:{port or '3001'}")
    return DEFAULT_BASE


def resolve_vip_path(cli_vip: str, data_dir: Path) -> Path:
    if cli_vip:
        return Path(cli_vip).expanduser()
    local_override = data_dir / "vip_chains.json"
    if local_override.is_file():
        return local_override
    return VIP_PATH_DEFAULT


def mask_key(key: str) -> str:
    if not key:
        return "(no configurada)"
    if len(key) <= 10:
        return "*" * len(key)
    return f"{key[:10]}...{key[-4:]}"


class RateGate:
    """Pausa global compartida por los hilos cuando el gateway pide esperar."""
    def __init__(self):
        self.lock = threading.Lock()
        self.resume_at = 0.0

    def wait(self):
        while True:
            with self.lock:
                delay = self.resume_at - time.time()
            if delay <= 0:
                return
            time.sleep(min(delay, 5.0))

    def pause(self, secs: float):
        with self.lock:
            self.resume_at = max(self.resume_at, time.time() + max(0.0, secs))


def http_req(base, key, method, path, body=None, timeout=25, extra_headers=None,
             raw_body=None, content_type="application/json"):
    h = {"Authorization": f"Bearer {key}"}
    if raw_body is not None or body is not None:
        h["Content-Type"] = content_type
    if extra_headers:
        h.update(extra_headers)
    data = raw_body if raw_body is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(base.rstrip("/") + path, method=method, data=data, headers=h)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read(), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read(), time.time() - t0
    except Exception as e:
        return None, {}, repr(e).encode(), time.time() - t0


def classify(status, body_txt: str) -> str:
    low = body_txt.lower()
    if status is None:
        return "timeout" if "timed out" in low else "conexion"
    if status == 200:
        return "ok"
    if status in (401, 403) or any(k in low for k in (
            "invalid_api_key", "invalid api key", "unauthorized", "authentication",
            "no available key", "no_key", "forbidden")):
        return "api_key_invalida"
    if status == 402 or any(k in low for k in (
            "out_of_credits", "out of credit", "insufficient_balance",
            "insufficient quota", "quota exceeded", "billing", "payment required")):
        return "sin_credito"
    if "disabled" in low:
        return "deshabilitado"
    if status in (404, 410) or any(k in low for k in (
            "model_not_found", "not found", "removed", "deprecated", "stale")):
        return "modelo_retirado"
    if any(k in low for k in ("lacks vision", "unsupported")):
        return "sin_capacidad"
    if status == 429 or any(k in low for k in (
            "rate_limit", "cooldown", "too many requests", "routing_exhausted", "exhausted")):
        return "rate_limit"
    if status in (500, 502, 503, 504) or any(k in low for k in (
            "upstream", "high demand", "empty_completion", "empty completion",
            "provider_error", "bad_request")):
        return "error_proveedor"
    return f"http_{status}"


def extract_provider(routed_via: str, fallback_trail: str, err_txt: str) -> str:
    if routed_via:
        if routed_via.lower().startswith("fusion"):
            return "fusion"
        if "/" in routed_via:
            return routed_via.split("/", 1)[0].strip().lower()
    for src in (fallback_trail or "", err_txt or ""):
        for m in re.finditer(r"\b([a-z0-9_-]+)/[@a-z0-9_.:-]+", src, re.I):
            cand = m.group(1).lower()
            if cand not in ("http", "https", "v1"):
                return cand
    return ""


def norm_slug(s: str) -> str:
    s = re.sub(r":(free|fp8|bf16|nvfp4)$", "", s.strip(), flags=re.I)
    return re.sub(r"[^a-z0-9]", "", s.lower())


def is_redirected(requested: str, routed_via: str, resp_model: str) -> bool:
    req_low = requested.lower()
    if req_low in KNOWN_ALIASES or req_low.startswith("auto:"):
        return True
    real = routed_via.split("/", 1)[-1] if routed_via else resp_model
    if not real:
        return False
    real_tail = real.rsplit("/", 1)[-1]
    m_norm = norm_slug(requested)
    r_norm = norm_slug(real_tail)
    r_full = norm_slug(real)
    if not m_norm or not r_norm:
        return False
    if m_norm == r_norm or m_norm in r_norm or m_norm in r_full:
        if re.search(r"(mini|lite|nano)$", r_norm) and not re.search(r"(mini|lite|nano)$", m_norm):
            return True
        return False
    return True


def parse_wait_secs(reset_hdr, retry_hdr, default=30) -> int:
    if reset_hdr and str(reset_hdr).isdigit():
        return max(1, int(reset_hdr) - int(time.time()) + 1)
    if retry_hdr:
        try:
            return max(1, int(float(retry_hdr)) + 1)
        except ValueError:
            pass
    return default


def probe_chat_model(base, key, model, timeout, gate, retries, max_wait):
    body = {"model": model, "max_tokens": 16, "temperature": 0,
            "messages": [{"role": "user", "content": PROMPT_PING}]}
    attempt = 0
    gw_attempts = 0
    while True:
        gate.wait()
        status, hdrs, raw, secs = http_req(base, key, "POST", "/v1/chat/completions", body, timeout)
        h = {k.lower(): v for k, v in hdrs.items()}
        rem = h.get("x-ratelimit-remaining")
        reset = h.get("x-ratelimit-reset")
        if rem is not None and rem.isdigit() and int(rem) <= 4:
            gate.pause(min(90, parse_wait_secs(reset, h.get("retry-after"), default=30)))

        txt = raw.decode("utf-8", "replace")
        low = txt.lower()
        if status == 429:
            is_provider_rl = (
                "x-fallback-trail" in h or "routing_exhausted" in low
                or "out_of_credits" in low or "out of credit" in low
                or "all models" in low or "reset ~" in low
            )
            if (rem == "0" or not is_provider_rl) and gw_attempts < 3:
                gw_attempts += 1
                wait_gw = max(5, min(90, parse_wait_secs(reset, h.get("retry-after"), default=25)))
                gate.pause(wait_gw)
                continue
            if attempt < retries and "out_of_credits" not in low and "out of credit" not in low:
                m = re.search(r"reset ~(\d+)s", txt)
                wait = int(m.group(1)) if m else int(float(h.get("retry-after", 999) or 999))
                if wait <= max_wait:
                    attempt += 1
                    time.sleep(wait + 1)
                    continue
        break

    routed_via = h.get("x-routed-via", "")
    fallback_trail = h.get("x-fallback-trail", "")
    try:
        j = json.loads(txt)
    except Exception:
        j = None

    err_msg = ""
    if status != 200:
        err = (j or {}).get("error") if isinstance(j, dict) else None
        err_msg = ((err or {}).get("message") if isinstance(err, dict) else txt)[:260]

    res = {
        "modelo_pedido": model,
        "status": status,
        "resultado": classify(status, txt),
        "latencia_s": round(secs, 2),
        "proveedor": extract_provider(routed_via, fallback_trail, err_msg),
        "routed_via": routed_via,
        "fallback_intentos": h.get("x-fallback-attempts", ""),
        "fallback_trail": fallback_trail,
        "modelo_respuesta": "",
        "respuesta": "",
        "tokens_prompt": "",
        "tokens_salida": "",
        "redirigido": False,
        "correcto": False,
        "error": err_msg,
    }
    if status == 200 and isinstance(j, dict) and j.get("choices"):
        msg = j["choices"][0].get("message") or {}
        content = (msg.get("content") or "").strip()
        res["respuesta"] = content[:80]
        res["correcto"] = "PONG" in content.upper()
        res["modelo_respuesta"] = j.get("model", "")
        u = j.get("usage") or {}
        res["tokens_prompt"], res["tokens_salida"] = u.get("prompt_tokens", ""), u.get("completion_tokens", "")
        res["redirigido"] = is_redirected(model, routed_via, res["modelo_respuesta"])
    return res


def build_wav_multipart(model="auto"):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        frames = b"".join(struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * i / 16000))) for i in range(6400))
        w.writeframes(frames)
    bnd = uuid.uuid4().hex
    mp = (
        f"--{bnd}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{model}\r\n"
        f"--{bnd}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"ping.wav\"\r\n"
        f"Content-Type: audio/wav\r\n\r\n"
    ).encode() + buf.getvalue() + f"\r\n--{bnd}--\r\n".encode()
    return mp, f"multipart/form-data; boundary={bnd}"


def ping_modules(base, key, gate, include_media=True, live_video=False, timeout=30):
    anth_hdrs = {"x-api-key": key, "anthropic-version": "2023-06-01"}

    def run_one(name, fn):
        gate.wait()
        try:
            return name, fn()
        except Exception as e:
            return name, {"modulo": name, "estado": "error", "status": None, "latencia_s": 0.0,
                          "modelo": "", "detalle": repr(e)[:180]}

    def call_json(name, path, body, extra_hdrs=None, to=timeout, parser=None):
        st, hdrs, raw, secs = http_req(base, key, "POST", path, body=body, timeout=to, extra_headers=extra_hdrs)
        h = {k.lower(): v for k, v in hdrs.items()}
        txt = raw.decode("utf-8", "replace")
        try:
            j = json.loads(txt)
        except Exception:
            j = None
        routed = h.get("x-routed-via", "")
        if parser:
            return parser(st, h, raw, txt, j, round(secs, 2), routed)
        estado = "ok" if st == 200 else classify(st, txt)
        detalle = ""
        if st != 200:
            err = (j or {}).get("error") if isinstance(j, dict) else None
            detalle = ((err or {}).get("message") if isinstance(err, dict) else txt)[:140]
        return {"modulo": name, "estado": estado, "status": st, "latencia_s": round(secs, 2),
                "modelo": routed or ((j or {}).get("model", "") if isinstance(j, dict) else ""),
                "detalle": detalle}

    def p_chat(m_name, model):
        def _parse(st, h, raw, txt, j, secs, routed):
            if st == 200 and isinstance(j, dict):
                txt_out = ((j.get("choices") or [{}])[0].get("message") or {}).get("content", "") or ""
                return {"modulo": m_name, "estado": "ok", "status": st, "latencia_s": secs,
                        "modelo": routed or j.get("model", ""), "detalle": f"resp={txt_out.strip()[:30]!r}"}
            err = (j or {}).get("error") if isinstance(j, dict) else None
            return {"modulo": m_name, "estado": classify(st, txt), "status": st, "latencia_s": secs,
                    "modelo": extract_provider(routed, h.get("x-fallback-trail", ""), txt),
                    "detalle": ((err or {}).get("message") if isinstance(err, dict) else txt)[:140]}
        return call_json(m_name, "/v1/chat/completions",
                         {"model": model, "max_tokens": 16, "temperature": 0,
                          "messages": [{"role": "user", "content": PROMPT_PING}]},
                         parser=_parse)

    def p_responses():
        def _parse(st, h, raw, txt, j, secs, routed):
            if st == 200 and isinstance(j, dict):
                out = (j.get("output_text") or "").strip()
                return {"modulo": "responses", "estado": "ok", "status": st, "latencia_s": secs,
                        "modelo": routed or j.get("model", ""), "detalle": f"output_text={out[:30]!r}"}
            err = (j or {}).get("error") if isinstance(j, dict) else None
            return {"modulo": "responses", "estado": classify(st, txt), "status": st, "latencia_s": secs,
                    "modelo": routed, "detalle": ((err or {}).get("message") if isinstance(err, dict) else txt)[:140]}
        return call_json("responses", "/v1/responses",
                         {"model": "auto:fast", "instructions": "Responde OK.", "input": "ping", "max_output_tokens": 16},
                         parser=_parse)

    def p_messages():
        def _parse(st, h, raw, txt, j, secs, routed):
            if st == 200 and isinstance(j, dict):
                blocks = [b.get("text", "") for b in (j.get("content") or []) if b.get("type") == "text"]
                return {"modulo": "messages_anthropic", "estado": "ok", "status": st, "latencia_s": secs,
                        "modelo": routed or j.get("model", ""), "detalle": f"text={' '.join(blocks).strip()[:30]!r}"}
            err = (j or {}).get("error") if isinstance(j, dict) else None
            return {"modulo": "messages_anthropic", "estado": classify(st, txt), "status": st, "latencia_s": secs,
                    "modelo": routed, "detalle": ((err or {}).get("message") if isinstance(err, dict) else txt)[:140]}
        return call_json("messages_anthropic", "/v1/messages",
                         {"model": "auto:fast", "max_tokens": 16, "messages": [{"role": "user", "content": "Responde OK."}]},
                         extra_hdrs=anth_hdrs, parser=_parse)

    def p_count_tokens():
        def _parse(st, h, raw, txt, j, secs, routed):
            if st == 200 and isinstance(j, dict):
                return {"modulo": "messages_count_tokens", "estado": "ok", "status": st, "latencia_s": secs,
                        "modelo": "gateway", "detalle": f"input_tokens={j.get('input_tokens')}"}
            return {"modulo": "messages_count_tokens", "estado": classify(st, txt), "status": st, "latencia_s": secs,
                    "modelo": "", "detalle": txt[:120]}
        return call_json("messages_count_tokens", "/v1/messages/count_tokens",
                         {"model": "auto:fast", "messages": [{"role": "user", "content": "hola mundo"}]},
                         extra_hdrs=anth_hdrs, parser=_parse)

    def p_emb(m_name, model, text):
        def _parse(st, h, raw, txt, j, secs, routed):
            if st == 200 and isinstance(j, dict) and j.get("data"):
                vec = j["data"][0].get("embedding") or []
                prov = j.get("provider", "")
                mod = j.get("model", model)
                return {"modulo": m_name, "estado": "ok", "status": st, "latencia_s": secs,
                        "modelo": f"{prov}/{mod}" if prov else mod, "detalle": f"{len(vec)} dims"}
            err = (j or {}).get("error") if isinstance(j, dict) else None
            return {"modulo": m_name, "estado": classify(st, txt), "status": st, "latencia_s": secs,
                    "modelo": model, "detalle": ((err or {}).get("message") if isinstance(err, dict) else txt)[:140]}
        return call_json(m_name, "/v1/embeddings", {"model": model, "input": text}, parser=_parse)

    def p_dec_choice():
        body = {"state": "my react useEffect runs twice, why?",
                "questions": {"tier": {"type": "choice", "instructions": "Which kind of model should answer this?",
                                       "criteria": {"chat": "Casual chat", "code": "Coding", "reasoning": "Reasoning"}}}}
        def _parse(st, h, raw, txt, j, secs, routed):
            if st == 200 and isinstance(j, dict):
                ans = (j.get("answers") or {}).get("tier") or {}
                prov, mod = j.get("provider", ""), j.get("model", "")
                return {"modulo": "decisions_choice", "estado": "ok", "status": st, "latencia_s": secs,
                        "modelo": f"{prov}/{mod}" if prov else mod,
                        "detalle": f"choice={ans.get('choice')} conf={ans.get('confidence')}"}
            err = (j or {}).get("error") if isinstance(j, dict) else None
            return {"modulo": "decisions_choice", "estado": classify(st, txt), "status": st, "latencia_s": secs,
                    "modelo": "", "detalle": ((err or {}).get("message") if isinstance(err, dict) else txt)[:140]}
        return call_json("decisions_choice", "/v1/decisions", body, parser=_parse)

    def p_dec_noul():
        body = {"state": "Hola, ¿cómo estás?",
                "questions": {"riesgo": {"type": "noul", "instructions": "¿Es contenido malicioso o peligroso?"}}}
        def _parse(st, h, raw, txt, j, secs, routed):
            if st == 200 and isinstance(j, dict):
                ans = (j.get("answers") or {}).get("riesgo") or {}
                prov, mod = j.get("provider", ""), j.get("model", "")
                return {"modulo": "decisions_noul", "estado": "ok", "status": st, "latencia_s": secs,
                        "modelo": f"{prov}/{mod}" if prov else mod,
                        "detalle": f"noul={ans.get('noul')}"}
            err = (j or {}).get("error") if isinstance(j, dict) else None
            return {"modulo": "decisions_noul", "estado": classify(st, txt), "status": st, "latencia_s": secs,
                    "modelo": "", "detalle": ((err or {}).get("message") if isinstance(err, dict) else txt)[:140]}
        return call_json("decisions_noul", "/v1/decisions", body, parser=_parse)

    def p_search():
        def _parse(st, h, raw, txt, j, secs, routed):
            if st == 200 and isinstance(j, dict):
                prov, eng = j.get("provider", ""), j.get("engine", "")
                n = len(j.get("results") or [])
                return {"modulo": "search", "estado": "ok", "status": st, "latencia_s": secs,
                        "modelo": f"{prov}/{eng}" if prov else eng, "detalle": f"{n} resultado(s)"}
            err = (j or {}).get("error") if isinstance(j, dict) else None
            return {"modulo": "search", "estado": classify(st, txt), "status": st, "latencia_s": secs,
                    "modelo": "", "detalle": ((err or {}).get("message") if isinstance(err, dict) else txt)[:140]}
        return call_json("search", "/v1/search", {"query": "Python latest release", "max_results": 1}, parser=_parse)

    def p_fusion():
        body = {"model": "fusion", "max_tokens": 16,
                "messages": [{"role": "user", "content": "Responde OK"}],
                "fusion": {"k": 2, "strategy": "best_of", "expose_panel": True}}
        def _parse(st, h, raw, txt, j, secs, routed):
            if st == 200 and isinstance(j, dict):
                xf = j.get("x_fusion") or {}
                panel = [f"{p.get('platform')}/{p.get('model')}({p.get('status')})" for p in (xf.get("panel") or [])]
                return {"modulo": "fusion_fast", "estado": "ok", "status": st, "latencia_s": secs,
                        "modelo": routed or "fusion",
                        "detalle": f"best_of k=2 panel=[{', '.join(panel)}]"[:140]}
            err = (j or {}).get("error") if isinstance(j, dict) else None
            return {"modulo": "fusion_fast", "estado": classify(st, txt), "status": st, "latencia_s": secs,
                    "modelo": "fusion", "detalle": ((err or {}).get("message") if isinstance(err, dict) else txt)[:140]}
        return call_json("fusion_fast", "/v1/chat/completions", body, to=max(timeout, 45), parser=_parse)

    def p_tts():
        st, hdrs, raw, secs = http_req(base, key, "POST", "/v1/audio/speech",
                                       body={"model": "auto", "input": "ok", "voice": "alloy"}, timeout=timeout)
        h = {k.lower(): v for k, v in hdrs.items()}
        if st == 200:
            return {"modulo": "audio_tts", "estado": "ok", "status": st, "latencia_s": round(secs, 2),
                    "modelo": h.get("x-routed-via", "auto"),
                    "detalle": f"{h.get('content-type', 'audio')} ({len(raw)} bytes)"}
        txt = raw.decode("utf-8", "replace")
        return {"modulo": "audio_tts", "estado": classify(st, txt), "status": st, "latencia_s": round(secs, 2),
                "modelo": h.get("x-routed-via", ""), "detalle": txt[:140]}

    def p_stt():
        mp, ctype = build_wav_multipart("auto")
        st, hdrs, raw, secs = http_req(base, key, "POST", "/v1/audio/transcriptions",
                                       timeout=timeout, raw_body=mp, content_type=ctype)
        h = {k.lower(): v for k, v in hdrs.items()}
        txt = raw.decode("utf-8", "replace")
        try:
            j = json.loads(txt)
        except Exception:
            j = None
        if st == 200 and isinstance(j, dict) and "text" in j:
            return {"modulo": "audio_stt", "estado": "ok", "status": st, "latencia_s": round(secs, 2),
                    "modelo": h.get("x-routed-via", "auto"), "detalle": f"text={str(j.get('text'))[:30]!r}"}
        return {"modulo": "audio_stt", "estado": classify(st, txt), "status": st, "latencia_s": round(secs, 2),
                "modelo": h.get("x-routed-via", ""), "detalle": txt[:140]}

    def p_imagen():
        def _parse(st, h, raw, txt, j, secs, routed):
            if st == 200 and isinstance(j, dict) and (j.get("data") or []):
                item = j["data"][0]
                if "b64_json" in item:
                    det = f"b64_json ({len(item['b64_json'])} chars)"
                elif "url" in item:
                    det = f"url ({str(item['url'])[:40]})"
                else:
                    det = "data OK"
                return {"modulo": "imagen", "estado": "ok", "status": st, "latencia_s": secs,
                        "modelo": routed or j.get("model", "auto"), "detalle": det}
            err = (j or {}).get("error") if isinstance(j, dict) else None
            return {"modulo": "imagen", "estado": classify(st, txt), "status": st, "latencia_s": secs,
                    "modelo": routed, "detalle": ((err or {}).get("message") if isinstance(err, dict) else txt)[:140]}
        return call_json("imagen", "/v1/images/generations",
                         {"model": "auto", "prompt": "minimal blue square", "n": 1, "size": "256x256"},
                         to=max(timeout, 45), parser=_parse)

    def p_video():
        if live_video:
            return call_json("video", "/v1/videos/generations",
                             {"model": "auto", "prompt": "a rotating blue cube"}, to=60)
        st, hdrs, raw, secs = http_req(base, key, "POST", "/v1/videos/generations", body={}, timeout=timeout)
        txt = raw.decode("utf-8", "replace")
        if st == 400 and "prompt" in txt.lower():
            return {"modulo": "video", "estado": "ok_esquema", "status": st, "latencia_s": round(secs, 2),
                    "modelo": "validación esquema",
                    "detalle": "ruta activa y autenticada (usa --live-video para generación real)"}
        return {"modulo": "video", "estado": classify(st, txt), "status": st, "latencia_s": round(secs, 2),
                "modelo": "", "detalle": txt[:140]}

    tasks = {
        "chat_auto_fast": lambda: p_chat("chat_auto_fast", "auto:fast"),
        "chat_auto_smart": lambda: p_chat("chat_auto_smart", "auto:smart"),
        "responses": p_responses,
        "messages_anthropic": p_messages,
        "messages_count_tokens": p_count_tokens,
        "embeddings_auto": lambda: p_emb("embeddings_auto", "auto", "hola mundo"),
        "embeddings_bge_m3": lambda: p_emb("embeddings_bge_m3", "bge-m3", "hola mundo"),
        "embeddings_codestral": lambda: p_emb("embeddings_codestral", "codestral-embed", "def f(x): return x"),
        "decisions_choice": p_dec_choice,
        "decisions_noul": p_dec_noul,
        "search": p_search,
        "fusion_fast": p_fusion,
        "video": p_video,
    }
    if include_media:
        tasks.update({"audio_tts": p_tts, "audio_stt": p_stt, "imagen": p_imagen})

    order = [
        "chat_auto_fast", "chat_auto_smart", "responses", "messages_anthropic",
        "messages_count_tokens", "embeddings_auto", "embeddings_bge_m3", "embeddings_codestral",
        "decisions_choice", "decisions_noul", "search", "fusion_fast",
        "audio_tts", "audio_stt", "imagen", "video",
    ]
    out = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(run_one, name, fn): name for name, fn in tasks.items()}
        for f in as_completed(futs):
            name, res = f.result()
            out[name] = res
            mark = "✅" if res["estado"] in ("ok", "ok_esquema") else "❌"
            print(f"  [mod] {mark} {name:<22} {res['latencia_s']:>5}s  {res['estado']:<14} {res['modelo']} ({res['detalle']})",
                  flush=True)
    return [out[k] for k in order if k in out]


def load_json(path: Path, default):
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def save_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def unwrap_catalog_snapshot(raw_obj) -> tuple:
    """Devuelve (meta_dict, models_dict) tolerando formato plano heredado y formato con hostname."""
    if not isinstance(raw_obj, dict) or not raw_obj:
        return {}, {}
    if "models" in raw_obj and isinstance(raw_obj["models"], dict):
        return raw_obj, raw_obj["models"]
    return {}, raw_obj


def fetch_catalog(base, key):
    st, _, raw, _ = http_req(base, key, "GET", "/v1/models", timeout=30)
    if st != 200:
        sys.exit(f"Error consultando /v1/models en {base}: HTTP {st} {raw[:200]!r}")
    return json.loads(raw)["data"]


def compact_catalog(models):
    return {
        m["id"]: {
            "id": m["id"],
            "name": m.get("name"),
            "available": bool(m.get("available")),
            "unavailable_reason": m.get("unavailable_reason"),
            "execution_status": m.get("execution_status"),
            "context_window": m.get("context_window"),
        }
        for m in models
    }


def compute_catalog_diff(prev_cat: dict, curr_cat: dict) -> dict:
    if not prev_cat:
        return {"has_prev": False, "nuevos_en_catalogo": [], "eliminados_del_catalogo": [],
                "nuevos_disponibles": [], "recien_no_disponibles": [],
                "nuevos_no_key": [], "recuperaron_key": []}
    prev_ids, curr_ids = set(prev_cat), set(curr_cat)
    prev_avail = {i for i, m in prev_cat.items() if m.get("available")}
    curr_avail = {i for i, m in curr_cat.items() if m.get("available")}
    prev_nokey = {i for i, m in prev_cat.items() if m.get("unavailable_reason") == "no_key"}
    curr_nokey = {i for i, m in curr_cat.items() if m.get("unavailable_reason") == "no_key"}
    return {
        "has_prev": True,
        "nuevos_en_catalogo": sorted(curr_ids - prev_ids),
        "eliminados_del_catalogo": sorted(prev_ids - curr_ids),
        "nuevos_disponibles": sorted(curr_avail - prev_avail),
        "recien_no_disponibles": sorted((prev_avail & curr_ids) - curr_avail),
        "nuevos_no_key": sorted(curr_nokey - prev_nokey),
        "recuperaron_key": sorted((prev_nokey & curr_ids) - curr_nokey),
    }


def compute_probe_diff(prev_state: dict, results: list) -> dict:
    recuperados, caidos, nuevos_probados = [], [], []
    for r in results:
        mid = r["modelo_pedido"]
        prev = prev_state.get(mid)
        if not prev:
            nuevos_probados.append(mid)
        else:
            was_ok = prev.get("resultado") == "ok"
            now_ok = r["resultado"] == "ok"
            if not was_ok and now_ok:
                recuperados.append((mid, prev.get("resultado"), r.get("routed_via") or r.get("proveedor")))
            elif was_ok and not now_ok:
                caidos.append((mid, r["resultado"], r.get("proveedor") or "", r.get("error", "")[:100]))
    return {"recuperados": recuperados, "caidos": caidos, "nuevos_probados": nuevos_probados}


def render_markdown(ts_human, mode, hostname, plat, base, catalog_list, cat_diff, probe_diff,
                    modules, results, merged_state, vip_chains, csv_path, json_path):
    avail_cnt = sum(1 for m in catalog_list if m.get("available"))
    dis_cnt = sum(1 for m in catalog_list if m.get("unavailable_reason") == "disabled")
    nokey_models = [m for m in catalog_list if m.get("unavailable_reason") == "no_key"]

    lines = [
        f"# Auditoría FreeLLMAPI (`{mode}`) — {ts_human}",
        "",
        f"> Equipo: `{hostname}` (`{plat}`) · Endpoint: `{base}`",
        f"> Catálogo total: **{len(catalog_list)}** modelos (`{avail_cnt}` disponibles · `{dis_cnt}` disabled · `{len(nokey_models)}` `no_key`).",
        f"> Artefactos locales: `{json_path}`" + (f" · `{csv_path}`" if csv_path else ""),
        "",
        "## 1. Cambios desde la última auditoría (`Diff`)",
        "",
    ]
    if not cat_diff["has_prev"]:
        lines.append("- *Primera corrida local en este equipo (línea base creada ahora).*")
    else:
        lines.extend([
            f"- **Nuevos en `/v1/models`:** {len(cat_diff['nuevos_en_catalogo'])}"
            + (f" → {', '.join(f'`{x}`' for x in cat_diff['nuevos_en_catalogo'][:15])}" if cat_diff['nuevos_en_catalogo'] else ""),
            f"- **Recién habilitados (`available: true`):** {len(cat_diff['nuevos_disponibles'])}"
            + (f" → {', '.join(f'`{x}`' for x in cat_diff['nuevos_disponibles'][:15])}" if cat_diff['nuevos_disponibles'] else ""),
            f"- **Eliminados del catálogo:** {len(cat_diff['eliminados_del_catalogo'])}"
            + (f" → {', '.join(f'`{x}`' for x in cat_diff['eliminados_del_catalogo'][:15])}" if cat_diff['eliminados_del_catalogo'] else ""),
            f"- **Pasaron a no disponibles:** {len(cat_diff['recien_no_disponibles'])}"
            + (f" → {', '.join(f'`{x}`' for x in cat_diff['recien_no_disponibles'][:15])}" if cat_diff['recien_no_disponibles'] else ""),
            f"- **Cambios `no_key`:** +{len(cat_diff['nuevos_no_key'])} sin clave / -{len(cat_diff['recuperaron_key'])} desbloqueados",
        ])

    if results:
        lines.extend([
            f"- **Modelos recuperados (ahora `ok`):** {len(probe_diff['recuperados'])}"
            + ("".join(f"\n  - `{m}` (antes `{old}`) → `{via}`" for m, old, via in probe_diff['recuperados'][:15])),
            f"- **Modelos caídos (antes `ok`, ahora fallan):** {len(probe_diff['caidos'])}"
            + ("".join(f"\n  - `{m}` → `{cause}` [{prov}] {err}" for m, cause, prov, err in probe_diff['caidos'][:20])),
        ])
    lines.append("")

    if modules:
        lines.extend([
            "## 2. Salud de módulos y endpoints",
            "",
            "| Módulo | Estado | Latencia | Modelo / Proveedor | Detalle |",
            "|---|---|---:|---|---|",
        ])
        for m in modules:
            ok_flag = "✅" if m["estado"] in ("ok", "ok_esquema") else "❌"
            lines.append(
                f"| `{m['modulo']}` | {ok_flag} `{m['estado']}` ({m['status']}) | {m['latencia_s']} s | `{m['modelo']}` | {m['detalle']} |"
            )
        lines.append("")

    if vip_chains:
        lines.extend([
            "## 3. Salud de cadenas VIP",
            "",
            "| Cadena | OK / Total | Modelos OK | Alertas / Fallos |",
            "|---|---:|---|---|",
        ])
        run_map = {r["modelo_pedido"]: r for r in results}
        for cname, mids in vip_chains.items():
            ok_list, bad_list = [], []
            for mid in mids:
                st_rec = run_map.get(mid) or merged_state.get(mid)
                if not st_rec:
                    bad_list.append(f"`{mid}` (sin probar)")
                elif st_rec.get("resultado") == "ok":
                    src = "*" if mid not in run_map else ""
                    ok_list.append(f"`{mid}` ({st_rec.get('latencia_s')}s{src})")
                else:
                    bad_list.append(f"`{mid}` ({st_rec.get('resultado')})")
            lines.append(
                f"| `auto:{cname}` | **{len(ok_list)}/{len(mids)}** | {', '.join(ok_list) or '—'} | {', '.join(bad_list) or '✅ Sin fallos'} |"
            )
        lines.append("")

    if results:
        ok = [r for r in results if r["resultado"] == "ok"]
        reales = [r for r in ok if not r["redirigido"]]
        fails = Counter(r["resultado"] for r in results if r["resultado"] != "ok")
        lines.extend([
            "## 4. Resultados de modelos de chat probados",
            "",
            f"- **Probados:** {len(results)} · **OK:** {len(ok)} ({len(reales)} reales sin redirección, {len(ok)-len(reales)} redirigidos) · **Fallos:** {dict(fails.most_common())}",
            "",
            "### Diagnóstico por proveedor (esta corrida)",
            "",
            "| Proveedor | Total | Desglose |",
            "|---|---:|---|",
        ])
        by_prov = defaultdict(Counter)
        for r in results:
            by_prov[r["proveedor"] or "(sin_proveedor)"][r["resultado"]] += 1
        for prov, cnts in sorted(by_prov.items(), key=lambda kv: (-sum(kv[1].values()), kv[0])):
            det = ", ".join(f"`{k}`={v}" for k, v in cnts.most_common())
            lines.append(f"| `{prov}` | {sum(cnts.values())} | {det} |")
        lines.append("")

        reales_ok = sorted([r for r in reales if r.get("correcto")], key=lambda x: x["latencia_s"])
        if not reales_ok:
            reales_ok = sorted(reales, key=lambda x: x["latencia_s"])
        if reales_ok:
            lines.extend([
                "### Top más rápidos reales (esta corrida)",
                "",
                "| Latencia | Modelo | Contexto | Ruta real |",
                "|---:|---|---:|---|",
            ])
            for r in reales_ok[:20]:
                lines.append(
                    f"| {r['latencia_s']} s | `{r['modelo_pedido']}` | {r.get('context_window') or '—'} | `{r.get('routed_via') or r.get('modelo_respuesta')}` |"
                )
            lines.append("")

        nuevos_set = set(cat_diff.get("nuevos_disponibles", []) + cat_diff.get("nuevos_en_catalogo", []))
        nuevos_ok = [r for r in ok if r["modelo_pedido"] in nuevos_set]
        if nuevos_ok:
            lines.extend(["### Modelos nuevos que respondieron OK", ""])
            for r in nuevos_ok:
                lines.append(f"- `{r['modelo_pedido']}` ({r['latencia_s']} s) → `{r.get('routed_via') or r.get('modelo_respuesta')}`")
            lines.append("")

        retirados = [r for r in results if r["resultado"] == "modelo_retirado"]
        if retirados:
            lines.extend(["### Modelos retirados detectados (`modelo_retirado`)", ""])
            for r in retirados:
                lines.append(f"- `{r['modelo_pedido']}` (`{r['proveedor'] or '?'}`)")
            lines.append("")

    if nokey_models:
        lines.extend([
            f"## 5. Modelos `no_key` en el catálogo ({len(nokey_models)})",
            "",
            ", ".join(f"`{m['id']}`" for m in nokey_models),
            "",
        ])

    return "\n".join(lines)


def run_init(a, env_write_path: Path, current_base: str, current_key: str, data_dir: Path):
    print("Configuración local de FreeLLMAPI (solo para este equipo)\n")
    if a.base and a.key:
        base_val = normalize_base(a.base)
        key_val = a.key.strip()
    elif not sys.stdin.isatty():
        sys.exit("En modo no interactivo debes pasar --base y --key junto con --init.")
    else:
        raw_base = input(f"URL o host:puerto de FreeLLMAPI [{current_base}]: ").strip()
        if not raw_base:
            raw_host = input("¿O solo host/IP? (Enter para mantener el valor anterior): ").strip()
            if raw_host:
                raw_port = input("Puerto [3001]: ").strip() or "3001"
                raw_base = f"http://{raw_host}:{raw_port}"
            else:
                raw_base = current_base
        base_val = normalize_base(raw_base)
        prompt_k = f"API key unificada (Enter para mantener actual {mask_key(current_key)}): " if current_key else "API key unificada (freellmapi-...): "
        key_val = input(prompt_k).strip() or current_key
        if not key_val or key_val in PLACEHOLDER_KEYS:
            sys.exit("API key vacía o inválida. Cancelado.")

    print(f"\nProbando conexión contra {base_val}/v1/models ...", flush=True)
    st, _, raw, secs = http_req(base_val, key_val, "GET", "/v1/models", timeout=15)
    if st == 200:
        try:
            cnt = len(json.loads(raw).get("data") or [])
            print(f"✅ Conexión correcta ({secs:.2f}s) — {cnt} modelos en catálogo.")
        except Exception:
            print(f"✅ Respuesta 200 OK ({secs:.2f}s).")
    else:
        print(f"⚠️ No se pudo validar ahora (HTTP {st}: {raw[:160]!r}). Se guardará la configuración de todos modos.")

    env_write_path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "# Configuración local de FreeLLMAPI (específica de este equipo)\n"
        f"FREELLM_BASE={base_val}\n"
        f"FREELLM_KEY={key_val}\n"
        f"# FREELLM_DATA_DIR={data_dir}\n"
    )
    env_write_path.write_text(content, encoding="utf-8")
    if os.name != "nt":
        try:
            os.chmod(env_write_path, 0o600)
        except OSError:
            pass
    print(f"\nGuardado en: {env_write_path}")
    print(f"Histórico local asignado: {data_dir}")


def main():
    configure_utf8_stdio()
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--env-file", default="")
    pre_args, _ = pre.parse_known_args()
    env_read_path, env_write_path = resolve_env_path(pre_args.env_file)
    env_file = load_env_file(env_read_path)

    default_base = resolve_base("", env_file)
    default_key = (os.environ.get("FREELLM_KEY") or env_file.get("FREELLM_KEY") or "").strip()
    default_dir = os.environ.get("FREELLM_DATA_DIR") or env_file.get("FREELLM_DATA_DIR") or str(default_data_dir())

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env-file", default=pre_args.env_file, help="Ruta personalizada de archivo .env")
    ap.add_argument("--base", default="", help=f"URL base FreeLLMAPI (default: {default_base})")
    ap.add_argument("--key", default="", help="API key unificada de FreeLLMAPI")
    ap.add_argument("--data-dir", default=default_dir, help="Directorio local para histórico y reportes")
    ap.add_argument("--vip-file", default="", help="Ruta personalizada a vip_chains.json")

    ap.add_argument("--check-config", action="store_true", help="Verifica configuración local sin ejecutar pruebas")
    ap.add_argument("--init", action="store_true", help="Configura URL/puerto y API key en archivo .env local")
    ap.add_argument("--quick", action="store_true", help="Modelos nuevos + cadenas VIP + módulos (por defecto)")
    ap.add_argument("--full", action="store_true", help="Todos los modelos available=true + módulos")
    ap.add_argument("--provider", default="", help="Re-probar modelos de estos proveedores (separados por coma)")
    ap.add_argument("--filter", default="", help="Re-probar modelos cuyo ID contenga estas subcadenas")
    ap.add_argument("--modules-only", action="store_true", help="Solo probar módulos y endpoints")
    ap.add_argument("--seed-from", default="", help="Importar JSON previo como línea base en estado_acumulado")

    ap.add_argument("--modules", action="store_true", help="Forzar ping de módulos con --provider/--filter")
    ap.add_argument("--no-modules", action="store_true", help="Omitir ping de módulos")
    ap.add_argument("--no-media", action="store_true", help="Omitir pings de Audio TTS/STT e Imagen")
    ap.add_argument("--live-video", action="store_true", help="Enviar generación real a /v1/videos/generations")
    ap.add_argument("--include-unavailable", action="store_true")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--timeout", type=int, default=25)
    ap.add_argument("--retries", type=int, default=1)
    ap.add_argument("--max-wait", type=int, default=12)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    data_dir = Path(a.data_dir).expanduser()
    base = normalize_base(a.base) if a.base else default_base
    key = (a.key or default_key).strip()
    vip_path = resolve_vip_path(a.vip_file, data_dir)
    hostname = socket.gethostname()
    plat = sys.platform

    if a.init:
        run_init(a, env_write_path, base, key, data_dir)
        return

    if a.check_config:
        key_ok = bool(key) and key not in PLACEHOLDER_KEYS
        print(f"Equipo (hostname):  {hostname} ({plat})")
        print(f"Archivo .env:       {env_read_path or f'NO ENCONTRADO (destino: {env_write_path})'}")
        print(f"Endpoint base:      {base}")
        print(f"API key:            {mask_key(key) if key_ok else 'NO CONFIGURADA'}")
        print(f"Histórico local:    {data_dir}")
        print(f"Cadenas VIP:        {vip_path}")
        if not key_ok:
            print("\nFalta API key válida. Ejecuta con --init o crea el archivo .env local.")
            sys.exit(2)
        return

    if not key or key in PLACEHOLDER_KEYS:
        sys.exit(
            f"Falta API key de FreeLLMAPI.\n"
            f"Ejecuta `python {Path(__file__).name} --init` o define FREELLM_KEY en {env_write_path}"
        )

    data_dir.mkdir(parents=True, exist_ok=True)
    prev_cat_path = data_dir / "ultimo_catalogo.json"
    state_path = data_dir / "estado_acumulado.json"
    prev_cat_meta, prev_cat = unwrap_catalog_snapshot(load_json(prev_cat_path, {}))
    prev_state = load_json(state_path, {})
    vip_chains = load_json(vip_path, {})

    prev_host = prev_cat_meta.get("hostname")
    if prev_host and prev_host != hostname:
        print(f"⚠️ Aviso: el histórico en {data_dir} fue generado en '{prev_host}' y estás en '{hostname}'. "
              "Usa un FREELLM_DATA_DIR estrictamente local no compartido entre equipos.", flush=True)

    print(f"Consultando catálogo en {base} ...", flush=True)
    catalog_list = fetch_catalog(base, key)
    curr_cat = compact_catalog(catalog_list)
    meta = {m["id"]: m for m in catalog_list}

    if a.seed_from:
        seed_rows = load_json(Path(a.seed_from).expanduser(), [])
        if not isinstance(seed_rows, list) or not seed_rows:
            sys.exit(f"Archivo seed inválido o vacío: {a.seed_from}")
        now_iso = datetime.now().isoformat(timespec="seconds")
        for r in seed_rows:
            rec = dict(r)
            if rec.get("proveedor") == "cf":
                rec["proveedor"] = extract_provider(rec.get("routed_via", ""), rec.get("fallback_trail", ""), rec.get("error", "")) or "cloudflare"
            rec["ultima_prueba"] = now_iso
            prev_state[rec["modelo_pedido"]] = rec
        save_json(state_path, prev_state)
        save_json(prev_cat_path, {"hostname": hostname, "platform": plat, "updated_at": now_iso, "models": curr_cat})
        print(f"Línea base sembrada: {len(seed_rows)} modelos en {state_path} y {len(curr_cat)} entradas en {prev_cat_path}")
        return

    if a.full:
        mode = "full"
    elif a.modules_only:
        mode = "modules"
    elif a.provider or a.filter:
        mode = "targeted"
    else:
        mode = "quick"

    cat_diff = compute_catalog_diff(prev_cat, curr_cat)
    avail_ids = [m["id"] for m in catalog_list if a.include_unavailable or m.get("available")]

    if mode == "modules":
        target_ids = []
    elif mode == "full":
        target_ids = avail_ids
    elif mode == "targeted":
        if a.provider:
            provs = {p.strip().lower() for p in a.provider.split(",") if p.strip()}
            cand = [mid for mid, r in prev_state.items() if (r.get("proveedor") or "").lower() in provs]
            if not cand:
                print(f"Aviso: no hay modelos registrados para proveedor(es) {sorted(provs)} en {state_path}", flush=True)
            if a.filter:
                subs = [s.strip().lower() for s in a.filter.split(",") if s.strip()]
                cand = [mid for mid in cand if any(s in mid.lower() for s in subs)]
            target_ids = sorted(set(cand))
        else:
            subs = [s.strip().lower() for s in a.filter.split(",") if s.strip()]
            target_ids = [mid for mid in avail_ids if any(s in mid.lower() for s in subs)]
    else:
        all_vip = [mid for mids in vip_chains.values() for mid in mids]
        target_ids = sorted(set(all_vip) | set(cat_diff["nuevos_disponibles"]) | {"auto"})

    if a.limit and target_ids:
        target_ids = target_ids[:a.limit]

    run_modules = (mode == "modules") or (not a.no_modules and (mode in ("quick", "full") or a.modules))
    print(f"Equipo={hostname} | Modo={mode} | Catálogo={len(catalog_list)} (avail={len(avail_ids)}, "
          f"nuevos_avail={len(cat_diff['nuevos_disponibles'])}) | Chat={len(target_ids)} | "
          f"Módulos={'sí' if run_modules else 'no'}\n", flush=True)

    gate = RateGate()
    results = []
    t0 = time.time()

    if target_ids:
        with ThreadPoolExecutor(a.workers) as ex:
            futs = {ex.submit(probe_chat_model, base, key, mid, a.timeout, gate, a.retries, a.max_wait): mid
                    for mid in target_ids}
            for n, f in enumerate(as_completed(futs), 1):
                r = f.result()
                m_meta = meta.get(r["modelo_pedido"], {})
                r["context_window"] = m_meta.get("context_window", "")
                r["execution_status_inicial"] = m_meta.get("execution_status", "")
                r["ultima_prueba"] = datetime.now().isoformat(timespec="seconds")
                results.append(r)
                mark = "✅" if r["resultado"] == "ok" else "❌"
                via = f" → {r['routed_via']}" if r["routed_via"] else (f" [{r['proveedor']}]" if r["proveedor"] else "")
                print(f"[{n:>3}/{len(target_ids)}] {mark} {r['modelo_pedido']:<42} {r['latencia_s']:>5}s "
                      f"{r['resultado']:<16}{via}", flush=True)
        results.sort(key=lambda r: (r["resultado"] != "ok", r["latencia_s"]))

    modules = []
    if run_modules:
        print("\nProbando módulos / endpoints...", flush=True)
        modules = ping_modules(base, key, gate, include_media=not a.no_media,
                               live_video=a.live_video, timeout=max(a.timeout, 30))

    probe_diff = compute_probe_diff(prev_state, results)
    merged_state = dict(prev_state)
    for r in results:
        merged_state[r["modelo_pedido"]] = r

    ts_slug = datetime.now().strftime("%Y%m%d_%H%M")
    ts_human = datetime.now().strftime("%Y-%m-%d %H:%M")
    json_path = data_dir / f"corrida_{mode}_{ts_slug}.json"
    csv_path = (data_dir / f"corrida_{mode}_{ts_slug}.csv") if results else None
    md_path = data_dir / f"reporte_{mode}_{ts_slug}.md"
    latest_md_path = data_dir / "ultimo_reporte.md"

    save_json(json_path, {
        "meta": {
            "timestamp": ts_human, "hostname": hostname, "platform": plat,
            "mode": mode, "base": base, "duracion_s": round(time.time() - t0, 1),
            "catalog_total": len(catalog_list), "catalog_available": len(avail_ids),
        },
        "diff": cat_diff,
        "probe_diff": probe_diff,
        "modules": modules,
        "models": results,
    })
    if csv_path and results:
        cols = list(results[0].keys())
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(results)

    md_text = render_markdown(ts_human, mode, hostname, plat, base, catalog_list, cat_diff,
                              probe_diff, modules, results, merged_state, vip_chains, csv_path, json_path)
    md_path.write_text(md_text, encoding="utf-8")
    latest_md_path.write_text(md_text, encoding="utf-8")

    save_json(prev_cat_path, {
        "hostname": hostname,
        "platform": plat,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "models": curr_cat,
    })
    save_json(state_path, merged_state)

    print(f"\n===== COMPLETADO ({time.time()-t0:.1f}s) =====")
    print(f"Reporte Markdown: {md_path}")
    print(f"Último reporte:   {latest_md_path}")


if __name__ == "__main__":
    main()
