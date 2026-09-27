"""Integración con Spotify Web API (sin SDK, solo librería estándar).

Todo el tráfico HTTP con Spotify pasa por este módulo. El resto de la app usa
`SpotifyClient` y captura `SpotifyError` (y subclases).

Autenticación: Authorization Code Flow (el Client Secret vive solo en el
servidor, en variables de entorno). Los tokens se guardan en SQLite, dentro de
instance/ (fuera del repositorio), y el access token se renueva solo.

Referencias (revisadas en septiembre de 2026):
- https://developer.spotify.com/documentation/web-api/tutorials/code-flow
- https://developer.spotify.com/documentation/web-api/tutorials/refreshing-tokens
- https://developer.spotify.com/documentation/web-api/concepts/redirect_uri
- https://developer.spotify.com/documentation/web-api/concepts/rate-limits
"""
import base64
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import db

logger = logging.getLogger("alarms")

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"
SCOPES = ("user-read-playback-state", "user-modify-playback-state")

TIMEOUT = 10             # segundos por petición
REFRESH_MARGIN = 60      # renovar el token si caduca en menos de esto
MAX_RETRY_WAIT = 5       # esperar un 429 solo si Retry-After es corto


# --- Errores ---------------------------------------------------------------

class SpotifyError(Exception):
    """Error genérico de Spotify.

    `status` es el código HTTP si lo hay; `reason` y `api_message` son los
    campos `error.reason` y `error.message` tal y como los envía Spotify
    (p. ej. "UNKNOWN" / "Player command failed: Restriction violated").
    """

    def __init__(self, message, status=None, reason=None, api_message=None):
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.api_message = api_message


class SpotifyNotConfiguredError(SpotifyError):
    """Faltan SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET."""


class SpotifyAuthError(SpotifyError):
    """No hay cuenta vinculada o los tokens ya no valen (401, invalid_grant)."""

    def __init__(self, message, status=None, oauth_error=None):
        super().__init__(message, status)
        self.oauth_error = oauth_error  # p. ej. "invalid_grant", "invalid_client"


class SpotifyForbiddenError(SpotifyError):
    """403: sin Premium, usuario fuera de la allowlist o acción no permitida."""


class SpotifyNotFoundError(SpotifyError):
    """404: normalmente, no hay ningún dispositivo activo."""


class SpotifyRateLimitError(SpotifyError):
    """429: demasiadas peticiones. `retry_after` en segundos."""

    def __init__(self, message, retry_after):
        super().__init__(message, 429)
        self.retry_after = retry_after


class SpotifyConnectionError(SpotifyError):
    """No se pudo hablar con Spotify (sin red, DNS, timeout...)."""


# --- Transporte HTTP -------------------------------------------------------

