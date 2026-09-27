"""Página Diagnóstico: estado de cada componente de Groove.

Las rutas no ejecutan comandos ni hablan con Spotify: todo pasa por
HealthChecker (health.py), que es de solo lectura.
"""
from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, url_for

from health import ERROR, WARNING

bp = Blueprint("diagnostics", __name__, url_prefix="/diagnostics")


def checker():
    return current_app.extensions["health"]


@bp.get("/")
def index():
    # Se muestra el último informe (manual o de un pre-flight); la primera
    # visita tras arrancar Groove lanza una comprobación.
    report = checker().last or checker().run(trigger="Al abrir Diagnóstico")
    return render_template("diagnostics.html", report=report,
                           errors=report.count(ERROR), warnings=report.count(WARNING))


@bp.post("/check")
def check():
    report = checker().run(trigger="Manual")
    errors, warnings = report.count(ERROR), report.count(WARNING)
    if errors:
        flash(f"Comprobación terminada: {errors} con problemas.", "error")
    elif warnings:
        flash(f"Comprobación terminada: {warnings} con avisos.")
    else:
        flash("Comprobación terminada: todo en orden.")
    return redirect(url_for("diagnostics.index"))


@bp.get("/report.json")
def report_json():
    """Último informe en JSON (sin secretos). No lanza una comprobación nueva."""
    report = checker().last
    if report is None:
        return jsonify(checked_at=None, checks=[])
    return jsonify(report.as_dict())
