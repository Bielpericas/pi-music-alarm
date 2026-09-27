// Groove: JavaScript común (vanilla, sin dependencias).
// Todo es mejora progresiva: sin JS, los formularios siguen funcionando.

// Pide confirmación antes de enviar formularios con data-confirm (p. ej. borrar).
document.addEventListener("submit", function (event) {
  var message = event.target.getAttribute("data-confirm");
  if (message && !window.confirm(message)) {
    event.preventDefault();
  }
});

// Formulario de alarma: los campos de Spotify solo se ven si la fuente es Spotify,
// y la música local se llama «Música de respaldo» con Spotify.
// Sin JavaScript se ven siempre, y el servidor valida igual.
(function () {
  var field = document.querySelector("[data-spotify-field]");
  if (!field) return;
  var radios = document.querySelectorAll('input[name="source"]');
  var trackLabel = document.querySelector("[data-track-label]");

  function update() {
    var checked = document.querySelector('input[name="source"]:checked');
    var spotify = !!checked && checked.value === "spotify";
    field.hidden = !spotify;
    if (trackLabel) {
      trackLabel.textContent = trackLabel.getAttribute(spotify ? "data-label-spotify" : "data-label-local");
    }
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
// o tocando fuera. Se abren hacia abajo salvo que no quepan: entonces hacia
// arriba. Mientras hay uno abierto, el botón flotante se oculta (body.menu-open).
(function () {
  var menus = document.querySelectorAll("details.menu");
  if (!menus.length) return;
  var GAP = 8;  // margen mínimo entre el menú y lo que lo taparía
  var header = document.querySelector(".app-header");
  var bottomNav = document.querySelector(".nav-bottom");

  function closeAll(except) {
    for (var i = 0; i < menus.length; i++) {
      if (menus[i] !== except) menus[i].open = false;
    }
  }

  function openMenu() {
    return document.querySelector("details.menu[open]");
  }

  // Zona realmente visible: entre la cabecera sticky y la nav inferior (o el
  // borde del viewport visual, que se encoge con el teclado del móvil).
  function visibleArea() {
    var top = 0;
    var bottom = window.visualViewport
      ? window.visualViewport.offsetTop + window.visualViewport.height
      : window.innerHeight;
    if (header) top = Math.max(top, header.getBoundingClientRect().bottom);
    if (bottomNav && getComputedStyle(bottomNav).display !== "none") {
      bottom = Math.min(bottom, bottomNav.getBoundingClientRect().top);
    }
    return { top: top, bottom: bottom };
  }

  // Decide la dirección con el panel ya visible. Todo ocurre en la misma
  // tarea que abre el menú o que hace scroll, así que no se pinta un salto.
  function place(menu) {
    var panel = menu.querySelector(".menu-panel");
    if (!panel) return;
    var trigger = menu.querySelector("summary").getBoundingClientRect();
    var needed = panel.offsetHeight + GAP;
    var area = visibleArea();
    var below = area.bottom - trigger.bottom;
    var above = trigger.top - area.top;
    // Abajo si cabe; si no, arriba cuando allí cabe o hay más sitio.
    var up = below < needed && (above >= needed || above > below);
    menu.classList.toggle("menu-up", up);
  }

  function sync() {
    var menu = openMenu();
    document.body.classList.toggle("menu-open", !!menu);
    if (menu) place(menu);
  }

  for (var i = 0; i < menus.length; i++) {
    menus[i].querySelector("summary").addEventListener("click", function (event) {
      // Se abre a mano para colocar el panel antes de que se pinte.
      var menu = event.currentTarget.parentNode;
      event.preventDefault();
      menu.open = !menu.open;
      if (menu.open) {
        closeAll(menu);
        sync();
      }
    });
    menus[i].addEventListener("toggle", function (event) {
      if (event.target.open) closeAll(event.target);
      else event.target.classList.remove("menu-up");
      sync();
    });
  }

  var pending = false;
  function onViewportChange() {
    if (pending || !openMenu()) return;
    pending = true;
    window.requestAnimationFrame(function () {
      pending = false;
      sync();
    });
  }
  window.addEventListener("scroll", onViewportChange, { passive: true });
  window.addEventListener("resize", onViewportChange);
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", onViewportChange);
    window.visualViewport.addEventListener("scroll", onViewportChange);
  }

  document.addEventListener("click", function (event) {
    if (!event.target.closest("details.menu")) closeAll(null);
  });
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    var open = openMenu();
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
