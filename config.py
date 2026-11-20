# -*- coding: utf-8 -*-
import configparser
import os
import sys
from urllib.parse import unquote, urlparse


def _resolver_base_dir() -> str:
    # Si corre como ejecutable congelado (PyInstaller / cx_Freeze / Nuitka), usa la carpeta del .exe
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


class Settings:
    def __init__(self):
        self.BASE_DIR = _resolver_base_dir()
        # Busca config.ini junto al ejecutable/script; si no está, prueba en el directorio actual
        candidatos = [
            os.path.join(self.BASE_DIR, "config.ini"),
            os.path.join(os.getcwd(), "config.ini"),
        ]
        self.CONFIG_PATH = next((p for p in candidatos if os.path.exists(p)), candidatos[0])

        self.parser = configparser.ConfigParser()
        if os.path.exists(self.CONFIG_PATH):
            try:
                self.parser.read(self.CONFIG_PATH, encoding="utf-8")
            except UnicodeDecodeError:
                self.parser.read(self.CONFIG_PATH, encoding="latin-1")

        self.DEBUG = self._get_bool("DEFAULT", "DEBUG", False)
        self.TIMEZONE = self._get("DEFAULT", "TIMEZONE", "America/Guayaquil")

        self.SERVER_HOST = self._get("SERVER", "HOST", "0.0.0.0")
        self.SERVER_PORT = self._get_int("SERVER", "PORT", 8081)

        # Lectura desde entorno o config.ini externo (sin credenciales quemadas)
        self.DATABASE_URL = os.getenv("DATABASE_URL") or self._get("DATABASE", "URL", "")
        self.DB_HOST = os.getenv("DB_HOST") or self._get("DATABASE", "HOST", "localhost")
        self.DB_PORT = int(os.getenv("DB_PORT") or self._get_int("DATABASE", "PORT", 5432))
        self.DB_USER = os.getenv("DB_USER") or self._get("DATABASE", "USER", "postgres")
        self.DB_PASSWORD = os.getenv("DB_PASSWORD") or self._get("DATABASE", "PASSWORD", "")
        self.DB_NAME = os.getenv("DB_NAME") or self._get("DATABASE", "NAME", "bananeras_db")
        self.DB_SSL_MODE = os.getenv("DB_SSL_MODE") or self._get("DATABASE", "SSL_MODE", "disable")

        # Si viene DATABASE_URL (por ejemplo en Railway), completa los campos DB_* si no fueron forzados explícitamente
        if self.DATABASE_URL:
            parsed = urlparse(self.DATABASE_URL)
            if parsed.hostname and not os.getenv("DB_HOST"):
                self.DB_HOST = parsed.hostname
            if parsed.port and not os.getenv("DB_PORT"):
                self.DB_PORT = parsed.port
            if parsed.username and not os.getenv("DB_USER"):
                self.DB_USER = unquote(parsed.username)
            if parsed.password is not None and not os.getenv("DB_PASSWORD"):
                self.DB_PASSWORD = unquote(parsed.password)
            if parsed.path and len(parsed.path) > 1 and not os.getenv("DB_NAME"):
                self.DB_NAME = parsed.path.lstrip("/")

        # Réplica híbrida hacia Railway (BD remota / SSL / teletrabajo)
        env_railway_enabled = (
            os.getenv("RAILWAY_ENABLED")
            or os.getenv("HYBRID_CLOUD_ENABLED")
            or ""
        ).strip()
        if env_railway_enabled:
            self.RAILWAY_ENABLED = env_railway_enabled.lower() in ("1", "true", "yes", "si", "on")
        else:
            self.RAILWAY_ENABLED = self._get_bool("CLOUD_RAILWAY", "ENABLED", False)

        self.RAILWAY_DATABASE_URL = (
            os.getenv("RAILWAY_DATABASE_URL")
            or os.getenv("CLOUD_DATABASE_URL")
            or self._get("CLOUD_RAILWAY", "DATABASE_URL", "")
        )
        self.RAILWAY_API_URL = (
            os.getenv("RAILWAY_API_URL")
            or os.getenv("CLOUD_API_URL")
            or self._get("CLOUD_RAILWAY", "API_URL", "")
        )
        self.RAILWAY_SSL_MODE = (
            os.getenv("RAILWAY_SSL_MODE")
            or os.getenv("CLOUD_SSL_MODE")
            or self._get("CLOUD_RAILWAY", "SSL_MODE", "require")
        )
        raw_interval = (
            os.getenv("RAILWAY_SYNC_INTERVAL_SEC")
            or os.getenv("CLOUD_SYNC_INTERVAL_SEC")
            or str(self._get_int("CLOUD_RAILWAY", "SYNC_INTERVAL_SEC", 15))
        )
        try:
            self.RAILWAY_SYNC_INTERVAL_SEC = max(3, int(raw_interval))
        except ValueError:
            self.RAILWAY_SYNC_INTERVAL_SEC = 15

        raw_batch = (
            os.getenv("RAILWAY_BATCH_SIZE")
            or str(self._get_int("CLOUD_RAILWAY", "BATCH_SIZE", 2000))
        )
        try:
            self.RAILWAY_BATCH_SIZE = max(1, int(raw_batch))
        except ValueError:
            self.RAILWAY_BATCH_SIZE = 2000

        raw_heartbeat = (
            os.getenv("PUSH_HEARTBEAT_SEGUNDOS")
            or str(self._get_int("CLOUD_RAILWAY", "PUSH_HEARTBEAT_SEGUNDOS", 300))
        )
        try:
            self.PUSH_HEARTBEAT_SEGUNDOS = max(10, int(raw_heartbeat))
        except ValueError:
            self.PUSH_HEARTBEAT_SEGUNDOS = 300

        self.BRIDGE_SECRET_KEY = os.getenv("BRIDGE_SECRET_KEY") or self._get("SECURITY", "BRIDGE_SECRET_KEY", "")
        self.ANTHROPIC_API_KEY = (
            os.getenv("ANTHROPIC_API_KEY")
            or self._get("SECURITY", "ANTHROPIC_API_KEY", "")
            or self._get("DEFAULT", "ANTHROPIC_API_KEY", "")
        )

        codigos_str = self._get("EMPACADORAS", "CODIGOS", "EMP01,EMP02,EMP03,EMP04,EMP05")
        self.CODIGOS_EMPACADORAS = [c.strip() for c in codigos_str.split(",") if c.strip()]

        self.COMEDOR_INICIO = self._get("COMEDOR", "HORA_INICIO", "11:00:00")
        self.COMEDOR_FIN = self._get("COMEDOR", "HORA_FIN", "14:00:00")

    def _get(self, section, key, default):
        try:
            return self.parser.get(section, key)
        except Exception:
            return default

    def _get_int(self, section, key, default):
        try:
            return self.parser.getint(section, key)
        except Exception:
            return default

    def _get_bool(self, section, key, default):
        try:
            return self.parser.getboolean(section, key)
        except Exception:
            return default


settings = Settings()