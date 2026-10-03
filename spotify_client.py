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
- https://developer.spotify.com/documentation/web-api/reference/search
- https://developer.spotify.com/documentation/design (atribución de metadata)
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
from startup_budget import budget_lock, current_budget
from spotify_transport import deadline_transport

logger = logging.getLogger("alarms")

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"
SCOPES = ("user-read-playback-state", "user-modify-playback-state")

TIMEOUT = 10             # segundos por petición
REFRESH_MARGIN = 60      # renovar el token si caduca en menos de esto
MAX_RETRY_WAIT = 5       # esperar un 429 solo si Retry-After es corto

# Buscador del formulario (interfaz): peticiones cortas, sin esperas ni reintentos.
UI_TIMEOUT = 6           # segundos por petición del buscador / metadata
SEARCH_LIMIT = 5         # resultados por tipo (canciones, álbumes, playlists)
SEARCH_MIN_CHARS = 2
SEARCH_MAX_CHARS = 100
SEARCH_TYPES = ("track", "album", "playlist")
MAX_META_NAME = 120      # longitud máxima de los textos de metadata guardados
MAX_META_SUBTITLE = 160


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
        # 429 del buscador: solo frena al buscador, nunca a las alarmas.
        self._ui_blocked_until = 0.0

    # Estado

    @property
    def is_configured(self):
        return bool(self.client_id and self.client_secret and self.redirect_uri)

    def is_connected(self):
        return self.tokens.load() is not None

    def disconnect(self):
        self.tokens.clear()

    def missing_scopes(self):
        """Scopes que pide Groove y que no tiene la autorización guardada.

        Si no está vacío, hay que volver a vincular Spotify (cambió SCOPES). Una
        respuesta sin `scope` (tokens antiguos) no cuenta como falta.
        """
        tokens = self.tokens.load()
        granted = (tokens or {}).get("scope") or ""
        if not granted.strip():
            return set()
        return set(SCOPES) - set(granted.split())

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

    # Buscador y metadata (formulario de alarmas). Nunca los usa el reproductor.

    def search(self, query, limit=SEARCH_LIMIT):
        """Busca canciones, álbumes y playlists (GET /search).

        Devuelve {"tracks": [...], "albums": [...], "playlists": [...]} con
        elementos normalizados (ver `normalize_item`). Con menos de
        SEARCH_MIN_CHARS caracteres no llama a Spotify. Un 429 no se reintenta:
        el buscador queda en pausa `retry_after` segundos (solo el buscador).
        """
        query = " ".join((query or "").split())[:SEARCH_MAX_CHARS]
        if len(query) < SEARCH_MIN_CHARS:
            return empty_search()
        params = {"q": query, "type": ",".join(SEARCH_TYPES), "limit": int(limit),
                  "market": "from_token"}
        data = self._ui_api("/search", params)
        if not isinstance(data, dict) or not any(key in data for key in _SEARCH_KEYS.values()):
            # JSON roto o sin ninguna sección: no es "sin resultados", es un error.
            raise SpotifyError("Spotify devolvió una respuesta inesperada.")
        return normalize_search(data)

    def get_item(self, uri):
        """Metadata normalizada de una canción, álbum o playlist (o None).

        Para mostrar alarmas antiguas que solo guardan el URI. Lanza
        SpotifyError si Spotify no responde; ValueError si el URI no es válido.
        """
        uri = parse_spotify_uri(uri)
        kind, item_id = uri.split(":")[1:]
        if kind == "playlist":
            data = self._ui_api(f"/playlists/{item_id}",
                                {"fields": "uri,name,owner(display_name,id),images"})
        elif kind == "album":
            data = self._ui_api(f"/albums/{item_id}", {"market": "from_token"})
        else:
            data = self._ui_api(f"/tracks/{item_id}", {"market": "from_token"})
        item = normalize_item(data, kind)
        # Spotify puede devolver otro id (relinking); lo que vale es el URI de la alarma.
        return dict(item, uri=uri, external_url=spotify_web_url(uri)) if item else None

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
        with budget_lock(self._lock):
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

    def _ui_api(self, path, params):
        """GET para la interfaz: timeout corto y un 429 no espera ni reintenta."""
        remaining = self._ui_blocked_until - self._clock()
        if remaining > 0:
            raise SpotifyRateLimitError(
                f"Spotify pidió esperar; inténtalo en {int(remaining) + 1} s.",
                int(remaining) + 1,
            )
        try:
            return self._api("GET", path, params=params, timeout=UI_TIMEOUT, interactive=True)
        except SpotifyRateLimitError as exc:
            self._ui_blocked_until = max(self._ui_blocked_until,
                                         self._clock() + exc.retry_after)
            raise

    def _api(self, method, path, params=None, body=None, timeout=None, interactive=False):
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
                # Las peticiones de la interfaz no frenan a las alarmas (block=False).
                error = self._rate_limit_error(resp_headers, block=not interactive)
                if interactive:
                    raise error
                if not retried_rate and error.retry_after <= MAX_RETRY_WAIT:
                    retried_rate = True
                    logger.warning("Spotify 429: reintento en %ss", error.retry_after)
                    budget = current_budget()
                    if budget is None:
                        self._sleep(error.retry_after)
                    else:
                        budget.wait(error.retry_after, None if self._sleep is time.sleep else self._sleep)
                    continue
                raise error
            raise _api_error(status, content)

    def _send(self, method, url, headers, body, timeout=None):
        budget = current_budget()
        request_timeout = timeout or TIMEOUT
        if budget is not None:
            request_timeout = budget.timeout(request_timeout)
        remaining = self._blocked_until - self._clock()
        if remaining > 0:
            raise SpotifyRateLimitError(
                f"Spotify pidió esperar; inténtalo en {int(remaining) + 1} s.",
                int(remaining) + 1,
            )
        try:
            if budget is not None and self._transport is urllib_transport:
                return deadline_transport(method, url, headers, body, request_timeout, budget)
            response = self._transport(method, url, headers, body, request_timeout)
            if budget is not None:
                budget.remaining()
            return response
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise SpotifyConnectionError(f"No se pudo conectar con Spotify: {reason}") from exc

    def _rate_limit_error(self, headers, block=True):
        try:
            retry_after = max(1, int(headers.get("retry-after", "1")))
        except ValueError:
            retry_after = 1
        if block and retry_after > MAX_RETRY_WAIT:
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


