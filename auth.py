"""Seguridad de CuidarMéx: contraseñas, tokens JWT y límite de intentos.

Solo usa la biblioteca estándar y PyJWT. No toca la base de datos.
"""
import hashlib
import hmac
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt

ITERACIONES = 200_000
ALGORITMO = "HS256"
HORAS_SESION = 8

# Si no se define JWT_SECRET (solo desarrollo) se genera uno por arranque:
# los tokens dejan de valer al reiniciar. En producción es OBLIGATORIO.
_SECRETO = os.environ.get("JWT_SECRET") or secrets.token_urlsafe(48)


def secreto_definido() -> bool:
    return bool(os.environ.get("JWT_SECRET"))


# ---------- CONTRASEÑAS (PBKDF2-SHA256 con sal aleatoria) ----------

def hash_password(password: str) -> str:
    sal = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(sal), ITERACIONES
    ).hex()
    return f"pbkdf2_sha256${ITERACIONES}${sal}${h}"


def verificar_password(password: str, almacenado: str) -> bool:
    try:
        _, iteraciones, sal, esperado = almacenado.split("$")
        h = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(sal), int(iteraciones)
        ).hex()
        return hmac.compare_digest(h, esperado)
    except Exception:
        return False


# Hash de relleno: si el correo no existe se verifica contra este, para que
# tardar lo mismo y no revelar qué correos están registrados.
HASH_RELLENO = hash_password(secrets.token_hex(8))


# ---------- TOKENS ----------

def crear_token(user_id: int, rol: str) -> str:
    ahora = datetime.now(timezone.utc)
    carga = {
        "sub": str(user_id),
        "rol": rol,
        "iat": ahora,
        "exp": ahora + timedelta(hours=HORAS_SESION),
    }
    return jwt.encode(carga, _SECRETO, algorithm=ALGORITMO)


def leer_token(token: str) -> Optional[dict]:
    try:
        datos = jwt.decode(token, _SECRETO, algorithms=[ALGORITMO])
        datos["sub"] = int(datos["sub"])
        return datos
    except Exception:
        return None


# ---------- LÍMITE DE INTENTOS FALLIDOS (en memoria) ----------

class LimitadorIntentos:
    """5 fallos en 10 minutos bloquean ese correo durante 10 minutos."""

    def __init__(self, maximo: int = 5, ventana: int = 600, bloqueo: int = 600):
        self.maximo, self.ventana, self.bloqueo = maximo, ventana, bloqueo
        self._fallos: dict = {}
        self._bloqueados: dict = {}
        self._candado = threading.Lock()

    def segundos_de_bloqueo(self, clave: str) -> int:
        with self._candado:
            hasta = self._bloqueados.get(clave)
            if not hasta:
                return 0
            restante = int(hasta - time.time())
            if restante <= 0:
                self._bloqueados.pop(clave, None)
                return 0
            return restante

    def registrar_fallo(self, clave: str) -> None:
        ahora = time.time()
        with self._candado:
            recientes = [t for t in self._fallos.get(clave, []) if ahora - t < self.ventana]
            recientes.append(ahora)
            self._fallos[clave] = recientes
            if len(recientes) >= self.maximo:
                self._bloqueados[clave] = ahora + self.bloqueo
                self._fallos.pop(clave, None)

    def limpiar(self, clave: str) -> None:
        with self._candado:
            self._fallos.pop(clave, None)
            self._bloqueados.pop(clave, None)
