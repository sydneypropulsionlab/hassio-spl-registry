// SPL Registry: small progressive enhancements. Every action also works server-side.
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

  // Drawn signature: the inductee's (sign-form) or a checklist's (run-form, checked on Submit only).
  var canvas = document.getElementById('sig-canvas');
  if (canvas) {
    var ctx = canvas.getContext('2d'), drawing = false, drawn = false;
    var sigForm = canvas.closest('form'), sigData = document.getElementById('sig-data');
    window.splSigSync = function () { if (drawn) sigData.value = canvas.toDataURL('image/png'); };
    canvas.addEventListener('pointerdown', function () { canvas.parentNode.classList.add('drawn'); });
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
    document.getElementById('sig-clear').addEventListener('click', function () {
      fit(); sigData.value = ''; canvas.parentNode.classList.remove('drawn');
    });
    var redo = document.getElementById('sig-redo');
    if (redo) redo.addEventListener('click', function () {
      redo.parentNode.hidden = true;
      canvas.parentNode.hidden = false;
      sigData.value = '';
      fit();
    });
    sigForm.addEventListener('submit', function (e) {
      window.splSigSync();
      var checking = sigForm.id !== 'run-form' || (e.submitter && e.submitter.value === 'submit');
      if (checking && !sigData.value) { e.preventDefault(); alert('Draw your signature in the box first.'); }
    });
    if (!canvas.parentNode.hidden) fit();
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

// Checklists: save the draft as soon as a photo is chosen (so it's stored and shown), and a small
// formatting toolbar for rich text boxes. Without JavaScript the plain form still works.
(function () {
  'use strict';
  var form = document.getElementById('run-form');
  if (!form) return;
  form.addEventListener('change', function (e) {
    var input = e.target;
    if (!input.matches || !input.matches('input[type=file][data-autosave]') || !input.files.length) return;
    form.querySelector('input[name=anchor]').value = input.dataset.autosave === '_run' ? '' : input.dataset.autosave;
    var label = input.closest('.rf-add, .photo-add');
    if (label) { label.classList.add('busy'); label.querySelector('span').textContent = 'Uploading…'; }
    var b = document.createElement('input');
    b.type = 'hidden'; b.name = 'action'; b.value = 'photo';
    form.appendChild(b);
    syncRich();
    if (window.splSigSync) window.splSigSync();
    form.submit();
  });

  // progress: how many items have an answer; readings turn green or red against their limits
  var items = form.querySelectorAll('.rf-item[id^="i-"]');
  function answered(li) {
    if (li.querySelector('input[type=checkbox]:checked, input[type=radio]:checked, .rf-thumb')) return true;
    var t = li.querySelector('input.rf-input, .rf-num input, textarea[name^="v_"]');
    if (t && t.value.trim()) return true;
    var ed = li.querySelector('.rich-editor [contenteditable]');
    return !!(ed && ed.textContent.trim());
  }
  function progress() {
    var done = 0;
    items.forEach(function (li) { if (answered(li)) done++; });
    var total = items.length || 1;
    ['rf-done', 'rf-done-2'].forEach(function (id) { var el = document.getElementById(id); if (el) el.textContent = done; });
    var fill = document.getElementById('rf-bar-fill');
    if (fill) fill.style.width = Math.round(100 * done / total) + '%';
    var p = document.querySelector('.rf-progress');
    if (p) p.classList.toggle('done', done === items.length);
  }
  form.querySelectorAll('.rf-num input').forEach(function (inp) {
    function check() {
      var box = inp.parentNode, v = parseFloat(inp.value.replace(',', '.'));
      box.classList.remove('in', 'out');
      if (inp.value.trim() === '' || isNaN(v) || (!inp.dataset.min && !inp.dataset.max)) return;
      var ok = (!inp.dataset.min || v >= parseFloat(inp.dataset.min)) && (!inp.dataset.max || v <= parseFloat(inp.dataset.max));
      box.classList.add(ok ? 'in' : 'out');
    }
    inp.addEventListener('input', check);
    check();
  });
  form.addEventListener('input', progress);
  form.addEventListener('change', progress);

  var editors = [];
  form.querySelectorAll('textarea[data-rich]').forEach(function (ta) {
    var wrap = document.createElement('div');
    wrap.className = 'rich-editor';
    wrap.innerHTML = '<div class="rich-bar"><button type="button" data-cmd="bold"><b>B</b></button>' +
      '<button type="button" data-cmd="italic"><i>I</i></button>' +
      '<button type="button" data-cmd="insertUnorderedList">• List</button>' +
      '<button type="button" data-cmd="insertOrderedList">1. List</button></div><div contenteditable="true"></div>';
    var ed = wrap.querySelector('[contenteditable]');
    var v = ta.value.trim();
    ed.innerHTML = v && v.charAt(0) !== '<' ? v.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/\n/g, '<br>') : v;
    wrap.querySelectorAll('[data-cmd]').forEach(function (btn) {
      btn.addEventListener('click', function () { ed.focus(); document.execCommand(btn.dataset.cmd); });
    });
    ta.hidden = true;
    ta.parentNode.insertBefore(wrap, ta);
    editors.push([ta, ed]);
  });
  function syncRich() { editors.forEach(function (p) { p[0].value = p[1].innerHTML; }); }
  form.addEventListener('submit', syncRich);
  editors.forEach(function (p) { p[1].addEventListener('input', progress); });
  progress();
})();

// Dashboard, Registry and Wiki pages: as the page loads, check the wiki for a new version of the
// registry. Additions and edits are imported and the page reloads; a version that would retire
// something is held and shown as an alert.
(function () {
  'use strict';
  if (!document.body.classList.contains('wiki-check') || !window.CSRF) return;
  fetch('/wiki/check', { method: 'POST', credentials: 'same-origin',
                         headers: { 'X-CSRF-Token': window.CSRF } })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (d) {
      if (!d || d.status === 'off') return;
      // a newer bundle than this page was drawn from (imported now, or by another page a moment ago)
      var newer = typeof d.bundle === 'number' && d.bundle > Number(document.body.dataset.bundle || 0);
      if (d.status === 'imported' || newer) {
        window.location.reload();
        return;
      }
      if (d.held && !document.querySelector('.wiki-held')) {
        var p = document.createElement('p');
        p.className = 'error wiki-held';
        p.innerHTML = 'The wiki has a new version that would retire ' + d.held.retired.map(function (x) {
          return x.replace(/[&<>]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]; });
        }).join(', ') + ", so it hasn't been loaded. <a href=\"/wiki\">Review it on Wiki and bundles</a>.";
        var main = document.querySelector('main');
        main.insertBefore(p, main.firstChild);
      }
    })
    .catch(function () {});
})();
