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

// Formulario de alarma: contenido de Spotify. Dos formas igual de válidas que
// acaban en el mismo campo spotify_uri (el servidor lo valida al guardar):
//   1. pegar un enlace (siempre visible, funciona sin JS y sin buscador);
//   2. buscar en Spotify (comodidad; si la API no lo permite, se avisa y el
//      enlace manual sigue funcionando).
// La metadata (nombre/subtítulo) solo acompaña al URI del que viene: si el
// enlace cambia a otro contenido, se descarta. Todo se pinta con textContent.
(function () {
  var picker = document.querySelector("[data-spotify-picker]");
  if (!picker) return;
  var MIN_CHARS = 2;
  var DEBOUNCE_MS = 400;
  var MAX_CACHE = 20;
  var GROUPS = [["tracks", "Canciones"], ["albums", "Álbumes"], ["playlists", "Playlists"]];
  var KINDS = { track: "Canción", album: "Álbum", playlist: "Playlist" };
  // Mismos formatos que parse_spotify_uri (spotify_client.py).
  var URI_RE = /^spotify:(track|album|playlist):([A-Za-z0-9]{22})$/;
  var URL_RE = /^https?:\/\/open\.spotify\.com\/(?:intl-[a-z]{2}(?:-[a-z]{2})?\/)?(track|album|playlist)\/([A-Za-z0-9]{22})\/?(?:[?#].*)?$/i;

  var connected = picker.getAttribute("data-connected") === "true";
  var searchUrl = picker.getAttribute("data-search-url");
  var lookupUrl = picker.getAttribute("data-lookup-url");
  var spotifyPage = picker.getAttribute("data-spotify-url");
  var uriInput = document.getElementById("spotify_uri");
  var metaName = picker.querySelector('input[name="spotify_name"]');
  var metaSub = picker.querySelector('input[name="spotify_subtitle"]');
  var metaUri = picker.querySelector('input[name="spotify_meta_uri"]');
  var card = picker.querySelector("[data-selection]");
  var cardName = picker.querySelector("[data-selection-name]");
  var cardMeta = picker.querySelector("[data-selection-meta]");
  var cardLink = picker.querySelector("[data-selection-link]");
  var section = picker.querySelector("[data-search-section]");
  var input = document.getElementById("spotify_search");
  var status = picker.querySelector("[data-search-status]");
  var results = picker.querySelector("[data-search-results]");
  var attribution = picker.querySelector("[data-search-attribution]");
  var template = picker.querySelector("[data-result-template]");
  if (!uriInput || !card || !metaUri) return;

  // "spotify:<tipo>:<id>" a partir de un enlace o URI, o null si no vale.
  function canonical(text) {
    var value = (text || "").trim();
    var match = URI_RE.exec(value) || URL_RE.exec(value);
    return match ? "spotify:" + match[1].toLowerCase() + ":" + match[2] : null;
  }

  function webUrl(uri) {
    var parts = uri.split(":");
    return "https://open.spotify.com/" + parts[1] + "/" + parts[2];
  }

  function setMeta(item) {
    metaName.value = item ? item.name : "";
    metaSub.value = item ? item.subtitle || "" : "";
    metaUri.value = item ? item.uri : "";
  }

  // Tarjeta de la selección actual: con nombre si hay metadata; si no, el URI
  // y el tipo que se deducen del propio enlace.
  function showCard(item) {
    var kind = KINDS[item.type] || "Contenido";
    cardName.textContent = item.name || item.uri;
    cardName.classList.toggle("is-uri", !item.name);
    cardMeta.textContent = item.name && item.subtitle ? kind + " · " + item.subtitle
                                                      : kind + " de Spotify";
    cardLink.href = webUrl(item.uri);
    card.hidden = false;
  }

  function showUriOnly(uri) {
    showCard({ uri: uri, type: uri.split(":")[1] });
  }

  // Metadata de un URI (alarma antigua o enlace pegado). Si falla, se queda
  // el URI: la alarma se guarda igual.
  function lookup(uri) {
    if (!connected || !window.fetch) return;
    fetch(lookupUrl + "?uri=" + encodeURIComponent(uri), {
      credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" }
    })
      .then(function (resp) { return resp.ok ? resp.json() : null; })
      .then(function (body) {
        if (!body || !body.ok || !body.item) return;
        if (canonical(uriInput.value) !== uri) return;  // el enlace ya cambió
        var item = { uri: uri, type: body.item.type, name: body.item.name,
                     subtitle: body.item.subtitle };
        setMeta(item);
        showCard(item);
      })
      .catch(function () {});
  }

  // 1. Enlace manual: el último enlace escrito manda. La metadata de antes solo
  // se conserva si sigue siendo el mismo contenido.
  function onManualEdit() {
    var uri = canonical(uriInput.value);
    if (uri && uri === canonical(metaUri.value)) return;
    setMeta(null);
    if (uri) showUriOnly(uri);
    else card.hidden = true;
  }
  uriInput.addEventListener("input", onManualEdit);
  uriInput.addEventListener("change", function () {
    var uri = canonical(uriInput.value);
    if (uri && !metaUri.value) lookup(uri);
  });

  var initial = canonical(uriInput.value);
  if (initial && !metaUri.value) lookup(initial);

  // 2. Buscador (solo con Spotify vinculado y fetch disponible).
  if (!connected || !section || !input || !template || !window.fetch) return;
  section.hidden = false;

  var timer = null;
  var controller = null;
  var seq = 0;            // solo cuenta la respuesta de la última búsqueda
  var blockedUntil = 0;   // 429: no se vuelve a llamar hasta entonces
  var disabled = false;   // error permanente (403, sin vincular...): no se insiste
  var cache = {};
  var cacheKeys = [];

  function setStatus(text, isError, reconnect) {
    status.textContent = text || "";
    status.classList.toggle("is-error", !!isError);
    if (reconnect && spotifyPage) {
      var link = document.createElement("a");
      link.href = spotifyPage;
      link.textContent = "Ir a Spotify";
      status.appendChild(document.createTextNode(" "));
      status.appendChild(link);
    }
  }

  function clearResults() {
    while (results.firstChild) results.removeChild(results.firstChild);
    if (attribution) attribution.hidden = true;
  }

  function select(item) {
    // El campo manual muestra el enlace elegido: los dos métodos comparten valor.
    uriInput.value = item.external_url || webUrl(item.uri);
    setMeta(item);
    showCard(item);
    clearResults();
    setStatus("");
    input.value = "";
    card.scrollIntoView({ block: "nearest" });
  }

  function row(item) {
    var li = template.content.firstElementChild.cloneNode(true);
    li.querySelector("[data-name]").textContent = item.name;
    li.querySelector("[data-sub]").textContent = item.subtitle || KINDS[item.type] || "";
    var art = li.querySelector("[data-art]");
    if (item.image_url && item.image_url.indexOf("https://") === 0) {
      var img = document.createElement("img");
      img.alt = "";
      img.width = 48;
      img.height = 48;
      img.loading = "lazy";
      img.decoding = "async";
      img.referrerPolicy = "no-referrer";
      img.addEventListener("error", function () {
        art.classList.remove("has-image");
        img.remove();
      });
      img.src = item.image_url;
      art.appendChild(img);
      art.classList.add("has-image");
    }
    li.querySelector("[data-pick]").addEventListener("click", function () { select(item); });
    var open = li.querySelector("[data-open]");
    open.href = webUrl(item.uri);
    open.setAttribute("aria-label", "Abrir «" + item.name + "» en Spotify");
    return li;
  }

  function render(data) {
    clearResults();
    var total = 0;
    GROUPS.forEach(function (group) {
      var items = ((data && data[group[0]]) || []).filter(function (item) {
        return item && canonical(item.uri) === item.uri && item.name;
      });
      if (!items.length) return;
      total += items.length;
      var block = document.createElement("section");
      var title = document.createElement("h3");
      title.className = "search-group-title";
      title.textContent = group[1];
      var list = document.createElement("ul");
      list.className = "search-list";
      items.forEach(function (item) { list.appendChild(row(item)); });
      block.appendChild(title);
      block.appendChild(list);
      results.appendChild(block);
    });
    setStatus(total ? "" : "Sin resultados. También puedes pegar un enlace de Spotify arriba.");
    if (attribution) attribution.hidden = !total;
  }

  function remember(query, data) {
    if (!cache[query]) cacheKeys.push(query);
    cache[query] = data;
    if (cacheKeys.length > MAX_CACHE) delete cache[cacheKeys.shift()];
  }

  function currentQuery() {
    return input.value.replace(/\s+/g, " ").trim();
  }

  function cancel() {
    clearTimeout(timer);
    seq++;  // cualquier respuesta en camino se ignora
    if (controller) controller.abort();
    controller = null;
  }

  // Error que no se arregla reintentando: se deja de buscar en esta página.
  function disable(message, reconnect) {
    disabled = true;
    cancel();
    clearResults();
    input.disabled = true;
    setStatus(message, true, reconnect);
  }

  function run() {
    var query = currentQuery();
    if (disabled || query.length < MIN_CHARS) return;
    if (cache[query]) { render(cache[query]); return; }
    var wait = Math.ceil((blockedUntil - Date.now()) / 1000);
    if (wait > 0) {
      clearResults();
      setStatus("Spotify pide esperar " + wait + " s antes de volver a buscar. " +
                "Puedes pegar un enlace de Spotify arriba.", true);
      timer = setTimeout(run, wait * 1000);  // un único intento cuando termine la espera
      return;
    }
    cancel();
    var mine = seq;
    controller = window.AbortController ? new AbortController() : null;
    setStatus("Buscando…");
    fetch(searchUrl + "?q=" + encodeURIComponent(query), {
      credentials: "same-origin", cache: "no-store",
      headers: { Accept: "application/json" },
      signal: controller ? controller.signal : undefined
    })
      .then(function (resp) {
        return resp.json().catch(function () { return {}; })
          .then(function (body) { return { status: resp.status, body: body || {} }; });
      })
      .then(function (res) {
        if (mine !== seq) return;
        controller = null;
        var body = res.body;
        if (res.status === 200 && body.ok) {
          remember(query, body.results);
          render(body.results);
          return;
        }
        var message = body.message ||
          "La búsqueda de Spotify no está disponible. Puedes pegar un enlace de Spotify arriba.";
        if (body.search_available === false) {
          disable(message, body.action === "reconnect");
          return;
        }
        if (res.status === 429) {
          blockedUntil = Date.now() + Math.max(1, body.retry_after || 5) * 1000;
        }
        clearResults();
        setStatus(message, true);
      })
      .catch(function (err) {
        if (mine !== seq || (err && err.name === "AbortError")) return;
        clearResults();
        setStatus("No se pudo conectar con Groove para buscar. Puedes pegar un enlace de " +
                  "Spotify arriba.", true);
      });
  }

  input.addEventListener("input", function () {
    if (disabled) return;
    cancel();
    var query = currentQuery();
    if (query.length < MIN_CHARS) {
      clearResults();
      setStatus(query ? "Escribe al menos " + MIN_CHARS + " caracteres." : "");
      return;
    }
    timer = setTimeout(run, DEBOUNCE_MS);
  });
  // Enter busca ya, pero nunca envía el formulario de la alarma.
  input.addEventListener("keydown", function (event) {
    if (event.key !== "Enter") return;
    event.preventDefault();
    clearTimeout(timer);
    run();
  });
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
