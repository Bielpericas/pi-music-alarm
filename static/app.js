// Pide confirmación antes de enviar formularios con data-confirm (p. ej. borrar).
document.addEventListener("submit", function (event) {
  var message = event.target.getAttribute("data-confirm");
  if (message && !window.confirm(message)) {
    event.preventDefault();
  }
});

// Formulario de alarma: el campo de Spotify solo se ve si la fuente es Spotify.
// Sin JavaScript el campo se ve siempre, y el servidor valida igual.
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

// Página principal: si empieza a sonar una alarma (o cambia un snooze), se
// recarga para mostrar/ocultar STOP y +10 MIN. Consulta ligera cada 10 s.
(function () {
  var marker = document.querySelector("[data-playback-key]");
  if (!marker || !window.fetch) return;
  var key = marker.getAttribute("data-playback-key");
  var url = marker.getAttribute("data-playback-url");

  setInterval(function () {
    if (document.hidden) return;
    fetch(url, { cache: "no-store" })
      .then(function (resp) { return resp.ok ? resp.json() : null; })
      .then(function (state) {
        if (state && state.key !== key) window.location.reload();
      })
      .catch(function () {});
  }, 10000);
})();

// Formulario de alarma: muestra el valor de los deslizadores de volumen.
(function () {
  var outputs = document.querySelectorAll("[data-volume-output]");
  for (var i = 0; i < outputs.length; i++) {
    (function (output) {
      var input = document.getElementById(output.getAttribute("for"));
      if (!input) return;
      input.addEventListener("input", function () { output.textContent = input.value; });
    })(outputs[i]);
  }
})();
