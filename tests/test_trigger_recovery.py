"""Recuperación de STOP y disparos reservados: reloj falso y SQLite real."""
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import db
import scheduler
from app import create_app
from audio_player import AudioPlayer
from playback import AlarmPlaybackManager, STOP_RETRY_LIMIT, STOP_RETRY_SECONDS


NOW = datetime(2026, 10, 2, 7, 0)
ALARM = dict(id=99, name="Anterior", time="06:30", source="local", spotify_uri=None,
             max_duration_minutes=0)


class StopRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.player = Mock(spec=AudioPlayer)
        self.player.play.return_value = True
        self.player.stop.return_value = False
        self.spotify = Mock()
        self.spotify.play.return_value = True
        self.spotify.stop.return_value = False
        self.bluetooth = Mock()
        self.now = NOW
        self.jobs = []
        self.manager = AlarmPlaybackManager(
            self.player, self.spotify, bluetooth=self.bluetooth,
            schedule_once=self.schedule, clock=lambda: self.now)

    def schedule(self, when, callback):
        cancel = Mock()
        self.jobs.append((when, callback, cancel))
        return cancel

    def fire(self, index=-1):
        when, callback, _ = self.jobs[index]
        self.now = when
        return callback()

    def test_auto_stop_recovers_for_local_and_spotify(self):
        for source in ("local", "spotify"):
            with self.subTest(source=source):
                self.jobs.clear()
                self.manager.start(dict(ALARM, source=source, spotify_uri="spotify:track:x",
                                        max_duration_minutes=15))
                self.fire()
                self.assertEqual(self.manager.active.status, "stop_pending")
                self.assertEqual(self.jobs[-1][0], self.now + timedelta(seconds=STOP_RETRY_SECONDS))
                self.bluetooth.resume.assert_not_called()
                target = self.spotify if source == "spotify" else self.player
                target.stop.return_value = True
                self.assertTrue(self.fire().silenced)
                self.assertIsNone(self.manager.active)
                self.bluetooth.resume.assert_called_once()
                self.bluetooth.reset_mock()
                self.player.stop.return_value = self.spotify.stop.return_value = False

    def test_retry_exhaustion_remains_visible_and_manual_stop_still_works(self):
        self.manager.start(ALARM)
        self.manager.stop()
        for _ in range(STOP_RETRY_LIMIT):
            self.fire()
        self.assertEqual(len(self.jobs), STOP_RETRY_LIMIT)
        self.assertEqual(self.player.stop.call_count, STOP_RETRY_LIMIT + 1)
        self.assertEqual(self.manager.active.status, "stop_pending")
        self.bluetooth.resume.assert_not_called()
        self.player.stop.return_value = True
        self.assertTrue(self.manager.stop().silenced)

    def test_manual_repeated_failures_do_not_multiply_retry_jobs(self):
        self.manager.start(ALARM)
        self.manager.stop()
        self.manager.stop()
        self.assertEqual(len(self.jobs), 1)
        old = self.jobs[0][1]
        self.fire(0)
        self.assertEqual(len(self.jobs), 2)
        calls = self.player.stop.call_count
        old()  # callback ya ejecutado, mientras hay otro reintento pendiente
        self.assertEqual(self.player.stop.call_count, calls)

    def test_old_retry_cannot_stop_new_playback(self):
        self.manager.start(ALARM)
        self.manager.stop()
        old, cancel = self.jobs[0][1:]
        self.player.stop.return_value = True
        self.manager.stop()
        cancel.assert_called_once()
        self.manager.start(dict(ALARM, id=100))
        calls = self.player.stop.call_count
        old()
        self.assertEqual(self.player.stop.call_count, calls)
        self.assertEqual(self.manager.active.id, 100)

    def test_scheduler_failure_keeps_manual_control(self):
        self.manager.schedule_once = Mock(side_effect=RuntimeError("scheduler stopped"))
        self.manager.start(ALARM)
        self.assertFalse(self.manager.stop().silenced)
        self.assertEqual(self.manager.active.status, "stop_pending")
        self.player.stop.return_value = True
        self.assertTrue(self.manager.stop().silenced)

    def test_snooze_rejected_by_pending_stop_is_retained_until_recovery(self):
        self.player.stop.return_value = True
        self.manager.start(ALARM)
        self.manager.snooze(minutes=10)
        snooze_callback = self.jobs[-1][1]
        self.manager.start(dict(ALARM, id=100))
        self.player.stop.return_value = False
        self.manager.stop()
        self.now = NOW + timedelta(minutes=10)
        snooze_callback()
        pending = self.manager.pending_snoozes[0]
        self.assertEqual(pending.snoozes, 1)
        self.assertEqual(pending.retry_until, self.now + timedelta(minutes=2))
        retry_callback = self.jobs[-1][1]
        self.player.stop.return_value = True
        self.manager.stop()
        self.now += timedelta(seconds=30)
        retry_callback()
        self.assertEqual(self.manager.pending_snoozes, ())
        self.assertEqual(self.manager.active.id, 99)
        self.assertEqual(self.manager.active.snoozes, 1)

    def test_snooze_retry_window_does_not_extend_on_each_rejection(self):
        self.player.stop.return_value = True
        self.manager.start(ALARM)
        self.manager.snooze(minutes=10)
        callback = self.jobs[-1][1]
        self.manager.start(dict(ALARM, id=100))
        self.player.stop.return_value = False
        self.manager.stop()
        self.now = NOW + timedelta(minutes=10)
        for _ in range(5):
            callback()
            if self.manager.pending_snoozes:
                self.assertEqual(self.manager.pending_snoozes[0].retry_until,
                                 NOW + timedelta(minutes=12))
                callback = self.jobs[-1][1]
            self.now += timedelta(seconds=30)
        self.assertEqual(self.manager.pending_snoozes, ())
        self.assertEqual(self.manager.active.id, 100)
        self.assertEqual(self.player.play.call_count, 2)


class TriggerRecoveryTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.addCleanup(scheduler.close_logging)
        self.path = str(Path(temp.name) / "alarms.db")
        self.app = create_app(dict(TESTING=True, SECRET_KEY="test", DATABASE=self.path,
                                   ALARM_LOG=str(Path(temp.name) / "log"), SCHEDULER_ENABLED=False))
        self.client = self.app.test_client()
        self.add_alarm()
        self.player = Mock(spec=AudioPlayer)
        self.player.play.return_value = True
        self.player.stop.return_value = False
        self.jobs = []
        self.now = NOW
        self.manager = AlarmPlaybackManager(self.player, clock=lambda: self.now,
                                            schedule_once=self.schedule)
        self.manager.start(ALARM)
        self.manager.stop()

    def schedule(self, when, callback):
        self.jobs.append((when, callback))
        return Mock()

    def add_alarm(self, name="Una vez", days=""):
        conn = db.connect(self.path)
        with conn:
            cur = conn.execute("INSERT INTO alarms (name, time, days, max_duration_minutes) "
                               "VALUES (?, '07:00', ?, 0)", (name, days))
        conn.close()
        return cur.lastrowid

    def check(self, now=NOW):
        self.now = now
        return scheduler.check_alarms(self.path, now, manager=self.manager)

    def row(self, table="alarms", alarm_id=1):
        conn = db.connect(self.path)
        key = "id" if table == "alarms" else "alarm_id"
        row = conn.execute(f"SELECT * FROM {table} WHERE {key} = ?", (alarm_id,)).fetchone()
        conn.close()
        return row

    def test_rejected_one_time_alarm_is_reserved_not_consumed_then_starts_once(self):
        self.assertEqual(self.check(), [])
        self.assertEqual(self.row()["enabled"], 1)
        self.assertEqual(self.row()["last_triggered"], "2026-10-02 07:00")
        self.assertEqual(self.row("alarm_triggers")["status"], "stop_pending")
        self.assertEqual(self.check(), [])  # mismo minuto: no duplica el intento
        self.assertIn("Inicio pendiente", self.client.get("/").get_data(as_text=True))
        self.player.stop.return_value = True
        self.jobs[-1][1]()  # recuperación autónoma a los 30 s
        self.assertIsNone(self.manager.active)
        self.assertEqual(self.check(NOW + timedelta(minutes=1)), ["Una vez"])
        self.assertEqual(self.row()["enabled"], 0)
        self.assertEqual(self.row("alarm_triggers")["status"], "local")
        self.assertEqual(self.check(NOW + timedelta(minutes=1)), [])
        self.assertEqual(self.player.play.call_count, 2)  # anterior y nueva

    def test_expired_one_time_alarm_is_explicitly_closed_and_does_not_fire_tomorrow(self):
        self.check()
        self.check(NOW + timedelta(minutes=3))
        self.assertEqual(self.row("alarm_triggers")["status"], "expired")
        self.assertEqual(self.row()["enabled"], 0)
        self.assertIn("No iniciada", self.client.get("/").get_data(as_text=True))
        self.player.stop.return_value = True
        self.manager.stop()
        self.assertEqual(self.check(NOW + timedelta(days=1)), [])

    def test_retry_at_deadline_allowed_but_seconds_later_is_expired(self):
        self.check()
        self.player.stop.return_value = True
        self.manager.stop()
        self.assertEqual(self.check(NOW + timedelta(minutes=2)), ["Una vez"])
        self.add_alarm(name="Tardía")
        conn = db.connect(self.path)
        db.reserve_trigger(conn, 2, "2026-10-02 07:00", "2026-10-02 07:02:00")
        db.finish_trigger(conn, 2, "2026-10-02 07:00", "stop_pending")
        conn.close()
        self.assertEqual(self.check(NOW + timedelta(minutes=2, seconds=1)), [])
        self.assertEqual(self.row("alarm_triggers", 2)["status"], "expired")

    def test_recurring_alarm_retains_next_scheduled_day_after_expiry(self):
        self.add_alarm(name="Diaria", days="0,1,2,3,4,5,6")
        self.check()
        self.check(NOW + timedelta(minutes=3))
        self.assertEqual(self.row(alarm_id=2)["enabled"], 1)
        self.player.stop.return_value = True
        self.manager.stop()
        self.assertEqual(self.check(NOW + timedelta(days=1)), ["Diaria"])

    def test_disable_edit_and_delete_cancel_pending_retry(self):
        for action in ("disable", "edit", "delete"):
            with self.subTest(action=action):
                conn = db.connect(self.path)
                with conn:
                    conn.execute("UPDATE alarms SET enabled=1, last_triggered=NULL, time='07:00' WHERE id=1")
                conn.close()
                self.check()
                with self.app.app_context():
                    if action == "disable":
                        db.toggle_alarm(1)
                    elif action == "edit":
                        db.update_alarm(1, "Nueva hora", "08:00", [])
                    else:
                        db.delete_alarm(1)
                self.assertEqual(self.check(NOW + timedelta(minutes=1)), [])
                self.assertEqual(self.player.play.call_count, 1)
                if action != "delete":
                    self.assertEqual(self.row("alarm_triggers")["status"], "cancelled")

    def test_pending_retry_survives_restart_but_not_outside_window(self):
        self.check()
        restored = AlarmPlaybackManager(self.player, schedule_once=self.schedule)
        self.assertEqual(scheduler.check_alarms(self.path, NOW + timedelta(minutes=1),
                                               manager=restored), ["Una vez"])
        self.assertEqual(self.row()["enabled"], 0)

    def test_inflight_rejection_cannot_revive_cancelled_retry(self):
        fake = Mock()
        def reject_after_disable(alarm):
            with self.app.app_context():
                db.toggle_alarm(alarm["id"])
            return "stop_pending"
        fake.start.side_effect = reject_after_disable
        self.assertEqual(scheduler.check_alarms(self.path, NOW, manager=fake), [])
        self.assertEqual(self.row()["enabled"], 0)
        self.assertEqual(self.row("alarm_triggers")["status"], "cancelled")
        self.assertEqual(self.check(NOW + timedelta(minutes=1)), [])

    def test_simultaneous_alarms_keep_deterministic_last_wins_order(self):
        self.add_alarm(name="Segunda")
        self.check()
        self.player.stop.return_value = True
        self.manager.stop()
        self.assertEqual(self.check(NOW + timedelta(minutes=1)), ["Una vez", "Segunda"])
        self.assertEqual(self.manager.active.id, 2)
        self.assertEqual(self.row("alarm_triggers", 1)["status"], "local")
        self.assertEqual(self.row("alarm_triggers", 2)["status"], "local")

    def test_concurrent_schedulers_do_not_duplicate_reserved_or_retried_start(self):
        entered, release = threading.Event(), threading.Event()
        fake = Mock()
        def play(alarm):
            entered.set()
            if not release.wait(3):
                raise AssertionError("No terminó la comprobación concurrente")
            return "stop_pending"
        fake.start.side_effect = play
        results, errors = [], []
        def run():
            try:
                results.append(scheduler.check_alarms(self.path, NOW, manager=fake))
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.assertTrue(entered.wait(3))
        try:
            self.assertEqual(scheduler.check_alarms(self.path, NOW, manager=fake), [])
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(errors)
        self.assertFalse(thread.is_alive())
        self.assertEqual(fake.start.call_count, 1)
        fake.start.return_value = "local"
        fake.start.side_effect = None
        # Dos conexiones compiten por el reintento: solo una lo reserva.
        conn1, conn2 = db.connect(self.path), db.connect(self.path)
        try:
            self.assertTrue(db.reserve_retry(conn1, 1, "2026-10-02 07:00", "2026-10-02 07:01:00"))
            self.assertFalse(db.reserve_retry(conn2, 1, "2026-10-02 07:00", "2026-10-02 07:01:00"))
        finally:
            conn1.close()
            conn2.close()

    def test_restart_does_not_repeat_attempt_with_uncertain_result(self):
        conn = db.connect(self.path)
        db.reserve_trigger(conn, 1, "2026-10-02 07:00", "2026-10-02 07:02:00")
        rows = db.interrupt_unfinished_triggers(conn)
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.row("alarm_triggers")["status"], "interrupted")
        self.assertEqual(self.row()["enabled"], 0)
        self.assertEqual(self.check(NOW + timedelta(minutes=1)), [])

    def test_failed_or_cancelled_start_is_recorded_and_not_reported_as_playing(self):
        for outcome in ("failed", "cancelled"):
            with self.subTest(outcome=outcome):
                alarm_id = self.add_alarm(name=outcome)
                fake = Mock()
                fake.start.return_value = outcome
                self.assertEqual(scheduler.check_alarms(self.path, NOW, manager=fake), [])
                self.assertEqual(self.row("alarm_triggers", alarm_id)["status"], outcome)
                self.assertEqual(self.row(alarm_id=alarm_id)["enabled"], 0)

    def test_polling_key_changes_when_pending_attempt_expires(self):
        before = self.client.get("/playback/state").json["key"]
        self.check()
        pending = self.client.get("/playback/state").json["key"]
        self.assertNotEqual(before, pending)
        self.check(NOW + timedelta(minutes=3))
        self.assertNotEqual(pending, self.client.get("/playback/state").json["key"])


if __name__ == "__main__":
    unittest.main()
