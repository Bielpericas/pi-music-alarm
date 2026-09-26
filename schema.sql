CREATE TABLE IF NOT EXISTS alarms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    time TEXT NOT NULL,              -- "HH:MM"
    days TEXT NOT NULL DEFAULT '',   -- "0,2,4" (0 = lunes); vacío = una vez
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_triggered TEXT              -- "YYYY-MM-DD HH:MM" del último disparo programado
);
