"""Página Bluetooth: estado, dispositivos y ventana de emparejamiento.

Las rutas no ejecutan comandos: todo pasa por BluetoothManager
(bluetooth_manager.py). Las acciones son POST a URLs concretas (no hay una
"acción" genérica que elija el navegador) y la MAC se valida antes de nada.
El estado del audio (bluealsa-aplay, pausado por una alarma...) sale del
mismo check que usa Diagnóstico (HealthChecker.check_bluetooth).
"""
from flask import (
    Blueprint, abort, current_app, flash, jsonify, redirect, render_template, url_for,
)

from bluetooth_manager import BluetoothError, InvalidMac, normalize_mac

bp = Blueprint("bluetooth", __name__, url_prefix="/bluetooth")


def manager():
    return current_app.extensions["bluetooth_manager"]


def _mac_or_404(mac):
    try:
        return normalize_mac(mac)
    except InvalidMac:
        abort(404)


def _audio_state():
    """Resultado del check de Diagnóstico para el audio Bluetooth, o None."""
    health = current_app.extensions.get("health")
    if health is None:
        return None
    try:
        return health.check_bluetooth()
    except Exception:  # la página nunca falla por esto
        current_app.logger.exception("Bluetooth: no se pudo consultar el audio")
        return None


@bp.get("/")
def index():
    bt = manager()
    adapter, devices, error = None, [], None
    if bt.available:
        try:
            adapter = bt.adapter()
            if adapter.available:
                devices = bt.devices()
        except BluetoothError as exc:
            current_app.logger.warning("Bluetooth (página): %s %s", exc, exc.detail)
            error = str(exc)
    return render_template(
        "bluetooth.html", available=bt.available, adapter=adapter, devices=devices,
        session=bt.session_view(), recent=bt.recent_view(), audio=_audio_state(), error=error,
        playback_paused=bool(getattr(current_app.extensions.get("bluetooth"), "paused", False)),
        # Temporizador de sueño: solo los dispositivos conectados ahora.
        sleep_targets=[{"source": "bluetooth", "mac": d.mac, "label": f"Bluetooth · {d.name}"}
                       for d in devices if d.connected],
    )


@bp.get("/status")
def status():
    """Estado de la ventana de emparejamiento (en memoria, sin comandos)."""
    resp = jsonify(session=manager().session_view())
    resp.cache_control.no_store = True
    return resp


@bp.post("/pairing/start")
def pairing_start():
    try:
        _session, created = manager().start_pairing()
    except BluetoothError as exc:
        flash(str(exc), "error")
    else:
        if created:
            flash("Groove está visible durante 2 minutos. Busca «Groove» desde tu móvil, "
                  "tablet u ordenador.")
        else:
            flash("Ya hay un emparejamiento en curso.")
    return redirect(url_for("bluetooth.index"))


@bp.post("/pairing/cancel")
def pairing_cancel():
    session = manager().cancel_pairing()
    if session is None:
        flash("No había ningún emparejamiento en curso.")
    elif session.private_again:
        flash("Emparejamiento cancelado. Groove vuelve a estar en modo privado.")
    else:
        flash("Emparejamiento cancelado, pero no se pudo confirmar el modo privado. "
              "Mira el log de Groove.", "error")
    return redirect(url_for("bluetooth.index"))


@bp.post("/private")
def make_private():
    """Vuelve a privado (discoverable off, pairable off) sin tocar dispositivos."""
    bt = manager()
    if bt.session is not None and bt.session.active:
        bt.cancel_pairing()
        ok = bt.session.private_again
    else:
        ok = bt.ensure_private()
    if ok:
        flash("Groove está en modo privado.")
    else:
        flash("No se pudo volver a modo privado. Mira el log de Groove.", "error")
    return redirect(url_for("bluetooth.index"))


def _device_action(mac, method, success):
    mac = _mac_or_404(mac)
    try:
        device = getattr(manager(), method)(mac)
    except BluetoothError as exc:
        flash(str(exc), "error")
    else:
        flash(success.format(name=device.name))
    return redirect(url_for("bluetooth.index"))


@bp.post("/devices/<mac>/connect")
def connect(mac):
    return _device_action(mac, "connect", "«{name}» conectado.")


@bp.post("/devices/<mac>/disconnect")
def disconnect(mac):
    return _device_action(mac, "disconnect", "«{name}» desconectado. Sigue emparejado: puedes "
                                             "volver a conectarlo cuando quieras.")


@bp.post("/devices/<mac>/trust")
def trust(mac):
    return _device_action(mac, "trust", "«{name}» es ahora de confianza: podrá reconectarse "
                                        "solo.")


@bp.post("/devices/<mac>/forget")
def forget(mac):
    return _device_action(mac, "forget", "Dispositivo olvidado.")
