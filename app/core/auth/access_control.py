"""Control de acceso global para BigPDF Desktop.

Al iniciar, consulta el Apps Script en Google Sheets y verifica si la app
está habilitada globalmente. No requiere usuario ni contraseña.

Flujo:
    check_access() → AccessResult(ok, message, kind)
    ok=True  → main.py muestra la ventana
    ok=False → main.py muestra el motivo (con "Reintentar" si fue un problema de red)

Sin conexión: se permite usar la app durante ``diasGracia`` días (definido en la
planilla) desde la última validación online exitosa. El caché queda firmado en
%LOCALAPPDATA%\\BigPDF y se invalida si se edita, si se atrasa el reloj o si
la planilla deniega el acceso.
"""
from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import json
import logging
import os
import platform
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import APP_DATA_DIR
from app.core.auth.auth_settings import (
    ACCESS_DEADLINE_SECONDS,
    ACCESS_SOURCE_URLS_B64,
    ACCESS_SOURCE_XOR_KEY,
    ACCESS_TIMEOUT_SECONDS,
    APP_ID,
    APP_VERSION,
    RECENT_VALIDATION_HOURS,
)

logger = logging.getLogger("bigpdf")

# Persistente (fuera de la carpeta temporal de sesión, que se borra en cada
# arranque): sin esto el período sin conexión nunca encontraba el caché.
_CACHE_PATH = Path(APP_DATA_DIR) / "access_cache.json"


@lru_cache(maxsize=1)
def _decode_access_url() -> str:
    """URL del Apps Script: variable de entorno tiene prioridad sobre lista ofuscada."""
    direct = os.environ.get("BIGPDF_ACCESS_URL", "").strip()
    if direct:
        return direct

    for raw in ACCESS_SOURCE_URLS_B64:
        raw = str(raw or "").strip()
        if not raw or raw.startswith("PEGAR_AQUI"):
            continue
        try:
            data = base64.b64decode(raw.encode("utf-8"))
            key = ACCESS_SOURCE_XOR_KEY.encode("utf-8") or b"k"
            decoded = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
            url = decoded.decode("utf-8").strip()
            if url:
                return url
        except Exception:
            continue
    return ""


def get_machine_name() -> str:
    for candidate in (os.environ.get("COMPUTERNAME"), platform.node(), getpass.getuser()):
        candidate = str(candidate or "").strip()
        if candidate:
            return candidate
    return "equipo"


def get_windows_user() -> str:
    for candidate in (os.environ.get("USERNAME"), getpass.getuser(), os.environ.get("USER")):
        candidate = str(candidate or "").strip()
        if candidate:
            return candidate
    return "usuario_windows"


def _os_info() -> str:
    try:
        return f"Windows {platform.version()} | Python {sys.version.split()[0]}"
    except Exception:
        return f"Python {sys.version.split()[0]}"


