CREATE TABLE IF NOT EXISTS alarms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    time TEXT NOT NULL,              -- "HH:MM"
    days TEXT NOT NULL DEFAULT '',   -- "0,2,4" (0 = lunes); vacío = una vez
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_triggered TEXT,             -- "YYYY-MM-DD HH:MM" del último disparo programado
    source TEXT NOT NULL DEFAULT 'local',  -- 'local' (WAV) o 'spotify'
    spotify_uri TEXT,                -- "spotify:<track|album|playlist>:<id>"
    volume_start INTEGER NOT NULL DEFAULT 20,  -- % al empezar (solo Spotify por ahora)
    volume_end INTEGER NOT NULL DEFAULT 60,    -- % al terminar el fade-in
    fade_minutes INTEGER NOT NULL DEFAULT 5,   -- duración del fade-in (0 = sin fade)
    max_duration_minutes INTEGER NOT NULL DEFAULT 30,  -- auto-stop (0 = sin límite)
    local_track TEXT                 -- pista de instance/music/ (solo el nombre); NULL = aleatoria
);

-- Tokens de Spotify (una sola fila). Viven en instance/, fuera del repositorio.
CREATE TABLE IF NOT EXISTS spotify_auth (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    access_token TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    expires_at REAL NOT NULL,        -- epoch en segundos
    scope TEXT NOT NULL DEFAULT ''
);

-- Ajustes sueltos clave/valor (p. ej. el dispositivo de Spotify elegido).
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
