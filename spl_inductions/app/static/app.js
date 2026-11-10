// SPL Inductions: small progressive enhancements. Every action also works server-side.
(function () {
  'use strict';

  function post(url, data) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': window.CSRF || '' },
      body: JSON.stringify(data),
      credentials: 'same-origin'
    }).then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); });
  }

  // Tick boxes: checklist items (assessor), demonstrations (assessor), acknowledgements (inductee).
  var busy = 0;
  document.addEventListener('change', function (e) {
    var box = e.target;
    if (!box.matches || !box.matches('input[data-tick]')) return;
    busy++;
    box.disabled = true;
    post(box.dataset.tick, { item: box.dataset.item, value: box.checked })
      .catch(function () { box.checked = !box.checked; alert('That tick did not save. Check the connection and try again.'); })
      .then(function () { box.disabled = false; busy--; refreshParticipants(); pollParticipant(); });
  });

  // Assessor session page: live participant list.
  var plist = document.getElementById('participants');
  function refreshParticipants() {
    if (!plist || busy) return;
    fetch(plist.dataset.url, { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.text() : Promise.reject(); })
      .then(function (html) { if (!busy) plist.innerHTML = html; })
      .catch(function () {});
  }
  if (plist) { refreshParticipants(); setInterval(refreshParticipants, 3000); }

  // Inductee page: follow the assessor's ticks and unlock signing.
  var ps = document.getElementById('pstate');
  function pollParticipant() {
    if (!ps) return;
    fetch(ps.dataset.url, { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (st) {
        if (st.status !== ps.dataset.status) { location.reload(); return; }
        var done = {};
        st.done.forEach(function (id) { done[id] = true; });
        document.querySelectorAll('li.item[data-item]').forEach(function (li) {
          li.classList.toggle('done', !!done[li.dataset.item]);
        });
        var p = document.getElementById('progress');
        if (p) p.textContent = st.progress;
        var btn = document.getElementById('sign-btn');
        var hint = document.getElementById('sign-hint');
        if (btn) btn.disabled = !st.complete;
        if (hint) hint.hidden = st.complete;
      })
      .catch(function () {});
  }
  if (ps && ps.dataset.status !== 'complete' && ps.dataset.status !== 'cancelled') {
    setInterval(pollParticipant, 3000);
  }

  // Optional drawn signature.
  var canvas = document.getElementById('sig-canvas');
  if (canvas) {
    var ctx = canvas.getContext('2d'), drawing = false, drawn = false;
    function fit() {
      var r = canvas.getBoundingClientRect(), d = window.devicePixelRatio || 1;
      canvas.width = r.width * d; canvas.height = r.height * d;
      ctx.scale(d, d); ctx.lineWidth = 2.2; ctx.lineCap = 'round'; ctx.strokeStyle = '#111';
      drawn = false;
    }
    function pos(e) { var r = canvas.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; }
    canvas.addEventListener('pointerdown', function (e) { drawing = true; canvas.setPointerCapture(e.pointerId); var p = pos(e); ctx.beginPath(); ctx.moveTo(p[0], p[1]); });
    canvas.addEventListener('pointermove', function (e) { if (!drawing) return; var p = pos(e); ctx.lineTo(p[0], p[1]); ctx.stroke(); drawn = true; });
    canvas.addEventListener('pointerup', function () { drawing = false; });
    document.getElementById('sig-clear').addEventListener('click', fit);
    document.getElementById('sign-form').addEventListener('submit', function (e) {
      if (!drawn) { e.preventDefault(); alert('Draw your signature in the box first.'); return; }
      document.getElementById('sig-data').value = canvas.toDataURL('image/png');
    });
    fit();
  }

  // Countdown to when joining closes.
  document.querySelectorAll('[data-countdown]').forEach(function (el) {
    var end = new Date(el.dataset.countdown).getTime();
    function tick() {
      var s = Math.max(0, Math.round((end - Date.now()) / 1000));
      el.textContent = s ? 'in ' + Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0') : 'now';
      if (!s) { clearInterval(t); setTimeout(function () { location.reload(); }, 1500); }
    }
    var t = setInterval(tick, 1000); tick();
  });

  // Register: quick filter by name or email.
  var search = document.getElementById('reg-search');
  if (search) {
    search.addEventListener('input', function () {
      var q = search.value.trim().toLowerCase();
      document.querySelectorAll('#register tr[data-search]').forEach(function (tr) {
        tr.hidden = q && tr.dataset.search.indexOf(q) < 0;
      });
    });
  }
})();