def urllib_transport(method, url, headers, body, timeout):
    """Hace la petición y devuelve (status, headers en minúsculas, cuerpo bytes)."""
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, _lower(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, _lower(exc.headers), exc.read()


def _lower(headers):
    return {k.lower(): v for k, v in headers.items()} if headers else {}


# --- Almacén de tokens -----------------------------------------------------

class SqliteTokenStore:
    """Guarda los tokens en la tabla spotify_auth (una sola fila)."""

    def __init__(self, database):
        self.database = database

    def load(self):
        conn = db.connect(self.database)
        try:
            row = conn.execute("SELECT * FROM spotify_auth WHERE id = 1").fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def save(self, tokens):
        conn = db.connect(self.database)
        try:
            conn.execute(
                """INSERT INTO spotify_auth (id, access_token, refresh_token, expires_at, scope)
                   VALUES (1, :access_token, :refresh_token, :expires_at, :scope)
                   ON CONFLICT(id) DO UPDATE SET
                     access_token = excluded.access_token,
                     refresh_token = excluded.refresh_token,
                     expires_at = excluded.expires_at,
                     scope = excluded.scope""",
                tokens,
            )
            conn.commit()
        finally:
            conn.close()

    def clear(self):
        conn = db.connect(self.database)
        try:
            conn.execute("DELETE FROM spotify_auth")
            conn.commit()
        finally:
            conn.close()


# --- Cliente ---------------------------------------------------------------

class SpotifyClient:
    def __init__(self, client_id, client_secret, redirect_uri, token_store,
                 transport=urllib_transport, clock=time.time, sleep=time.sleep):
        self.client_id = client_id or ""
        self.client_secret = client_secret or ""
        self.redirect_uri = redirect_uri
        self.tokens = token_store
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._blocked_until = 0.0  # por un 429 con Retry-After largo

    # Estado

    @property
    def is_configured(self):
        return bool(self.client_id and self.client_secret and self.redirect_uri)

    def is_connected(self):
        return self.tokens.load() is not None

    def disconnect(self):
        self.tokens.clear()

    # OAuth

    def get_authorize_url(self, state):
        self._require_config()
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(SCOPES),
            "state": state,
        }
        return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    def exchange_code(self, code):
        """Cambia el `code` del callback por tokens y los guarda."""
        self._require_config()
        data = self._token_request(
            {"grant_type": "authorization_code", "code": code,
             "redirect_uri": self.redirect_uri}
        )
        self._store_token_response(data, previous_refresh=None)

    # API del reproductor

    def get_devices(self, timeout=None):
        """Dispositivos visibles. `timeout` (s) acorta la espera (Diagnóstico)."""
        data = self._api("GET", "/me/player/devices", timeout=timeout)
        return (data or {}).get("devices", [])

    def transfer_playback(self, device_id, play=False):
        self._api("PUT", "/me/player", body={"device_ids": [device_id], "play": play})

    def play(self, device_id=None, uri=None, offset=None):
        """Reanuda, o empieza `uri` (track, album o playlist) si se indica.

        `offset` (índice desde 0) empieza un álbum o playlist directamente en
        esa pista (offset.position); con una canción suelta se ignora.
        """
        body = None
        if uri:
            kind = parse_spotify_uri(uri).split(":")[1]
            # Un track va en "uris"; álbumes y playlists son un "context_uri".
            body = {"uris": [uri]} if kind == "track" else {"context_uri": uri}
            if offset is not None and kind != "track":
                body["offset"] = {"position": int(offset)}
        self._api("PUT", "/me/player/play", params=_device(device_id), body=body)

    def get_track_count(self, uri):
        """Número de pistas de un álbum o playlist (None si no se sabe).

        - Álbum: GET /albums/{id} -> total_tracks.
        - Playlist: GET /playlists/{id}?fields=items.total (desde febrero de
          2026 "tracks" se llama "items"; se acepta también el nombre antiguo).
          Spotify solo da el contenido de playlists propias o colaborativas.
        """
        kind, item_id = parse_spotify_uri(uri).split(":")[1:]
        if kind == "album":
            data = self._api("GET", f"/albums/{item_id}") or {}
            total = data.get("total_tracks")
            if total is None:
                total = (data.get("tracks") or {}).get("total")
        elif kind == "playlist":
            data = self._api("GET", f"/playlists/{item_id}", params={"fields": "items.total"}) or {}
            total = (data.get("items") or {}).get("total")
            if total is None:
                total = (data.get("tracks") or {}).get("total")
        else:
            return None
        return total if isinstance(total, int) and not isinstance(total, bool) else None

    def pause(self, device_id=None):
        self._api("PUT", "/me/player/pause", params=_device(device_id))

    def set_volume(self, volume_percent, device_id=None):
        """PUT /me/player/volume (0-100). Requiere Premium y un dispositivo que
        admita control de volumen (en librespot, volume-ctrl distinto de fixed)."""
        volume = int(volume_percent)
        if not 0 <= volume <= 100:
            raise ValueError(f"Volumen fuera de rango: {volume}")
        params = {"volume_percent": volume}
        params.update(_device(device_id) or {})
        self._api("PUT", "/me/player/volume", params=params)

    # Internos

    def _require_config(self):
        if not self.is_configured:
            raise SpotifyNotConfiguredError(
                "Spotify no está configurado: faltan SPOTIFY_CLIENT_ID / "
                "SPOTIFY_CLIENT_SECRET en .env."
            )

    def _access_token(self, force_refresh=False):
        with self._lock:
            tokens = self.tokens.load()
            if tokens is None:
                raise SpotifyAuthError("No hay ninguna cuenta de Spotify vinculada.")
            if force_refresh or tokens["expires_at"] - self._clock() < REFRESH_MARGIN:
                tokens = self._refresh(tokens)
            return tokens["access_token"]

    def _refresh(self, tokens):
        self._require_config()
        try:
            data = self._token_request(
                {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}
            )
        except SpotifyAuthError as exc:
            # Refresh token revocado o caducado: hay que volver a vincular.
            # (Con invalid_client el problema es la configuración, no los tokens.)
            if exc.oauth_error == "invalid_grant":
                self.tokens.clear()
            raise
        return self._store_token_response(data, previous_refresh=tokens["refresh_token"])

    def _store_token_response(self, data, previous_refresh):
        # Spotify puede no devolver un refresh_token nuevo: se sigue usando el anterior.
        tokens = {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token") or previous_refresh,
            "expires_at": self._clock() + int(data.get("expires_in", 3600)),
            "scope": data.get("scope", ""),
        }
        if not tokens["refresh_token"]:
            raise SpotifyAuthError("Spotify no devolvió refresh_token.")
        self.tokens.save(tokens)
        return tokens

    def _token_request(self, form):
        credentials = f"{self.client_id}:{self.client_secret}".encode()
        headers = {
            "Authorization": "Basic " + base64.b64encode(credentials).decode(),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        body = urllib.parse.urlencode(form).encode()
        status, resp_headers, content = self._send("POST", TOKEN_URL, headers, body)
        data = _json(content)
        if status == 200:
            return data
        error = data.get("error_description") or data.get("error") or f"HTTP {status}"
        if status == 429:
            raise self._rate_limit_error(resp_headers)
        if status in (400, 401):
            raise SpotifyAuthError(
                f"Spotify rechazó la autenticación: {error}", status, data.get("error")
            )
        raise SpotifyError(f"Error pidiendo token a Spotify: {error}", status)

    def _api(self, method, path, params=None, body=None, timeout=None):
        url = API_BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        payload = json.dumps(body).encode() if body is not None else b""

        token = self._access_token()
        retried_auth = retried_rate = False
        while True:
            headers = {"Authorization": f"Bearer {token}"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            status, resp_headers, content = self._send(
                method, url, headers, payload if method != "GET" else None, timeout
            )

            if status in (200, 201, 202, 204):
                return _json(content) if content else None
            if status == 401 and not retried_auth:
                # Token rechazado antes de caducar: renovar una vez y reintentar.
                retried_auth = True
                token = self._access_token(force_refresh=True)
                continue
            if status == 429:
                error = self._rate_limit_error(resp_headers)
                if not retried_rate and error.retry_after <= MAX_RETRY_WAIT:
                    retried_rate = True
                    logger.warning("Spotify 429: reintento en %ss", error.retry_after)
                    self._sleep(error.retry_after)
                    continue
                raise error
            raise _api_error(status, content)

    def _send(self, method, url, headers, body, timeout=None):
        remaining = self._blocked_until - self._clock()
        if remaining > 0:
            raise SpotifyRateLimitError(
                f"Spotify pidió esperar; inténtalo en {int(remaining) + 1} s.",
                int(remaining) + 1,
            )
        try:
            return self._transport(method, url, headers, body, timeout or TIMEOUT)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise SpotifyConnectionError(f"No se pudo conectar con Spotify: {reason}") from exc

    def _rate_limit_error(self, headers):
        try:
            retry_after = max(1, int(headers.get("retry-after", "1")))
        except ValueError:
            retry_after = 1
        if retry_after > MAX_RETRY_WAIT:
            self._blocked_until = self._clock() + retry_after
        return SpotifyRateLimitError(
            f"Demasiadas peticiones a Spotify; espera {retry_after} s.", retry_after
        )


SPOTIFY_KINDS = ("track", "album", "playlist")
_URI_RE = re.compile(r"^spotify:(track|album|playlist):([A-Za-z0-9]{22})$")
# https://open.spotify.com/[intl-xx/]<tipo>/<id>[?si=...]
_URL_RE = re.compile(
    r"^https?://open\.spotify\.com/(?:intl-[a-z]{2}(?:-[a-z]{2})?/)?"
    r"(track|album|playlist)/([A-Za-z0-9]{22})/?(?:[?#].*)?$",
    re.IGNORECASE,
)


def parse_spotify_uri(text):
    """Convierte una URL de open.spotify.com o una URI en "spotify:<tipo>:<id>".

    Solo acepta track, album y playlist. Lanza ValueError si no es válida.
    """
    text = (text or "").strip()
    match = _URI_RE.match(text) or _URL_RE.match(text)
    if not match:
        raise ValueError(
            "Pega una URL de open.spotify.com o una URI spotify: de una canción, "
            "álbum o playlist."
        )
    return f"spotify:{match.group(1).lower()}:{match.group(2)}"


def _device(device_id):
    return {"device_id": device_id} if device_id else None


def _json(content):
    if not content:
        return {}
    try:
        return json.loads(content)
    except ValueError:
        return {}


def _api_error(status, content):
    error = _json(content).get("error")
    message = error.get("message") if isinstance(error, dict) else None
    reason = error.get("reason") if isinstance(error, dict) else None
    detail = message or f"HTTP {status}"
    if reason:
        detail += f" ({reason})"
    if status == 401:
        exc = SpotifyAuthError(f"Spotify rechazó el token: {detail}", status)
    elif status == 403:
        exc = SpotifyForbiddenError(f"Spotify no permite la acción: {detail}", status)
    elif status == 404:
        exc = SpotifyNotFoundError(f"Spotify no encuentra el dispositivo: {detail}", status)
    else:
        exc = SpotifyError(f"Error de Spotify: {detail}", status)
    exc.reason, exc.api_message = reason, message
    return exc


def create_spotify_client(config):
    return SpotifyClient(
        client_id=config.get("SPOTIFY_CLIENT_ID"),
        client_secret=config.get("SPOTIFY_CLIENT_SECRET"),
        redirect_uri=config.get("SPOTIFY_REDIRECT_URI"),
        token_store=SqliteTokenStore(config["DATABASE"]),
    )