def spotify_web_url(uri):
    """Enlace a open.spotify.com construido solo a partir de un URI válido."""
    kind, item_id = parse_spotify_uri(uri).split(":")[1:]
    return f"https://open.spotify.com/{kind}/{item_id}"


# --- Normalización (buscador y metadata) -----------------------------------
#
# Formato interno, igual para los tres tipos:
#   {"uri": "spotify:<tipo>:<id>", "type": "track|album|playlist",
#    "name": "...", "subtitle": "...", "external_url": "https://open.spotify.com/...",
#    "image_url": "https://..." o None}
# subtitle: artistas (canción y álbum) o propietario (playlist). Nada más del
# JSON de Spotify. external_url se construye desde el URI, no desde la respuesta.

_SEARCH_KEYS = {"track": "tracks", "album": "albums", "playlist": "playlists"}


def empty_search():
    return {key: [] for key in _SEARCH_KEYS.values()}


def clean_text(value, max_len):
    """Texto de una línea, sin caracteres de control y con longitud máxima."""
    if not isinstance(value, str):
        return ""
    text = " ".join("".join(ch if ch.isprintable() else " " for ch in value).split())
    return text[:max_len].rstrip()


def normalize_search(data):
    """Respuesta de GET /search -> {"tracks": [...], "albums": [...], "playlists": [...]}.

    Tolera respuestas parciales o raras: tipos ausentes, `items` que no son
    lista, elementos null (Spotify los manda en playlists) o sin URI válido.
    """
    results = empty_search()
    if not isinstance(data, dict):
        return results
    for kind, key in _SEARCH_KEYS.items():
        section = data.get(key)
        items = section.get("items") if isinstance(section, dict) else None
        if not isinstance(items, list):
            continue
        seen = set()
        for raw in items:
            item = normalize_item(raw, kind)
            if item and item["uri"] not in seen:
                seen.add(item["uri"])
                results[key].append(item)
    return results


def normalize_item(raw, kind):
    """Un track/album/playlist de Spotify -> formato interno, o None si no vale."""
    if not isinstance(raw, dict) or not isinstance(raw.get("uri"), str):
        return None
    try:
        uri = parse_spotify_uri(raw["uri"])
    except ValueError:
        return None
    name = clean_text(raw.get("name"), MAX_META_NAME)
    if uri.split(":")[1] != kind or not name:
        return None
    if kind == "playlist":
        owner = raw.get("owner") if isinstance(raw.get("owner"), dict) else {}
        subtitle = owner.get("display_name") or owner.get("id")
        images = raw.get("images")
    else:
        subtitle = _artists(raw.get("artists"))
        album = raw.get("album") if kind == "track" else raw
        images = album.get("images") if isinstance(album, dict) else None
    return {
        "uri": uri,
        "type": kind,
        "name": name,
        "subtitle": clean_text(subtitle, MAX_META_SUBTITLE),
        "external_url": spotify_web_url(uri),
        "image_url": _thumbnail(images),
    }


def _artists(artists):
    """"Daft Punk, Romanthony" a partir de la lista de artistas de Spotify."""
    if not isinstance(artists, list):
        return ""
    names = [clean_text(a.get("name"), MAX_META_SUBTITLE) for a in artists if isinstance(a, dict)]
    return ", ".join(name for name in names if name)


def _thumbnail(images, target=64):
    """URL https de la imagen más pequeña que llegue a `target` px (o None)."""
    if not isinstance(images, list):
        return None
    candidates = []
    for image in images:
        url = image.get("url") if isinstance(image, dict) else None
        if not isinstance(url, str) or not url.startswith("https://") or len(url) > 500:
            continue
        width = image.get("width")
        width = width if isinstance(width, int) and not isinstance(width, bool) else 0
        candidates.append((width, url))
    if not candidates:
        return None
    big_enough = [c for c in candidates if c[0] >= target]
    return min(big_enough)[1] if big_enough else max(candidates)[1]


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