def post_access_json(payload: dict[str, Any], timeout: int = ACCESS_TIMEOUT_SECONDS) -> dict[str, Any]:
    """POST JSON al Apps Script y devuelve la respuesta como dict."""
    access_url = _decode_access_url()
    if not access_url:
        raise RuntimeError(
            "No hay URL de acceso configurada. "
            "Completá BIGPDF_ACCESS_URL o ACCESS_SOURCE_URLS_B64 en auth_settings.py."
        )

    req = urllib.request.Request(
        access_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": f"BigIntegraDesktop/{APP_VERSION}",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read()

    data = json.loads(body.decode("utf-8", errors="ignore"))
    if not isinstance(data, dict):
        raise RuntimeError("El servicio de acceso devolvió una respuesta no válida.")
    return data


def _cache_key() -> bytes:
    """Clave HMAC ligada a este equipo y usuario de Windows.

    No es secreto real (está en el ejecutable), pero impide que el usuario extienda
    el período sin internet editando el archivo a mano, o que copie el caché a
    otra PC. Mismo criterio que la ofuscación de la URL: disuasión, no cifrado.
    """
    material = "|".join((APP_ID, ACCESS_SOURCE_XOR_KEY, get_machine_name(), get_windows_user()))
    return hashlib.sha256(material.encode("utf-8")).digest()


def _sign(fields: dict[str, Any]) -> str:
    body = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(_cache_key(), body, hashlib.sha256).hexdigest()


def _write_cache(fields: dict[str, Any]) -> None:
    try:
        payload = dict(fields)
        payload["firma"] = _sign(fields)
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(_CACHE_PATH)
    except Exception as exc:
        logger.warning("access cache: no se pudo guardar: %s", exc)


def _save_cache(data: dict[str, Any], dias_gracia: int) -> None:
    now = int(time.time())
    _write_cache({
        "ok": True,
        "timestamp": now,       # última validación online exitosa
        "ultimoUso": now,       # último arranque visto (detecta reloj atrasado)
        "diasGracia": max(0, dias_gracia),
        "version": APP_VERSION,
    })


def _clear_cache() -> None:
    try:
        _CACHE_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def _load_cache() -> dict[str, Any] | None:
    """Lee el caché y verifica su firma. Un caché alterado se descarta."""
    try:
        if not _CACHE_PATH.exists():
            return None
        data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        firma = str(data.pop("firma", ""))
        if not firma or not hmac.compare_digest(firma, _sign(data)):
            logger.warning("access cache: firma inválida, se descarta")
            return None
        return data
    except Exception:
        return None


def _to_int(value: Any) -> int:
    """Entero tolerante: la planilla puede devolver "7", 7.0 o "7 días"."""
    match = re.match(r"\s*(-?\d+(?:[.,]\d+)?)", str(value if value is not None else ""))
    if not match:
        return 0
    return int(float(match.group(1).replace(",", ".")))


# Tolerancia para ajustes de hora (cambio de huso, sincronización NTP).
_CLOCK_SKEW_SECONDS = 2 * 3600


def _check_grace_period(reason: str) -> tuple[bool, str]:
    """Modo sin conexión: permite usar la app solo durante ``diasGracia`` días
    contados desde la última validación online exitosa. El límite lo define la
    planilla (columna DiasGracia); 0 = no se permite usar sin conexión."""
    cache = _load_cache()
    if not cache or not cache.get("ok"):
        return False, (
            f"{reason}\n\n"
            "Big PDF necesita conectarse a internet al menos una vez para habilitar "
            "el uso sin conexión. Verificá tu conexión e intentá de nuevo."
        )

    now = time.time()
    validated_at = _to_int(cache.get("timestamp"))
    last_seen = max(validated_at, _to_int(cache.get("ultimoUso")))
    dias_gracia = _to_int(cache.get("diasGracia"))

    if now + _CLOCK_SKEW_SECONDS < last_seen:
        # El reloj del equipo está antes del último uso: alguien lo atrasó para
        # estirar el período sin conexión.
        return False, (
            f"{reason}\n\n"
            "La fecha y hora del equipo no son correctas. Corregilas y conectate a "
            "internet para seguir usando Big PDF."
        )

    age_days = (now - validated_at) / 86400
    if dias_gracia > 0 and age_days <= dias_gracia:
        cache["ultimoUso"] = int(max(now, last_seen))
        _write_cache(cache)
        remaining_days = dias_gracia - age_days
        if remaining_days >= 1:
            remaining = f"{int(remaining_days)} día(s)"
        else:
            remaining = f"{max(1, int(remaining_days * 24))} hora(s)"
        return True, f"Sin conexión. Uso sin conexión habilitado por {remaining} más."

    if dias_gracia <= 0:
        detail = "Esta instalación no tiene habilitado el uso sin conexión."
    else:
        detail = f"Se agotó el período de uso sin conexión ({dias_gracia} día(s))."
    return False, (
        f"{reason}\n\n{detail}\n"
        "Conectate a internet para seguir usando Big PDF."
    )


def has_recent_validation(max_age_hours: float = RECENT_VALIDATION_HOURS) -> bool:
    """True si la última validación ONLINE exitosa (caché firmado) fue hace menos
    de ``max_age_hours``. Permite abrir la app sin esperar la respuesta de la
    planilla: la verificación sigue corriendo en segundo plano y, si deniega el
    acceso, la app se cierra. Un caché alterado o un reloj atrasado no cuentan."""
    cache = _load_cache()
    if not cache or not cache.get("ok"):
        return False
    validated_at = _to_int(cache.get("timestamp"))
    last_seen = max(validated_at, _to_int(cache.get("ultimoUso")))
    now = time.time()
    if now + _CLOCK_SKEW_SECONDS < last_seen:
        return False
    return 0 <= now - validated_at <= max_age_hours * 3600


@dataclass(frozen=True)
class AccessResult:
    ok: bool
    message: str
    # "online" | "offline" (período sin conexión) | "denied" | "network" | "invalid"
    kind: str

    @property
    def can_retry(self) -> bool:
        return self.kind in {"network", "invalid"}


def _run_with_deadline(fn, deadline_seconds: float):
    """Ejecuta ``fn`` en un hilo y espera como máximo ``deadline_seconds`` en total.

    El timeout de urlopen es por operación de socket (no cubre DNS ni el
    redirect de Apps Script), así que una red lenta podía colgar el arranque
    20-40 s. Si se vence, el hilo daemon se abandona.
    """
    result: dict[str, Any] = {}

    def _target() -> None:
        try:
            result["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 — se relanza en el hilo llamador
            result["error"] = exc

    thread = threading.Thread(target=_target, name="bigpdf-access-check", daemon=True)
    thread.start()
    thread.join(deadline_seconds)
    if thread.is_alive():
        raise TimeoutError("Tiempo de espera agotado verificando el acceso.")
    if "error" in result:
        raise result["error"]
    return result.get("value")


def check_access() -> AccessResult:
    """Verifica el acceso contra el Apps Script y, si no hay red, el período
    sin conexión. Bloquea hasta ACCESS_DEADLINE_SECONDS: llamarla fuera del hilo
    de Qt (main.py la corre en un hilo mientras muestra "Verificando acceso…")."""
    payload = {
        "accion": "check",
        "appId": APP_ID,
        "version": APP_VERSION,
        "equipo": get_machine_name(),
        "usuarioWindows": get_windows_user(),
        "observaciones": _os_info(),
    }

    try:
        data = _run_with_deadline(
            lambda: post_access_json(payload, timeout=ACCESS_TIMEOUT_SECONDS),
            ACCESS_DEADLINE_SECONDS,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("access check: sin conexión (%s)", exc)
        ok, message = _check_grace_period("No hay conexión con el servicio de acceso.")
        return AccessResult(ok, message, "offline" if ok else "network")
    except Exception as exc:
        # Hay red pero la respuesta no es la esperada (página de error de Google,
        # cuota agotada, script no publicado). No es culpa del usuario.
        logger.warning("access check: respuesta inválida (%s)", exc)
        ok, message = _check_grace_period(
            "El servicio de acceso respondió con un error."
        )
        return AccessResult(ok, message, "offline" if ok else "invalid")

    if data.get("ok"):
        _save_cache(data, _to_int(data.get("diasGracia")))
        return AccessResult(True, str(data.get("mensaje") or "Acceso autorizado."), "online")

    # Acceso revocado desde la planilla: se borra el caché para que tampoco
    # pueda seguir usándose sin conexión.
    _clear_cache()
    return AccessResult(
        False,
        str(data.get("mensaje") or "Acceso denegado. Contactá al administrador."),
        "denied",
    )


def check_startup_access() -> tuple[bool, str]:
    """Compatibilidad: (ok, mensaje)."""
    result = check_access()
    return result.ok, result.message
