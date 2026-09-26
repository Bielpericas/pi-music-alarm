import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scheduler  # noqa: E402
from app import create_app  # noqa: E402

# 2026-09-28 es lunes (weekday 0).
MONDAY_0730 = datetime(2026, 9, 28, 7, 30, 5)


class AlarmAppTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test.db")
        self.log_path = os.path.join(self.tmpdir.name, "alarms.log")
        self.app = self.make_app()
        self.client = self.app.test_client()

    def make_app(self):
        return create_app(
            {"TESTING": True, "DATABASE": self.db_path, "ALARM_LOG": self.log_path}
        )

    def tearDown(self):
        scheduler.close_logging()
        self.tmpdir.cleanup()

    def rows(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute("SELECT * FROM alarms").fetchall()
        finally:
            conn.close()

    def create(self, **overrides):
        data = {"name": "Trabajo", "time": "07:30", "days": ["0", "1", "2", "3", "4"]}
        data.update(overrides)
        return self.client.post("/alarms/new", data=data)

    def test_index_empty(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("No hay alarmas", resp.get_data(as_text=True))

    def test_new_form(self):
        self.assertEqual(self.client.get("/alarms/new").status_code, 200)

    def test_create_alarm(self):
        resp = self.create()
        self.assertEqual(resp.status_code, 302)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["days"], "0,1,2,3,4")
        self.assertEqual(rows[0]["enabled"], 1)
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Trabajo", html)
        self.assertIn("07:30", html)
        self.assertIn("Lunes a viernes", html)

    def test_create_once(self):
        self.create(days=[])
        self.assertEqual(self.rows()[0]["days"], "")
        self.assertIn("Una vez", self.client.get("/").get_data(as_text=True))

    def test_invalid_input_rejected(self):
        for bad in ({"time": "25:00"}, {"time": ""}, {"name": "  "},
                    {"name": "x" * 51}, {"days": ["7"]}, {"days": ["abc"]}):
            with self.subTest(bad=bad):
                self.assertEqual(self.create(**bad).status_code, 400)
        self.assertEqual(self.rows(), [])

    def test_toggle(self):
        self.create()
        alarm_id = self.rows()[0]["id"]
        self.client.post(f"/alarms/{alarm_id}/toggle")
        self.assertEqual(self.rows()[0]["enabled"], 0)
        self.client.post(f"/alarms/{alarm_id}/toggle")
        self.assertEqual(self.rows()[0]["enabled"], 1)

    def test_delete(self):
        self.create()
        alarm_id = self.rows()[0]["id"]
        self.assertEqual(self.client.post(f"/alarms/{alarm_id}/delete").status_code, 302)
        self.assertEqual(self.rows(), [])

    def test_missing_alarm_404(self):
        self.assertEqual(self.client.post("/alarms/999/delete").status_code, 404)
        self.assertEqual(self.client.post("/alarms/999/toggle").status_code, 404)

    def test_delete_requires_post(self):
        self.assertEqual(self.client.get("/alarms/1/delete").status_code, 405)

    # --- Prueba manual ---

    def test_manual_test_button(self):
        self.create()
        alarm_id = self.rows()[0]["id"]
        with self.assertLogs("alarms", "INFO") as logs:
            resp = self.client.post(f"/alarms/{alarm_id}/test")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("ALARMA ACTIVADA: Trabajo (prueba manual)", logs.output[0])
        # La prueba no cuenta como disparo programado.
        self.assertIsNone(self.rows()[0]["last_triggered"])
        self.assertEqual(self.client.post("/alarms/999/test").status_code, 404)

    def test_index_shows_test_button(self):
        self.create()
        self.assertIn("Probar", self.client.get("/").get_data(as_text=True))

    # --- Scheduler ---

    def check(self, now):
        return scheduler.check_alarms(self.db_path, now)

    def test_fires_on_matching_day_and_time(self):
        self.create()  # 07:30, lunes a viernes
        with self.assertLogs("alarms", "INFO") as logs:
            self.assertEqual(self.check(MONDAY_0730), ["Trabajo"])
        self.assertIn("ALARMA ACTIVADA: Trabajo", logs.output[0])
        self.assertEqual(self.rows()[0]["last_triggered"], "2026-09-28 07:30")

    def test_writes_log_file(self):
        self.create()
        self.check(MONDAY_0730)
        with open(self.log_path, encoding="utf-8") as f:
            self.assertIn("ALARMA ACTIVADA: Trabajo", f.read())

    def test_respects_days_time_and_enabled(self):
        self.create()
        saturday = MONDAY_0730.replace(day=26)
        self.assertEqual(self.check(saturday), [])
        self.assertEqual(self.check(MONDAY_0730.replace(minute=31)), [])
        self.client.post(f"/alarms/{self.rows()[0]['id']}/toggle")
        self.assertEqual(self.check(MONDAY_0730), [])

    def test_fires_only_once_per_minute(self):
        self.create()
        self.assertEqual(self.check(MONDAY_0730), ["Trabajo"])
        self.assertEqual(self.check(MONDAY_0730.replace(second=45)), [])
        tuesday = MONDAY_0730.replace(day=29)
        self.assertEqual(self.check(tuesday), ["Trabajo"])

    def test_one_time_alarm_fires_once_and_disables(self):
        self.create(days=[])
        self.assertEqual(self.check(MONDAY_0730), ["Trabajo"])
        self.assertEqual(self.rows()[0]["enabled"], 0)
        self.assertEqual(self.check(MONDAY_0730.replace(day=29)), [])

    def test_alarms_survive_restart(self):
        self.create()
        scheduler.close_logging()
        self.app = self.make_app()  # "reinicio": nueva app sobre la misma BD
        self.assertEqual(self.check(MONDAY_0730), ["Trabajo"])

    def test_migrates_old_database(self):
        scheduler.close_logging()
        self.tmpdir.cleanup()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "old.db")
        self.log_path = os.path.join(self.tmpdir.name, "alarms.log")
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE alarms (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,"
            " time TEXT NOT NULL, days TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL"
            " DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute("INSERT INTO alarms (name, time, days) VALUES ('Vieja', '07:30', '0')")
        conn.commit()
        conn.close()
        self.make_app()
        self.assertIsNone(self.rows()[0]["last_triggered"])
        self.assertEqual(self.check(MONDAY_0730), ["Vieja"])

    def test_scheduler_not_started_in_tests(self):
        # Si arrancara, habría un hilo de APScheduler vivo.
        import threading
        names = [t.name for t in threading.enumerate()]
        self.assertFalse(any("APScheduler" in n for n in names))


if __name__ == "__main__":
    unittest.main()
