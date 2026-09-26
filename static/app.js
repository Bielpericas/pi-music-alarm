// Groove: JavaScript común (vanilla, sin dependencias).
// Todo es mejora progresiva: sin JS, los formularios siguen funcionando.

// Pide confirmación antes de enviar formularios con data-confirm (p. ej. borrar).
document.addEventListener("submit", function (event) {
  var message = event.target.getAttribute("data-confirm");
  if (message && !window.confirm(message)) {
    event.preventDefault();
  }
});

// Formulario de alarma: los campos de Spotify solo se ven si la fuente es Spotify.
// Sin JavaScript se ven siempre, y el servidor valida igual.
(function () {
  var field = document.querySelector("[data-spotify-field]");
  if (!field) return;
  var radios = document.querySelectorAll('input[name="source"]');

  function update() {
    var checked = document.querySelector('input[name="source"]:checked');
    field.hidden = !checked || checked.value !== "spotify";
  }

  for (var i = 0; i < radios.length; i++) {
    radios[i].addEventListener("change", update);
  }
  update();
})();

// Formulario de alarma: porcentaje de los deslizadores y resumen del fade-in.
(function () {
  var outputs = document.querySelectorAll("[data-volume-output]");
  for (var i = 0; i < outputs.length; i++) {
    (function (output) {
      var input = document.getElementById(output.getAttribute("for"));
      if (!input) return;
      input.addEventListener("input", function () { output.textContent = input.value; });
    })(outputs[i]);
  }

  var summary = document.querySelector("[data-fade-summary]");
  var start = document.getElementById("volume_start");
  var end = document.getElementById("volume_end");
  var fade = document.getElementById("fade_minutes");
  if (!summary || !start || !end || !fade) return;

  function describe() {
    var minutes = parseInt(fade.value, 10);
    var from = parseInt(start.value, 10);
    var to = parseInt(end.value, 10);
    if (from > to) {
      summary.textContent = "El volumen al empezar no puede ser mayor que el final.";
    } else if (minutes === 0 || from === to) {
      summary.textContent = "Suena directamente al " + to + " %.";
    } else {
      summary.textContent = "Empieza al " + from + " % y sube poco a poco hasta el " + to +
        " % en " + minutes + (minutes === 1 ? " minuto." : " minutos.");
    }
    summary.hidden = false;
  }

  [start, end, fade].forEach(function (el) {
    el.addEventListener("input", describe);
    el.addEventListener("change", describe);
  });
  describe();
})();

// Menús de acciones (<details>): uno abierto a la vez; se cierran con Escape
// o tocando fuera.
(function () {
  var menus = document.querySelectorAll("details.menu");
  if (!menus.length) return;

  function closeAll(except) {
    for (var i = 0; i < menus.length; i++) {
      if (menus[i] !== except) menus[i].open = false;
    }
  }

  for (var i = 0; i < menus.length; i++) {
    menus[i].addEventListener("toggle", function (event) {
      if (event.target.open) closeAll(event.target);
    });
  }
  document.addEventListener("click", function (event) {
    if (!event.target.closest("details.menu")) closeAll(null);
  });
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    var open = document.querySelector("details.menu[open]");
    if (open) {
      open.open = false;
      open.querySelector("summary").focus();
    }
  });
})();

// Estado de Groove cada 10 s (sin caché): si empieza a sonar una alarma o
// cambia un snooze, se muestra; si Groove no responde, se avisa. No hay modo
// offline: sin conexión con la Raspberry no se puede hacer nada.
(function () {
  var marker = document.querySelector("[data-playback-key]");
  if (!marker || !window.fetch) return;
  var key = marker.getAttribute("data-playback-key");
  var url = marker.getAttribute("data-playback-url");
  var home = marker.getAttribute("data-home-url");
  var banner = document.querySelector("[data-offline-banner]");
  var text = document.querySelector("[data-connection-text]");
  var onHome = window.location.pathname === home;

  function setOnline(online) {
    document.body.classList.toggle("is-offline", !online);
    if (banner) banner.hidden = online;
    if (text) text.textContent = online ? "En línea" : "Sin conexión";
  }

  function check() {
    if (document.hidden) return;
    fetch(url, { cache: "no-store", credentials: "same-origin" })
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (state) {
        setOnline(true);
        if (state.key === key) return;
        if (onHome) {
          window.location.reload();
        } else if (state.active) {
          window.location.href = home;  // alarma sonando: a la pantalla de STOP
        } else {
          key = state.key;
        }
      })
      .catch(function () { setOnline(false); });
  }

  setInterval(check, 10000);
  window.addEventListener("online", check);
  window.addEventListener("offline", function () { setOnline(false); });
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) check();
  });
})();

// PWA: service worker pequeño (solo cachea la "carcasa" estática).
// Los navegadores solo lo permiten en HTTPS o en 127.0.0.1/localhost.
if ("serviceWorker" in navigator && window.isSecureContext) {
  window.addEventListener("load", function () {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(function () {});
  });
}
