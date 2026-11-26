"""The script openhop adds to OpenCode Desktop's window (one <script> block in index.html, see desktop_brand).

It only adds things next to OpenCode's own UI and never changes OpenCode's code. It reuses OpenCode's own
components (data-component=icon-button-v2, text-input-v2, menu-v2-*) so it looks like the rest of the app:
- a microphone button in the chat box (voice typing, turned into text on this PC by the keeper's helper);
- a "website or :icon:" field next to the project icon (site icon or a Lucide icon), and the Color row back while
  a custom icon is set (OpenCode hides its own then; :icon: icons are transparent, so the color shows behind them);
- "Edit project" in a session tab's right-click menu (it opens the same page as the header's menu);
- the "Select model" dialog as a small hub: tabs Models (free models as cards, search, "most used on openhop"),
  Explorar (shared skills / commands / rules / prompts, installed by the helper) and Installed; the paid-provider
  cards are hidden behind a small "Use my own keys" link.
If OpenCode's markup changes, the selectors match nothing and the script does nothing.
"""

from __future__ import annotations

import hashlib

from opencode_extras import PORT

LUCIDE = "https://cdn.jsdelivr.net/npm/lucide-static@1.74.0"
# The model-picker hub + Explorar (handoff 14) isn't tested inside the real OpenCode yet and the website side
# isn't live, so 3.7.2 ships it switched off. Set True to turn it on.
HUB_ENABLED = False

JS = r"""
(() => {
  const prev = window.__openhopExtras;
  if (prev && prev.dispose) prev.dispose();
  else if (prev) return;
  const API = "http://127.0.0.1:__PORT__";
  const LUCIDE = "__LUCIDE__";
  const offs = [];
  const on = (el, ev, fn, opt) => { el.addEventListener(ev, fn, opt); offs.push(() => el.removeEventListener(ev, fn, opt)); };
  const es = () => /^es\b/i.test(document.documentElement.lang || navigator.language || "");
  const T = (en, sp) => (es() ? sp : en);
  const api = (path, opts = {}) => fetch(API + path, { ...opts, headers: { "X-Openhop": "1", ...(opts.headers || {}) } });
  const apiJson = async (path, opts) => (await api(path, opts)).json();
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  const css = `
  .ohx-mic svg{width:16px;height:16px}
  .ohx-mic[data-state=rec]{color:var(--v2-state-text-danger,#ff6369)!important;width:auto!important;padding-inline:8px!important;gap:6px;display:inline-flex;align-items:center}
  .ohx-mic[data-state=busy]{width:auto!important;padding-inline:8px!important;gap:6px;display:inline-flex;align-items:center;cursor:progress}
  .ohx-mic .ohx-lbl{font-size:12px;font-variant-numeric:tabular-nums;color:var(--v2-text-text-muted)}
  .ohx-mic[data-state=rec] .ohx-lbl{color:inherit}
  .ohx-bars{display:inline-flex;align-items:center;gap:2px;height:14px}
  .ohx-bars i{display:block;width:2px;border-radius:1px;background:currentColor;height:3px;transition:height .08s}
  .ohx-spin{width:12px;height:12px;border-radius:50%;border:1.5px solid currentColor;border-right-color:transparent;animation:ohx-spin .8s linear infinite}
  @keyframes ohx-spin{to{transform:rotate(360deg)}}
  .ohx-pop{position:fixed!important;z-index:2147483000;width:300px;padding:12px!important;display:block!important;
    font-size:13px;line-height:1.45;color:var(--v2-text-text-base)}
  .ohx-pop b{display:block;font-weight:530;margin-bottom:4px}
  .ohx-pop p{margin:0 0 12px;color:var(--v2-text-text-faint)}
  .ohx-pop .ohx-row{display:flex;gap:6px;justify-content:flex-end}
  .ohx-bar{height:4px;border-radius:2px;background:var(--v2-background-bg-layer-03,#303030);overflow:hidden;margin:2px 0 6px}
  .ohx-bar i{display:block;height:100%;width:0;background:var(--v2-text-text-base);transition:width .3s}
  .ohx-small{font-size:12px;color:var(--v2-text-text-faint);margin-bottom:10px;min-height:1em}
  .ohx-icon-ctl{display:flex;align-items:center;gap:12px}
  .ohx-icon-ctl [data-component=text-input-v2]{width:196px}
  .ohx-grid-pop{position:fixed!important;z-index:2147483000;width:264px;padding:6px!important;display:block!important}
  .ohx-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:2px;max-height:192px;overflow:auto}
  .ohx-grid button{height:34px;border:0;border-radius:6px;background:transparent;cursor:pointer;display:flex;align-items:center;
    justify-content:center;color:var(--v2-icon-icon-base)}
  .ohx-grid button:hover,.ohx-grid button:focus-visible,.ohx-grid button[data-sel]{background:var(--menu-v2-hover,var(--v2-overlay-simple-overlay-hover));outline:none}
  .ohx-grid span{width:18px;height:18px;background:currentColor;-webkit-mask:var(--u) center/contain no-repeat;mask:var(--u) center/contain no-repeat}
  .ohx-msg{font-size:12px;color:var(--v2-text-text-faint);padding:6px 6px 2px}
  .ohx-msg:empty{display:none}
  .ohx-mi[data-highlighted]{background:var(--menu-v2-hover)}
  .ohx-hide-menus [data-component=menu-v2-content]{opacity:0!important}
  .ohx-color-row[data-busy] .project-settings-colors{opacity:.5;pointer-events:none}
  .ohx-hub .ohx-paid,.ohx-hub .ohx-models>div:first-child{display:none!important}
  .ohx-hub:not([data-ohx-tab=models]) .ohx-models-only{display:none!important}
  .ohx-hub[data-ohx-tab=models] .ohx-panel{display:none}
  .ohx-tabs{display:flex;gap:2px;padding:0 16px;border-bottom:.5px solid var(--v2-border-border-muted)}
  .ohx-tabs button{all:unset;cursor:pointer;padding:8px 10px 9px;font-size:13px;font-weight:530;color:var(--v2-text-text-muted);
    border-bottom:2px solid transparent;margin-bottom:-.5px}
  .ohx-tabs button:hover{color:var(--v2-text-text-base)}
  .ohx-tabs button[aria-selected]{color:var(--v2-text-text-base);border-bottom-color:var(--v2-text-text-base)}
  .ohx-msearch{padding:12px 4px 8px}
  .ohx-msearch input,.ohx-tools input,.ohx-tools select,.ohx-form input,.ohx-form select,.ohx-form textarea,.ohx-report input{box-sizing:border-box;width:100%;
    font:inherit;font-size:13px;color:var(--v2-text-text-base);background:var(--v2-background-bg-layer-01,transparent);
    border:.5px solid var(--v2-border-border-strong,#444);border-radius:8px;padding:7px 10px;outline:none}
  .ohx-msearch input:focus,.ohx-tools input:focus,.ohx-form input:focus,.ohx-form textarea:focus{border-color:var(--v2-text-text-muted)}
  .ohx-hub .ohx-models{display:grid!important;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:8px;padding:4px 4px 8px!important}
  .ohx-hub .ohx-models>button{position:relative;flex-wrap:wrap;align-items:center;min-height:64px;padding:12px 14px!important;
    border:.5px solid var(--v2-border-border-muted);border-radius:10px!important;background:var(--v2-background-bg-layer-01,transparent)}
  .ohx-hub .ohx-models>button:hover,.ohx-hub .ohx-models>button:focus{border-color:var(--v2-border-border-strong,#555)}
  .ohx-hub .ohx-models>button.ohx-off,.ohx-hub .ohx-freetag{display:none!important}
  .ohx-hub .ohx-models>button::after{content:attr(data-ohx-desc);flex-basis:100%;font-size:12px;font-weight:440;color:var(--v2-text-text-muted);
    margin-top:2px}
  .ohx-hub .ohx-models>button[data-ohx-desc=""]::after,.ohx-hub .ohx-models>button:not([data-ohx-desc])::after{display:none}
  .ohx-hub .ohx-models>button::before{content:"";position:absolute;left:14px;bottom:8px;height:2px;border-radius:1px;width:var(--ohx-share,0);
    max-width:calc(100% - 28px);background:var(--v2-text-text-faint,#666);opacity:.6}
  .ohx-hub .ohx-models>button[data-ohx-top]{border-color:var(--v2-text-text-muted)}
  .ohx-mfoot{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:4px 8px 6px;font-size:12px;color:var(--v2-text-text-faint)}
  .ohx-link{all:unset;cursor:pointer;font-size:12px;color:var(--v2-text-text-muted);text-decoration:underline;text-underline-offset:2px}
  .ohx-link:hover{color:var(--v2-text-text-base)}
  .ohx-panel{padding:12px 4px 8px;font-size:13px;line-height:1.45;color:var(--v2-text-text-base)}
  .ohx-panel .ohx-row{display:flex;gap:6px;justify-content:flex-end;flex-wrap:wrap}
  .ohx-panel .ohx-row.ohx-left{justify-content:flex-start;margin:8px 0}
  .ohx-note{border:.5px solid var(--v2-border-border-muted);border-radius:10px;padding:14px}
  .ohx-note b{display:block;font-weight:530;margin-bottom:6px}
  .ohx-note p{margin:0 0 10px;color:var(--v2-text-text-muted)}
  .ohx-tools{display:flex;gap:6px;align-items:center}
  .ohx-tools input{flex:1}
  .ohx-tools select{width:auto}
  .ohx-chips{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0}
  .ohx-chip{all:unset;cursor:pointer;font-size:12px;padding:4px 10px;border-radius:999px;border:.5px solid var(--v2-border-border-muted);color:var(--v2-text-text-muted)}
  .ohx-chip[aria-pressed]{color:var(--v2-text-text-base);border-color:var(--v2-text-text-muted);background:var(--v2-background-bg-layer-01,transparent)}
  .ohx-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:8px}
  .ohx-list .ohx-pager,.ohx-list .ohx-small,.ohx-list .ohx-empty{grid-column:1/-1}
  .ohx-card{all:unset;box-sizing:border-box;cursor:pointer;display:flex;flex-direction:column;gap:3px;padding:12px 14px;border-radius:10px;
    border:.5px solid var(--v2-border-border-muted);background:var(--v2-background-bg-layer-01,transparent)}
  .ohx-card:hover,.ohx-card:focus-visible{border-color:var(--v2-border-border-strong,#555)}
  .ohx-card b{font-weight:530}
  .ohx-card span{color:var(--v2-text-text-muted);font-size:12px}
  .ohx-kind{align-self:flex-start;font-size:11px!important;text-transform:uppercase;letter-spacing:.04em;color:var(--v2-text-text-faint)!important}
  .ohx-meta{font-size:12px;color:var(--v2-text-text-faint)!important}
  .ohx-empty{padding:28px 8px;text-align:center;color:var(--v2-text-text-muted)}
  .ohx-item h3,.ohx-form h3{margin:4px 0;font-size:15px;font-weight:530}
  .ohx-item p{margin:0 0 4px;color:var(--v2-text-text-muted)}
  .ohx-text{white-space:pre-wrap;word-break:break-word;max-height:300px;overflow:auto;margin:10px 0;padding:10px 12px;border-radius:8px;
    font-size:12px;line-height:1.5;background:var(--v2-background-bg-layer-02,rgba(127,127,127,.08));border:.5px solid var(--v2-border-border-muted)}
  .ohx-msg2{font-size:12px;color:var(--v2-text-text-muted);min-height:1em}
  .ohx-msg2[data-bad]{color:var(--v2-state-text-danger,#ff6369)}
  .ohx-report{margin-top:10px;display:flex;gap:6px;align-items:center}
  .ohx-form label{display:flex;flex-direction:column;gap:4px;margin:10px 0;font-size:12px;color:var(--v2-text-text-muted)}
  .ohx-form textarea{resize:vertical;font-family:var(--v2-font-family-mono,monospace);font-size:12px}
  .ohx-mrow{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:8px 0;border-bottom:.5px solid var(--v2-border-border-muted)}
  .ohx-mrow .ohx-small{margin:0;word-break:break-all}
  `;
  const style = document.createElement("style");
  style.id = "openhop-extras-style";
  style.textContent = css;
  document.head.appendChild(style);

  // Same drawing style as OpenCode's own icons (16-unit box, 1-unit stroke).
  const MIC = '<svg data-slot="icon-svg" width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><rect x="5.5" y="1.5" width="5" height="8" rx="2.5" stroke="currentColor"/><path d="M3 7.5a5 5 0 0 0 10 0" stroke="currentColor" stroke-linecap="round"/><path d="M8 12.5v2" stroke="currentColor" stroke-linecap="round"/></svg>';
  const STOP = '<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><rect x="1" y="1" width="10" height="10" rx="2" fill="currentColor"/></svg>';

  // ------------------------------------------------------------------------------------------- floating card
  let pop = null;
  function closePop() { if (pop) { pop.remove(); pop = null; } }
  function card(cls) {
    const el = document.createElement("div");
    el.setAttribute("data-component", "menu-v2-content");
    el.setAttribute("data-appearance", "standard");
    el.className = cls;
    return el;
  }
  function place(el, anchor, width, below) {
    const r = anchor.getBoundingClientRect();
    el.style.left = Math.max(8, Math.min(window.innerWidth - width - 8, r.right - width)) + "px";
    el.style.top = below ? r.bottom + 6 + "px" : Math.max(8, r.top - el.offsetHeight - 8) + "px";
  }
  function openPop(anchor, html) {
    closePop();
    pop = card("ohx-pop");
    pop.innerHTML = html;
    document.body.appendChild(pop);
    place(pop, anchor, 300, false);
    return pop;
  }
  const btn = (label, main, act) => `<button type="button" data-component="button-v2" data-size="normal" data-variant="${main ? "contrast" : "ghost-muted"}" data-x="${act}">${esc(label)}</button>`;
  on(document, "mousedown", (e) => {
    if (pop && !pop.contains(e.target) && !(e.target.closest && e.target.closest(".ohx-mic"))) closePop();
  }, true);

  // ------------------------------------------------------------------------------------------- voice typing
  const rec = { on: false, stream: null, ctx: null, proc: null, chunks: [], level: 0, peak: 0, start: 0, timer: 0, btn: null, editor: null };

  function setBtn(b, state) {
    b.dataset.state = state;
    if (state === "rec") {
      b.innerHTML = STOP + '<span class="ohx-bars">' + "<i></i>".repeat(5) + '</span><span class="ohx-lbl">0:00</span>';
      b.setAttribute("aria-label", T("Stop and write it (Enter). Esc cancels", "Parar y escribirlo (Enter). Esc cancela"));
    } else if (state === "busy") {
      b.innerHTML = '<span class="ohx-spin"></span><span class="ohx-lbl">' + T("Writing…", "Escribiendo…") + "</span>";
      b.setAttribute("aria-label", T("Writing what you said", "Escribiendo lo que has dicho"));
    } else {
      b.innerHTML = MIC;
      b.setAttribute("aria-label", T("Voice typing", "Dictar por voz"));
    }
    b.title = b.getAttribute("aria-label");
  }

  const editorFor = (b) => (b.closest("[data-component=composer]") || document).querySelector("[data-component=composer-editor]");
  const fmt = (s) => Math.floor(s / 60) + ":" + String(Math.floor(s % 60)).padStart(2, "0");

  function info(b, title, text, ms) {
    openPop(b, "<b>" + esc(title) + "</b>" + (text ? "<p>" + esc(text) + "</p>" : ""));
    if (ms) setTimeout(closePop, ms);
  }

  async function startRec(b) {
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    } catch (e) {
      return info(b, T("Can't use the microphone", "No se puede usar el micrófono"),
        T("Check that one is connected and that Windows lets apps use it (Settings > Privacy > Microphone).",
          "Mira que haya uno conectado y que Windows deje usarlo (Configuración > Privacidad > Micrófono)."));
    }
    const ctx = new AudioContext({ sampleRate: 16000 });
    const src = ctx.createMediaStreamSource(stream);
    const proc = ctx.createScriptProcessor(4096, 1, 1);
    Object.assign(rec, { on: true, stream, ctx, proc, chunks: [], level: 0, peak: 0, start: Date.now(), btn: b, editor: editorFor(b) });
    proc.onaudioprocess = (e) => {
      if (!rec.on) return;
      const d = e.inputBuffer.getChannelData(0);
      rec.chunks.push(new Float32Array(d));
      let s = 0;
      for (let i = 0; i < d.length; i++) s += d[i] * d[i];
      rec.level = Math.sqrt(s / d.length);
      rec.peak = Math.max(rec.peak, rec.level);
    };
    src.connect(proc);
    proc.connect(ctx.destination);
    setBtn(b, "rec");
    rec.timer = setInterval(() => {
      const secs = (Date.now() - rec.start) / 1000;
      const t = b.querySelector(".ohx-lbl");
      if (t) t.textContent = fmt(secs);
      b.querySelectorAll(".ohx-bars i").forEach((bar, i) => {
        const v = Math.min(1, rec.level * (8 + i * 2) * (0.6 + Math.random() * 0.8));
        bar.style.height = 3 + Math.round(v * 11) + "px";
      });
      if (secs > 600) stopRec(true);
    }, 90);
  }

  function cleanup() {
    clearInterval(rec.timer);
    try { rec.proc && rec.proc.disconnect(); } catch (e) {}
    try { rec.stream && rec.stream.getTracks().forEach((t) => t.stop()); } catch (e) {}
    try { rec.ctx && rec.ctx.close(); } catch (e) {}
    rec.on = false;
  }

  function wavOf(chunks, rate) {
    let n = 0;
    chunks.forEach((c) => (n += c.length));
    const buf = new ArrayBuffer(44 + n * 2);
    const v = new DataView(buf);
    const w = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
    w(0, "RIFF"); v.setUint32(4, 36 + n * 2, true); w(8, "WAVE"); w(12, "fmt ");
    v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, rate, true);
    v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true); w(36, "data"); v.setUint32(40, n * 2, true);
    let o = 44;
    for (const c of chunks) for (let i = 0; i < c.length; i++, o += 2) {
      const s = Math.max(-1, Math.min(1, c[i]));
      v.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return new Blob([buf], { type: "audio/wav" });
  }

  function insertText(editor, text) {
    if (!editor || !text) return;
    editor.focus();
    const sel = window.getSelection();
    if (!sel.rangeCount || !editor.contains(sel.anchorNode)) {
      const r = document.createRange();
      r.selectNodeContents(editor);
      r.collapse(false);
      sel.removeAllRanges();
      sel.addRange(r);
    }
    let before = "";
    try { const r = sel.getRangeAt(0).cloneRange(); r.setStart(editor, 0); before = r.toString(); } catch (e) {}
    document.execCommand("insertText", false, (before && !/\s$/.test(before) ? " " : "") + text);
  }

  async function stopRec(send) {
    if (!rec.on) return;
    const { btn: b, editor, chunks, peak } = rec;
    const rate = rec.ctx ? rec.ctx.sampleRate : 16000;
    const secs = (Date.now() - rec.start) / 1000;
    cleanup();
    if (!send) return setBtn(b, "idle");
    if (secs < 0.6 || peak < 0.01) {
      setBtn(b, "idle");
      return info(b, T("Nothing was heard", "No se ha oído nada"),
        T("Click the microphone, talk, then click again to write it.", "Pulsa el micro, habla y vuelve a pulsar para escribirlo."), 3000);
    }
    setBtn(b, "busy");
    try {
      const r = await apiJson("/voice/transcribe?lang=" + (es() ? "es" : "auto"), { method: "POST", body: wavOf(chunks, rate), headers: { "Content-Type": "audio/wav" } });
      setBtn(b, "idle");
      if (r.ok && r.text) insertText(editor, r.text);
      else if (r.ok) info(b, T("Nothing was heard", "No se ha oído nada"), "", 2500);
      else if (r.error === "not_installed") showInstall(b);
      else info(b, T("Couldn't write it", "No se ha podido escribir"),
        T("Try again. If it keeps failing, send a report from openhop.", "Prueba otra vez. Si sigue fallando, manda un reporte desde openhop."));
    } catch (e) {
      setBtn(b, "idle");
      showNoOpenhop(b);
    }
  }

  function showNoOpenhop(b) {
    info(b, T("Open openhop", "Abre openhop"),
      T("Voice typing runs on your PC through openhop. Start openhop and try again.",
        "El dictado funciona en tu PC a través de openhop. Abre openhop y vuelve a probar."));
  }

  let pollT = 0;
  function showInstall(b, st) {
    const mb = (st && st.size_mb) || 200;
    const p = openPop(b, "<b>" + T("Voice typing", "Dictado por voz") + "</b><p>" +
      esc(T(`Talk and it's written in the chat box. It needs a one-time download (${mb} MB). After that it works offline and your voice never leaves this PC.`,
        `Hablas y se escribe en la caja del chat. Hace falta una descarga, solo la primera vez (${mb} MB). Después funciona sin internet y tu voz no sale de este PC.`)) +
      '</p><div class="ohx-bar" hidden><i></i></div><div class="ohx-small" data-x="pct" hidden></div><div class="ohx-row">' +
      btn(T("Not now", "Ahora no"), false, "close") + btn(T("Download", "Descargar"), true, "go") + "</div>");
    p.querySelector("[data-x=close]").onclick = closePop;
    const go = p.querySelector("[data-x=go]");
    const watch = () => {
      clearInterval(pollT);
      p.querySelector(".ohx-bar").hidden = false;
      p.querySelector("[data-x=pct]").hidden = false;
      go.disabled = true;
      go.textContent = T("Downloading…", "Descargando…");
      pollT = setInterval(async () => {
        let s;
        try { s = await apiJson("/voice/status"); } catch (e) { clearInterval(pollT); return; }
        const pct = s.total ? Math.floor((s.done / s.total) * 100) : 0;
        p.querySelector(".ohx-bar i").style.width = pct + "%";
        p.querySelector("[data-x=pct]").textContent = pct + "% · " + Math.round(s.done / 1e6) + " / " + Math.round(s.total / 1e6) + " MB";
        if (s.installed) {
          clearInterval(pollT);
          p.querySelector("p").textContent = T("Ready. Click the microphone and talk.", "Listo. Pulsa el micro y habla.");
          p.querySelector(".ohx-bar").hidden = true;
          p.querySelector("[data-x=pct]").hidden = true;
          p.querySelector("[data-x=close]").hidden = true;
          go.textContent = T("Done", "Hecho");
          go.disabled = false;
          go.onclick = closePop;
        } else if (!s.downloading && s.error) {
          clearInterval(pollT);
          p.querySelector("[data-x=pct]").textContent = T("The download failed. Check the connection and try again.", "La descarga ha fallado. Mira la conexión y vuelve a probar.");
          go.textContent = T("Try again", "Reintentar");
          go.disabled = false;
          go.onclick = start;
        }
      }, 700);
    };
    const start = async () => { try { await apiJson("/voice/install", { method: "POST" }); watch(); } catch (e) { showNoOpenhop(b); } };
    go.onclick = start;
    if (st && st.downloading) watch();
  }

  async function onMic(b) {
    if (rec.on) return stopRec(true);
    if (b.dataset.state === "busy") return;
    closePop();
    let st;
    try { st = await apiJson("/voice/status"); } catch (e) { return showNoOpenhop(b); }
    if (!st.installed) return showInstall(b, st);
    startRec(b);
  }

  on(document, "keydown", (e) => {
    if (!rec.on) return;
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); stopRec(false); }
    else if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); e.stopPropagation(); stopRec(true); }
  }, true);

  function addMic(actions) {
    if (actions.querySelector(".ohx-mic")) return;
    const b = document.createElement("button");
    b.type = "button";
    b.className = "ohx-mic me-1";
    // OpenCode's own icon button (same look as the "+" button next to it)
    b.setAttribute("data-component", "icon-button-v2");
    b.setAttribute("data-size", "normal");
    b.setAttribute("data-variant", "ghost-muted");
    b.classList.add("size-7", "rounded-md", "p-[6px]");
    setBtn(b, "idle");
    b.addEventListener("mousedown", (e) => e.preventDefault()); // keep the caret in the chat box
    b.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); onMic(b); });
    actions.insertBefore(b, actions.firstChild);
  }

  // ------------------------------------------------------------------------------------------- project icon
  // OpenCode paints the project's color behind a custom icon, so :icon: icons are white on transparent.
  // 3.7.0 drew them on one of these colors instead (unrelated to the project): recolor() makes those transparent.
  const OLD_BG = ["#e7af36", "#d92e3c", "#49c970", "#7698fd", "#7152f4", "#ff8648", "#0096b8", "#e4429e"];
  const VARIANTS = ["orange", "yellow", "cyan", "green", "red", "pink", "blue", "purple", "gray"];
  let tagsP = null;
  const tags = () => (tagsP = tagsP || fetch(LUCIDE + "/tags.json").then((r) => r.json()).catch(() => { tagsP = null; return {}; }));

  async function lucideFile(name) {
    const r = await fetch(LUCIDE + "/icons/" + name + ".svg");
    if (!r.ok) throw new Error("no_icon");
    let svg = await r.text();
    svg = svg.replace(/<!--[\s\S]*?-->/g, "").replace(/stroke="currentColor"/g, 'stroke="#ffffff"')
      .replace(/width="24"/, 'width="72"').replace(/height="24"/, 'height="72"');
    const img = new Image();
    const url = URL.createObjectURL(new Blob([svg], { type: "image/svg+xml" }));
    await new Promise((ok, bad) => { img.onload = ok; img.onerror = bad; img.src = url; });
    const c = document.createElement("canvas");
    c.width = c.height = 128;
    c.getContext("2d").drawImage(img, 28, 28, 72, 72);
    URL.revokeObjectURL(url);
    const blob = await new Promise((ok) => c.toBlob(ok, "image/png"));
    return new File([blob], name + ".png", { type: "image/png" });
  }

  async function siteFile(u) {
    const r = await api("/icon?u=" + encodeURIComponent(u));
    if (!r.ok) throw new Error(r.status === 404 ? "no_site_icon" : "fail");
    const blob = await r.blob();
    const ext = (blob.type.split("/")[1] || "png").replace("x-icon", "ico").replace("vnd.microsoft.icon", "ico").replace("svg+xml", "svg");
    return new File([blob], "site." + ext, { type: blob.type || "image/png" });
  }

  // The icon on screen as a file again (to put it back after changing the color). One drawn by 3.7.0 on an
  // OLD_BG color gets that color taken out (un-blended), so it becomes white on transparent like the new ones.
  async function currentIconFile(src) {
    const img = new Image();
    await new Promise((ok, bad) => { img.onload = ok; img.onerror = bad; img.src = src; });
    const w = Math.min(512, img.naturalWidth || 128);
    const h = Math.min(512, img.naturalHeight || 128);
    const c = document.createElement("canvas");
    c.width = w;
    c.height = h;
    const g = c.getContext("2d");
    g.drawImage(img, 0, 0, w, h);
    if (w === 128 && h === 128) {
      const d = g.getImageData(0, 0, 128, 128);
      const px = d.data;
      const at = (4 * 128 + 64) * 4; // top middle, inside the old rounded square, outside the icon
      const hex = "#" + [0, 1, 2].map((k) => px[at + k].toString(16).padStart(2, "0")).join("");
      if (px[at + 3] === 255 && OLD_BG.includes(hex)) {
        const bg = [px[at], px[at + 1], px[at + 2]];
        for (let i = 0; i < px.length; i += 4) {
          let a = 0;
          for (let k = 0; k < 3; k++) if (255 - bg[k] > 40) a = Math.max(a, (px[i + k] - bg[k]) / (255 - bg[k]));
          a = Math.max(0, Math.min(1, a)) * (px[i + 3] / 255);
          px[i] = px[i + 1] = px[i + 2] = 255;
          px[i + 3] = Math.round(a * 255);
        }
        g.putImageData(d, 0, 0);
      }
    }
    const blob = await new Promise((ok) => c.toBlob(ok, "image/png"));
    return new File([blob], "icon.png", { type: "image/png" });
  }

  // OpenCode hides its Color row while a custom icon is set, although it still paints that color behind the icon.
  // This row looks the same; a click takes the icon off for a moment (so OpenCode's row shows), picks the color
  // there and puts the icon back. All through OpenCode's own buttons, so it saves like a normal change.
  const COLOR_NAMES = {
    orange: ["orange", "naranja"], yellow: ["yellow", "amarillo"], cyan: ["cyan", "cian"], green: ["green", "verde"], red: ["red", "rojo"],
    pink: ["pink", "rosa"], blue: ["blue", "azul"], purple: ["purple", "morado"], gray: ["gray", "gris"],
  };
  const CHECK = '<svg class="project-settings-color-check" data-slot="icon-svg" width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true"><use href="#opencode-v2-icon-check"></use></svg>';

  function addColorRow(field) {
    const row = field.closest("[data-component=settings-row]");
    if (!row) return;
    const next = row.nextElementSibling;
    let mine = next && next.classList.contains("ohx-color-row") ? next : null;
    if (mine && mine.hasAttribute("data-busy")) return;
    const surface = field.querySelector("[data-slot=project-avatar-surface]");
    const img = field.querySelector("img[data-slot=project-avatar-image]");
    if (document.querySelector("[data-action=settings-project-color]") || !img || !surface) {
      if (mine) mine.remove();
      return;
    }
    const cur = surface.dataset.variant || "gray";
    if (mine && mine.dataset.cur === cur) return;
    if (!mine) {
      mine = document.createElement("div");
      mine.setAttribute("data-component", "settings-row");
      mine.className = "ohx-color-row";
      row.parentNode.insertBefore(mine, row.nextSibling);
    }
    mine.dataset.cur = cur;
    mine.innerHTML = '<div data-slot="settings-row-copy"><div data-slot="settings-row-title">Color</div><div data-slot="settings-row-description">' +
      esc(T("Shown behind the icon (:icon: icons are transparent).", "Se ve detrás del icono (los iconos :icono: son transparentes).")) +
      '</div></div><div data-slot="settings-row-control"><div class="project-settings-colors">' +
      VARIANTS.map((v) => {
        const sel = v === cur;
        return '<button type="button" class="project-settings-color' + (sel ? " project-settings-color--selected" : "") + '" aria-pressed="' + sel +
          '" data-v="' + v + '" aria-label="' + esc(T("Select color " + COLOR_NAMES[v][0], "Seleccionar color " + COLOR_NAMES[v][1])) + '">' +
          '<div data-component="project-avatar-v2" class="!size-5 [&_[data-slot=project-avatar-surface]]:!rounded-[6px]"><div data-slot="project-avatar-surface" data-variant="' + v +
          '"><span data-slot="project-avatar-initial"></span></div></div>' + (sel ? CHECK : "") + "</button>";
      }).join("") + "</div></div>";
    mine.querySelectorAll("button[data-v]").forEach((b) => (b.onclick = () => recolor(field, mine, b.dataset.v)));
  }

  async function recolor(field, mine, color) {
    const img = field.querySelector("img[data-slot=project-avatar-image]");
    const iconBtn = field.querySelector("[data-action=settings-project-icon]");
    if (!img || !iconBtn || document.querySelector("[data-action=settings-project-color]")) return;
    mine.setAttribute("data-busy", "");
    try {
      const file = await currentIconFile(img.src);
      iconBtn.click(); // with a custom icon set, OpenCode's icon button removes it (it never opens the file picker then)
      const theirs = await waitFor(() => document.querySelector("[data-action=settings-project-color]"), 3000);
      if (theirs) {
        const b = [...theirs.querySelectorAll("button")].find((x) => x.querySelector('[data-variant="' + color + '"]'));
        // Clicking the selected color again would clear it, so only click a different one.
        if (b && b.getAttribute("aria-pressed") !== "true") b.click();
        await sleep(60);
      }
      giveFile(field, file);
    } catch (e) {
    } finally {
      mine.removeAttribute("data-busy");
      mine.dataset.cur = "";
      // OpenCode saves in the background; redraw the row (the check mark) once the new color is on screen.
      for (const ms of [150, 600, 1500]) setTimeout(() => { if (mine.isConnected) addColorRow(field); }, ms);
    }
  }

  function giveFile(field, file) {
    const input = field.parentElement && field.parentElement.querySelector("input[type=file]");
    if (!input) throw new Error("no_input");
    const dt = new DataTransfer();
    dt.items.add(file);
    input.files = dt.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function addIconBox(field) {
    const ctl = field.parentElement;
    if (!ctl || ctl.querySelector(".ohx-icon-in")) return;
    ctl.classList.add("ohx-icon-ctl");
    const wrap = document.createElement("div");
    wrap.className = "ohx-icon-in";
    wrap.innerHTML = '<div data-component="text-input-v2" data-appearance="base" data-surface="base"><div data-slot="text-input-v2-value">' +
      '<input type="text" data-slot="text-input-v2-input" spellcheck="false" autocomplete="off"></div></div>';
    const input = wrap.querySelector("input");
    input.placeholder = T("Website or :icon:", "Web o :icono:");
    input.title = T("Paste a website (github.com) or type :rocket: and pick an icon. Enter applies it.",
      "Pega una web (github.com) o escribe :rocket: y elige un icono. Enter lo pone.");
    input.setAttribute("aria-label", input.title);
    ctl.insertBefore(wrap, field);

    let gp = null;
    const closeGrid = () => { if (gp) { gp.remove(); gp = null; } };
    const grid = () => {
      if (!gp) {
        gp = card("ohx-grid-pop");
        gp.innerHTML = '<div class="ohx-grid"></div><div class="ohx-msg"></div>';
        document.body.appendChild(gp);
      }
      place(gp, input.closest("[data-component=text-input-v2]"), 264, true);
      return gp;
    };
    const say = (t) => { grid().querySelector(".ohx-msg").textContent = t || ""; };
    on(document, "mousedown", (e) => { if (gp && !gp.contains(e.target) && !wrap.contains(e.target)) closeGrid(); }, true);

    const pick = async (fn, label) => {
      grid().querySelector(".ohx-grid").innerHTML = "";
      say(T("Loading…", "Cargando…"));
      try {
        giveFile(field, await fn());
        input.value = "";
        closeGrid();
      } catch (e) {
        const m = String(e && e.message);
        say(m === "no_icon" ? T("There's no icon with that name. Try another word (in English).", "No hay ningún icono con ese nombre. Prueba otra palabra (en inglés).")
          : m === "no_site_icon" ? T("That website has no icon, or it couldn't be reached.", "Esa web no tiene icono o no se ha podido abrir.")
          : m === "fail" || m.includes("fetch") ? T("Couldn't get it. Is openhop open?", "No se ha podido coger. ¿Está openhop abierto?")
          : T("Couldn't set the icon.", "No se ha podido poner el icono."));
      }
    };
    let searchT = 0;
    const search = async (q) => {
      const all = await tags();
      q = q.toLowerCase();
      const score = (n) => (n === q ? 0 : n.startsWith(q) ? 1 : n.includes(q) ? 2 : (all[n] || []).some((t) => t.startsWith(q)) ? 3 : 9);
      const hits = Object.keys(all).map((n) => [n, score(n)]).filter((x) => x[1] < 9)
        .sort((a, b) => a[1] - b[1] || a[0].length - b[0].length).slice(0, 49).map((x) => x[0]);
      const g = grid().querySelector(".ohx-grid");
      g.innerHTML = "";
      for (const n of hits) {
        const b = document.createElement("button");
        b.type = "button";
        b.title = ":" + n + ":";
        b.innerHTML = "<span></span>";
        b.firstChild.style.setProperty("--u", `url("${LUCIDE}/icons/${n}.svg")`);
        b.onclick = () => pick(() => lucideFile(n), ":" + n + ":");
        g.appendChild(b);
      }
      if (hits[0]) g.firstChild.dataset.sel = "";
      say(hits.length ? "" : T("No icons match.", "Ningún icono coincide."));
    };
    input.addEventListener("input", () => {
      clearTimeout(searchT);
      const v = input.value.trim();
      const m = v.match(/^:([a-z0-9-]{1,})/i);
      if (m) searchT = setTimeout(() => search(m[1]), 120);
      else if (v.startsWith(":")) { grid().querySelector(".ohx-grid").innerHTML = ""; say(T("Type a word in English: :rocket:, :cat:, :music:…", "Escribe una palabra en inglés: :rocket:, :cat:, :music:…")); }
      else closeGrid();
    });
    input.addEventListener("keydown", (e) => {
      e.stopPropagation(); // OpenCode's settings page has its own typing shortcuts
      if (e.key === "Escape") return closeGrid();
      if (e.key !== "Enter") return;
      e.preventDefault();
      const v = input.value.trim();
      if (!v) return;
      if (v.startsWith(":")) {
        const sel = gp && gp.querySelector(".ohx-grid button");
        const name = v.replace(/:/g, "").toLowerCase();
        return sel && !/:$/.test(v.slice(1)) ? sel.click() : pick(() => lucideFile(name), ":" + name + ":");
      }
      pick(() => siteFile(v), v);
    });
  }

  // ------------------------------------------------------------------------------------------- "Edit project" in a tab's menu
  let lastTab = null;
  on(document, "contextmenu", (e) => {
    const a = e.target.closest && e.target.closest("a[data-slot=tab-link][href*='/session/']");
    lastTab = a ? { a, at: Date.now() } : null;
  }, true);

  const waitFor = async (fn, ms = 2000) => { for (let t = 0; t < ms; t += 50) { const v = fn(); if (v) return v; await sleep(50); } return null; };

  async function editProject(tabLink) {
    document.documentElement.classList.add("ohx-hide-menus");
    try {
      // Show that chat first (the header menu with "Edit project" belongs to the chat on screen).
      tabLink.click();
      await sleep(150);
      const trig = await waitFor(() => {
        const t = document.querySelector("[data-slot=session-location-trigger]");
        return t && t.isConnected && t.getBoundingClientRect().width > 0 ? t : null;
      });
      if (!trig) return;
      await sleep(80);
      trig.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, button: 0, pointerType: "mouse" }));
      const gear = await waitFor(() => {
        const u = document.querySelector("[role=menuitem] use[href='#opencode-v2-icon-settings-gear']");
        return u && u.closest("[role=menuitem]");
      }, 1500);
      if (gear) {
        gear.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, button: 0, pointerType: "mouse" }));
        gear.dispatchEvent(new PointerEvent("pointerup", { bubbles: true, button: 0, pointerType: "mouse" }));
        gear.click();
      } else {
        document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      }
    } finally {
      setTimeout(() => document.documentElement.classList.remove("ohx-hide-menus"), 150);
    }
  }

  function addTabMenuItem(menu) {
    if (!lastTab || Date.now() - lastTab.at > 2000 || menu.querySelector(".ohx-mi")) return;
    const first = menu.querySelector("[role=menuitem]");
    if (!first) return;
    const tabLink = lastTab.a;
    const mi = first.cloneNode(false);
    for (const at of ["id", "data-key", "aria-disabled", "data-highlighted"]) mi.removeAttribute(at);
    mi.classList.add("ohx-mi");
    mi.innerHTML = '<span data-slot="menu-v2-item-content">' + esc(T("Edit project", "Editar proyecto")) + "</span>";
    mi.addEventListener("pointerenter", () => mi.setAttribute("data-highlighted", ""));
    mi.addEventListener("pointerleave", () => mi.removeAttribute("data-highlighted"));
    mi.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      menu.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      setTimeout(() => editProject(tabLink), 60);
    });
    const sep = menu.querySelector("[role=separator], [data-slot=menu-v2-separator]");
    if (sep) sep.parentNode.insertBefore(mi, sep);
    else menu.appendChild(mi);
  }

  // ------------------------------------------------------------------------------------------- model picker hub
  // OpenCode's "Select model" dialog (the free-models one) becomes a small hub with tabs: Models (the free models
  // as cards, with a search box and "most used on openhop this week"), Explorar (shared skills, commands, rules and
  // prompts) and Installed. The paid-provider cards are hidden behind a small "Use my own keys" link that opens
  // OpenCode's own provider list. OpenCode's model buttons stay (and keep working); only their look changes.
  const hub = { tab: "models", popular: null, view: "list", kind: "", q: "", sort: "new", page: 0, item: null, msg: "" };
  const KIND_NAMES = {
    skill: ["Skill", "Skill"], command: ["Command", "Comando"], rules: ["Rules", "Reglas"], prompt: ["Prompt", "Prompt"],
  };
  const kindName = (k) => (KIND_NAMES[k] ? T(KIND_NAMES[k][0], KIND_NAMES[k][1]) : k);
  const ERR = {
    auth: ["To publish, join the ranking first (Ranking tab in openhop).", "Para publicar, únete antes al ranking (pestaña Ranking de openhop)."],
    rate: ["Too many tries today. Try again tomorrow.", "Demasiados intentos hoy. Prueba mañana."],
    full: ["You already have 50 items. Delete one first.", "Ya tienes 50 fichas. Borra una antes."],
    not_found: ["That item isn't there any more.", "Esa ficha ya no está."],
    unavailable: ["Explorar isn't available right now.", "Explorar no está disponible ahora mismo."],
    kind: ["Pick what it is.", "Elige qué es."],
    title: ["Write a title of 3 to 60 characters.", "Escribe un título de 3 a 60 caracteres."],
    summary: ["Write a description of 10 to 200 characters.", "Escribe una descripción de 10 a 200 caracteres."],
    text: ["The text is empty or too long (32,000 characters at most).", "El texto está vacío o es demasiado largo (32.000 caracteres como mucho)."],
    binary: ["Only plain text can be shared.", "Solo se puede compartir texto."],
    tags: ["Use up to 5 tags of letters, numbers and -.", "Usa hasta 5 etiquetas de letras, números y -."],
    too_large: ["That's too long.", "Es demasiado largo."],
    offline: ["Can't reach openhop.cfd right now.", "No se puede conectar con openhop.cfd ahora mismo."],
    project: ["Open a project first.", "Abre antes un proyecto."],
    exists: ["You already have one with that name. openhop won't replace it.", "Ya tienes uno con ese nombre. openhop no lo sustituye."],
    write: ["Couldn't save the file.", "No se ha podido guardar el archivo."],
  };
  const errText = (r) => (r && ERR[r.code] ? T(ERR[r.code][0], ERR[r.code][1]) : (r && r.message) || T("Something went wrong.", "Algo ha fallado."));
  const norm = (s) => String(s || "").toLowerCase().replace(/\(free\)|free$/g, "").replace(/[^a-z0-9]/g, "");
  const fmtN = (n) => (n >= 1e9 ? (n / 1e9).toFixed(1) + "B" : n >= 1e6 ? Math.round(n / 1e6) + "M" : n >= 1e3 ? Math.round(n / 1e3) + "k" : String(n));
  const ago = (ms) => {
    const d = Math.max(0, Date.now() - ms) / 864e5;
    if (d < 1) return T("today", "hoy");
    if (d < 2) return T("yesterday", "ayer");
    return T(Math.floor(d) + " days ago", "hace " + Math.floor(d) + " días");
  };

  // The project open on screen: OpenCode puts its folder in the address, base64url-encoded. The helper only
  // accepts folders OpenCode itself has as projects, so this is just "which one".
  function projectDir() {
    const segs = (location.pathname + "/" + location.hash.replace(/^#/, "")).split(/[/?]/).filter(Boolean);
    for (const s of segs) {
      if (s.length < 4 || !/^[A-Za-z0-9_-]+=*$/.test(s)) continue;
      try {
        const b = s.replace(/-/g, "+").replace(/_/g, "/");
        const bin = atob(b + "===".slice((b.length + 3) % 4));
        const txt = new TextDecoder().decode(Uint8Array.from(bin, (c) => c.charCodeAt(0)));
        if (/^([A-Za-z]:[\\/]|\/)/.test(txt) && !/[\u0000-\u001f]/.test(txt)) return txt;
      } catch (e) {}
    }
    return "";
  }
  const projectName = () => { const p = projectDir(); return p ? p.split(/[\\/]/).filter(Boolean).pop() : ""; };

  async function loadPopular() {
    if (hub.popular) return hub.popular;
    try { hub.popular = (await apiJson("/explore/popular")).models || []; } catch (e) { hub.popular = []; }
    return hub.popular;
  }

  function markModels(sec) {
    const pop = hub.popular || [];
    const total = pop.reduce((a, m) => a + m.tokens, 0) || 1;
    sec.querySelectorAll("button").forEach((b) => {
      const nameEl = b.querySelector("span");
      const key = norm(nameEl ? nameEl.textContent : b.textContent);
      const i = pop.findIndex((m) => norm(m.id) === key || (key && norm(m.id).startsWith(key)));
      const desc = i < 0 ? "" : i === 0
        ? T("Most used on openhop this week", "El más usado en openhop esta semana")
        : T("#" + (i + 1) + " on openhop this week", "Nº " + (i + 1) + " en openhop esta semana");
      if (b.getAttribute("data-ohx-desc") !== desc) b.setAttribute("data-ohx-desc", desc);
      // Every model here is free (the footer says so): the per-card "Free" tag is just noise.
      [...b.children].forEach((c, k) => { if (k && /^(free|gratis|gratuito)$/i.test(c.textContent.trim())) c.classList.add("ohx-freetag"); });
      if (i === 0) b.setAttribute("data-ohx-top", ""); else b.removeAttribute("data-ohx-top");
      const share = i < 0 ? "" : String(Math.max(4, Math.round((pop[i].tokens / total) * 100)));
      if (share) b.style.setProperty("--ohx-share", share + "%"); else b.style.removeProperty("--ohx-share");
      const q = hub.mq || "";
      b.classList.toggle("ohx-off", !!q && !norm(b.textContent).includes(norm(q)));
    });
  }

  function addHub(sec) {
    const root = sec.parentElement;
    const dialog = sec.closest("[role=dialog]") || sec.closest("[data-component=dialog]");
    if (!root || !dialog || !root.parentElement) return;
    const paid = sec.nextElementSibling;
    if (paid && !paid.classList.contains("ohx-paid")) paid.classList.add("ohx-paid");
    sec.classList.add("ohx-models");
    if (hub.popular) markModels(sec);
    if (root.dataset.ohx) return;
    root.dataset.ohx = "1";
    root.classList.add("ohx-models-only");
    dialog.classList.add("ohx-hub");
    dialog.dataset.ohxTab = hub.tab;

    const tabs = document.createElement("div");
    tabs.className = "ohx-tabs";
    tabs.innerHTML = [["models", T("Models", "Modelos")], ["explore", "Explorar"], ["installed", T("Installed", "Instalado")]]
      .map(([k, l]) => '<button type="button" data-tab="' + k + '"' + (hub.tab === k ? " aria-selected=true" : "") + ">" + esc(l) + "</button>").join("");
    const header = dialog.querySelector("[data-slot=dialog-header]");
    if (header) header.after(tabs); else root.parentElement.prepend(tabs);

    const search = document.createElement("div");
    search.className = "ohx-msearch ohx-models-only";
    search.innerHTML = '<input type="search" placeholder="' + esc(T("Search models", "Buscar modelos")) + '" value="' + esc(hub.mq || "") + '">';
    root.before(search);
    const input = search.querySelector("input");
    input.addEventListener("input", () => { hub.mq = input.value; markModels(sec); });

    const foot = document.createElement("div");
    foot.className = "ohx-mfoot ohx-models-only";
    foot.innerHTML = "<span>" + esc(T("All free · thanks to openhop.cfd", "Todos gratis · gracias a openhop.cfd")) +
      '</span><button type="button" class="ohx-link">' + esc(T("Use my own keys…", "Usar mis propias claves…")) + "</button>";
    root.after(foot);
    foot.querySelector("button").onclick = () => {
      const more = paid && paid.querySelector("button.col-span-full");
      if (more) more.click();
    };

    const panel = document.createElement("div");
    panel.className = "ohx-panel";
    foot.after(panel);

    tabs.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-tab]");
      if (!b) return;
      hub.tab = b.dataset.tab;
      dialog.dataset.ohxTab = hub.tab;
      tabs.querySelectorAll("button").forEach((x) => x.toggleAttribute("aria-selected", x === b));
      if (hub.tab === "explore") { hub.view = "list"; renderExplore(panel); }
      if (hub.tab === "installed") renderInstalled(panel);
      if (hub.tab === "models") setTimeout(() => input.focus(), 0);
    });
    panel.addEventListener("click", (e) => onPanelClick(e, panel, dialog));
    if (hub.tab === "explore") renderExplore(panel);
    if (hub.tab === "installed") renderInstalled(panel);
    loadPopular().then(() => markModels(sec));
  }

  // ---- Explorar
  const OK_KEY = "openhop-explore-ok";
  const okSeen = () => { try { return localStorage.getItem(OK_KEY) === "1"; } catch (e) { return false; } };

  function renderExplore(panel) {
    if (!okSeen()) {
      panel.innerHTML = '<div class="ohx-note"><b>' + esc(T("Before you start", "Antes de empezar")) + "</b><p>" +
        esc(T("Everything in Explorar is shared by other openhop users. We moderate it, but we can't guarantee anything: read the text before you install it. Some items may ask the model to skip its own rules; using them is your responsibility.",
          "Todo lo que hay en Explorar lo suben otros usuarios de openhop. Lo moderamos, pero no garantizamos nada: lee el texto antes de instalarlo. Algunas fichas pueden pedir al modelo que se salte sus propias normas; usarlas es responsabilidad tuya.")) +
        "</p><p>" + esc(T("Only text is shared here (instructions for the model). Nothing in Explorar runs programs on your PC.",
          "Aquí solo se comparte texto (instrucciones para el modelo). Nada de Explorar ejecuta programas en tu PC.")) +
        '</p><div class="ohx-row">' + btn(T("Got it", "Entendido"), true, "explore-ok") + "</div></div>";
      return;
    }
    if (hub.view === "item") return renderItem(panel);
    if (hub.view === "publish") return renderPublish(panel);
    const chips = [["", T("All", "Todo")], ["skill", "Skills"], ["command", T("Commands", "Comandos")], ["rules", T("Rules", "Reglas")], ["prompt", "Prompts"]]
      .map(([k, l]) => '<button type="button" class="ohx-chip" data-x="kind" data-k="' + k + '"' + (hub.kind === k ? " aria-pressed=true" : "") + ">" + esc(l) + "</button>").join("");
    panel.innerHTML = '<div class="ohx-tools"><input type="search" class="ohx-q" placeholder="' + esc(T("Search Explorar", "Buscar en Explorar")) + '" value="' + esc(hub.q) + '">' +
      '<select class="ohx-sort"><option value="new"' + (hub.sort === "new" ? " selected" : "") + ">" + esc(T("Newest", "Nuevos")) + '</option><option value="top"' +
      (hub.sort === "top" ? " selected" : "") + ">" + esc(T("Most installed", "Más instalados")) + "</option></select>" +
      btn(T("Publish", "Publicar"), true, "publish") + '</div><div class="ohx-chips">' + chips + '</div><div class="ohx-list"><div class="ohx-small">' +
      esc(T("Loading…", "Cargando…")) + "</div></div>";
    const q = panel.querySelector(".ohx-q");
    let qt = 0;
    q.addEventListener("input", () => { clearTimeout(qt); qt = setTimeout(() => { hub.q = q.value.trim(); hub.page = 0; loadList(panel); }, 300); });
    panel.querySelector(".ohx-sort").addEventListener("change", (e) => { hub.sort = e.target.value; hub.page = 0; loadList(panel); });
    loadList(panel);
  }

  async function loadList(panel) {
    const list = panel.querySelector(".ohx-list");
    if (!list) return;
    const qs = new URLSearchParams({ sort: hub.sort, page: String(hub.page) });
    if (hub.kind) qs.set("kind", hub.kind);
    if (hub.q) qs.set("q", hub.q);
    let r;
    try { r = await apiJson("/explore/list?" + qs); } catch (e) { r = null; }
    if (!panel.isConnected || !panel.querySelector(".ohx-list")) return;
    if (!r) { list.innerHTML = '<div class="ohx-small">' + esc(T("Open openhop: Explorar works through it.", "Abre openhop: Explorar funciona a través de él.")) + "</div>"; return; }
    if (!r.ok) { list.innerHTML = '<div class="ohx-small">' + esc(errText(r)) + "</div>"; return; }
    if (!r.items.length) {
      list.innerHTML = '<div class="ohx-empty">' + esc(hub.q || hub.kind ? T("Nothing found.", "No hay nada.") : T("Nothing here yet. Be the first: press Publish.", "Aún no hay nada. Sé el primero: pulsa Publicar.")) + "</div>";
      return;
    }
    list.innerHTML = r.items.map((it) => '<button type="button" class="ohx-card" data-x="open" data-id="' + esc(it.id) + '"><span class="ohx-kind">' + esc(kindName(it.kind)) +
      "</span><b>" + esc(it.title) + "</b><span>" + esc(it.summary) + '</span><span class="ohx-meta">@' + esc(it.handle) + " · " + esc(ago(it.created)) +
      (it.installs ? " · " + esc(T(fmtN(it.installs) + " installs", fmtN(it.installs) + " instalaciones")) : "") + "</span></button>").join("") +
      '<div class="ohx-row ohx-pager">' + (hub.page ? btn(T("Previous", "Anterior"), false, "prev") : "") + (r.more ? btn(T("More", "Más"), false, "next") : "") + "</div>";
  }

  async function renderItem(panel) {
    panel.innerHTML = '<div class="ohx-small">' + esc(T("Loading…", "Cargando…")) + "</div>";
    let r;
    try { r = await apiJson("/explore/item?id=" + encodeURIComponent(hub.item)); } catch (e) { r = null; }
    if (!panel.isConnected) return;
    if (!r || !r.ok) {
      panel.innerHTML = '<div class="ohx-row ohx-left">' + btn(T("Back", "Volver"), false, "back") + '</div><div class="ohx-small">' + esc(r ? errText(r) : T("Open openhop.", "Abre openhop.")) + "</div>";
      return;
    }
    const it = r.item;
    const proj = projectName();
    const where = it.kind === "prompt" ? "" : it.kind === "rules"
      ? T("Added at the end of AGENTS.md (your own text stays).", "Se añade al final de AGENTS.md (tu texto se queda).")
      : it.kind === "skill" ? T("Saved as a skill OpenCode can use when it needs it.", "Se guarda como una skill que OpenCode usa cuando la necesita.")
        : T("Saved as a /command: type /" + it.slug + " in the chat.", "Se guarda como comando: escribe /" + it.slug + " en el chat.");
    const actions = it.kind === "prompt"
      ? btn(T("Use in the chat", "Usar en el chat"), true, "use")
      : (proj ? btn(T("Install in " + proj, "Instalar en " + proj), true, "install-project") : "") + btn(T("Install for all projects", "Instalar para todos los proyectos"), !proj, "install-global");
    panel.innerHTML = '<div class="ohx-row ohx-left">' + btn(T("Back", "Volver"), false, "back") + "</div>" +
      '<div class="ohx-item"><span class="ohx-kind">' + esc(kindName(it.kind)) + "</span><h3>" + esc(it.title) + "</h3><p>" + esc(it.summary) + '</p><div class="ohx-meta">@' +
      esc(it.handle) + " · " + esc(ago(it.created)) + (it.tags && it.tags.length ? " · " + it.tags.map((t) => "#" + esc(t)).join(" ") : "") +
      '</div><pre class="ohx-text"></pre><div class="ohx-small">' + esc(where) + '</div><div class="ohx-row ohx-left">' + actions + '</div><div class="ohx-msg2"></div>' +
      '<div class="ohx-report"><button type="button" class="ohx-link" data-x="report-open">' + esc(T("Report", "Reportar")) + "</button></div></div>";
    panel.querySelector(".ohx-text").textContent = it.text;
    panel.dataset.item = JSON.stringify({ id: it.id, kind: it.kind });
    panel._itemText = it.text;
  }

  function renderPublish(panel) {
    panel.innerHTML = '<div class="ohx-row ohx-left">' + btn(T("Back", "Volver"), false, "back") + "</div>" +
      '<div class="ohx-form"><h3>' + esc(T("Publish in Explorar", "Publicar en Explorar")) + "</h3><p class=\"ohx-small\">" +
      esc(T("Only text. It's public right away under your ranking name; the team can remove it.", "Solo texto. Sale publicado al momento con tu nombre del ranking; el equipo puede quitarlo.")) + "</p>" +
      "<label>" + esc(T("What is it?", "¿Qué es?")) + '<select name="kind">' + ["skill", "command", "rules", "prompt"].map((k) => '<option value="' + k + '">' + esc(kindName(k)) + "</option>").join("") + "</select></label>" +
      "<label>" + esc(T("Title", "Título")) + '<input name="title" maxlength="60"></label>' +
      "<label>" + esc(T("Short description", "Descripción corta")) + '<input name="summary" maxlength="200"></label>' +
      "<label>" + esc(T("Text (what the model reads)", "Texto (lo que lee el modelo)")) + '<textarea name="text" rows="10" maxlength="32000"></textarea></label>' +
      "<label>" + esc(T("Tags (optional, separated by spaces)", "Etiquetas (opcional, separadas por espacios)")) + '<input name="tags" maxlength="110"></label>' +
      "<label>" + esc(T("Language", "Idioma")) + '<select name="lang"><option value="">—</option><option value="es">Español</option><option value="en">English</option></select></label>' +
      '<div class="ohx-msg2"></div><div class="ohx-row ohx-left">' + btn(T("Publish", "Publicar"), true, "send") + "</div>" +
      "<h3>" + esc(T("Your items", "Tus fichas")) + '</h3><div class="ohx-mine ohx-small">' + esc(T("Loading…", "Cargando…")) + "</div></div>";
    loadMine(panel);
  }

  async function loadMine(panel) {
    const box = panel.querySelector(".ohx-mine");
    let r;
    try { r = await apiJson("/explore/mine"); } catch (e) { r = null; }
    if (!box || !box.isConnected) return;
    if (!r || !r.ok) { box.textContent = r ? errText(r) : ""; return; }
    if (!r.items.length) { box.textContent = T("You haven't published anything yet.", "Aún no has publicado nada."); return; }
    box.classList.remove("ohx-small");
    box.innerHTML = r.items.map((it) => '<div class="ohx-mrow"><span><b>' + esc(it.title) + "</b> · " + esc(kindName(it.kind)) +
      (it.status === "hidden" ? " · " + esc(T("hidden while the team checks it", "oculta mientras el equipo la revisa")) : "") +
      " · " + esc(T(it.installs + " installs", it.installs + " instalaciones")) + "</span>" + btn(T("Delete", "Borrar"), false, "del:" + it.id) + "</div>").join("");
  }

  async function renderInstalled(panel) {
    panel.innerHTML = '<div class="ohx-small">' + esc(T("Loading…", "Cargando…")) + "</div>";
    let r;
    try { r = await apiJson("/explore/installed"); } catch (e) { r = null; }
    if (!panel.isConnected) return;
    if (!r) { panel.innerHTML = '<div class="ohx-small">' + esc(T("Open openhop: this works through it.", "Abre openhop: esto funciona a través de él.")) + "</div>"; return; }
    const items = (r.items || []).filter((i) => i.present);
    if (!items.length) {
      panel.innerHTML = '<div class="ohx-empty">' + esc(T("Nothing installed from Explorar yet.", "Aún no has instalado nada de Explorar.")) + "</div>";
      return;
    }
    panel.innerHTML = items.map((i, n) => '<div class="ohx-mrow"><span><b>' + esc(i.title) + "</b> · " + esc(kindName(i.kind)) + " · " +
      esc(i.scope === "global" ? T("all projects", "todos los proyectos") : (i.project || "").split(/[\\/]/).filter(Boolean).pop() || "") +
      '<br><span class="ohx-small">' + esc(i.path) + "</span></span>" + btn(T("Remove", "Quitar"), false, "rm:" + n) + "</div>").join("");
    panel._installed = items;
  }

  async function post(path, body) {
    try {
      return await apiJson(path, { method: "POST", body: JSON.stringify(body), headers: { "Content-Type": "application/json" } });
    } catch (e) {
      return null;
    }
  }

  function closeDialog(dialog) {
    const x = dialog.querySelector("[data-slot=dialog-header] button, [data-slot=dialog-close-button]");
    if (x) x.click(); else document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  }

  async function onPanelClick(e, panel, dialog) {
    const b = e.target.closest("[data-x]");
    if (!b || !panel.contains(b)) return;
    const x = b.dataset.x;
    const say = (t, bad) => { const m = panel.querySelector(".ohx-msg2"); if (m) { m.textContent = t; m.toggleAttribute("data-bad", !!bad); } };
    if (x === "explore-ok") { try { localStorage.setItem(OK_KEY, "1"); } catch (err) {} return renderExplore(panel); }
    if (x === "kind") { hub.kind = b.dataset.k; hub.page = 0; return renderExplore(panel); }
    if (x === "next" || x === "prev") { hub.page += x === "next" ? 1 : -1; return loadList(panel); }
    if (x === "open") { hub.view = "item"; hub.item = b.dataset.id; return renderExplore(panel); }
    if (x === "publish") { hub.view = "publish"; return renderExplore(panel); }
    if (x === "back") { hub.view = "list"; return renderExplore(panel); }
    if (x === "install-project" || x === "install-global") {
      const it = JSON.parse(panel.dataset.item || "{}");
      b.disabled = true;
      const r = await post("/explore/install", { id: it.id, scope: x === "install-project" ? "project" : "global", project: projectDir() });
      b.disabled = false;
      if (!r) return say(T("Open openhop: installing works through it.", "Abre openhop: la instalación funciona a través de él."), true);
      return say(r.ok ? T("Installed. Start a new chat to use it.", "Instalado. Empieza un chat nuevo para usarlo.") : errText(r), !r.ok);
    }
    if (x === "use") {
      const it = JSON.parse(panel.dataset.item || "{}");
      const text = panel._itemText || "";
      post("/explore/install", { id: it.id, scope: "global", project: "" });
      closeDialog(dialog);
      setTimeout(() => insertText(document.querySelector("[data-component=composer-editor]"), text), 120);
      return;
    }
    if (x === "report-open") {
      const box = b.parentElement;
      box.innerHTML = '<input class="ohx-reason" maxlength="200" placeholder="' + esc(T("What's wrong with it? (optional)", "¿Qué le pasa? (opcional)")) + '">' + btn(T("Send report", "Enviar reporte"), false, "report");
      return;
    }
    if (x === "report") {
      const it = JSON.parse(panel.dataset.item || "{}");
      const reason = (panel.querySelector(".ohx-reason") || {}).value || "";
      const r = await post("/explore/report", { id: it.id, reason });
      b.parentElement.innerHTML = '<span class="ohx-small">' + esc(r && r.ok ? T("Thanks. The team will look at it.", "Gracias. El equipo lo mirará.") : errText(r)) + "</span>";
      return;
    }
    if (x === "send") {
      const f = panel.querySelector(".ohx-form");
      const v = (n) => f.querySelector("[name=" + n + "]").value;
      const tags = v("tags").split(/[\s,]+/).map((t) => t.replace(/^#/, "").toLowerCase()).filter(Boolean);
      b.disabled = true;
      const r = await post("/explore/submit", { kind: v("kind"), title: v("title"), summary: v("summary"), text: v("text"), tags, lang: v("lang") });
      b.disabled = false;
      if (!r) return say(T("Open openhop: publishing works through it.", "Abre openhop: publicar funciona a través de él."), true);
      if (!r.ok) return say(errText(r), true);
      f.querySelectorAll("input,textarea").forEach((el) => (el.value = ""));
      say(T("Published. It can take up to 2 minutes to show in the list.", "Publicado. Puede tardar hasta 2 minutos en salir en la lista."));
      return loadMine(panel);
    }
    if (x.startsWith("del:")) {
      b.disabled = true;
      const r = await post("/explore/delete", { id: x.slice(4) });
      if (r && r.ok) return loadMine(panel);
      b.disabled = false;
      return say(errText(r), true);
    }
    if (x.startsWith("rm:")) {
      const i = (panel._installed || [])[Number(x.slice(3))];
      if (!i) return;
      b.disabled = true;
      await post("/explore/uninstall", { id: i.id, scope: i.scope, project: i.project });
      return renderInstalled(panel);
    }
  }

  // ------------------------------------------------------------------------------------------- watch the page
  function scan() {
    document.querySelectorAll("[data-component=composer] [data-slot=composer-actions]").forEach(addMic);
    document.querySelectorAll(".project-settings-icon-field").forEach(addIconBox);
    document.querySelectorAll(".project-settings-icon-field").forEach(addColorRow);
    document.querySelectorAll("[data-component=menu-v2-content]:not(.ohx-pop):not(.ohx-grid-pop)").forEach(addTabMenuItem);
    if (__HUB__) document.querySelectorAll("[data-section=free-models]").forEach(addHub);
  }
  let queued = false;
  const mo = new MutationObserver(() => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; try { scan(); } catch (e) {} });
  });
  mo.observe(document.documentElement, { childList: true, subtree: true });
  scan();

  window.__openhopExtras = {
    dispose() {
      mo.disconnect();
      offs.forEach((f) => f());
      if (rec.on) cleanup();
      clearInterval(pollT);
      closePop();
      style.remove();
      document.querySelectorAll(".ohx-mic,.ohx-icon-in,.ohx-grid-pop,.ohx-mi,.ohx-color-row,.ohx-tabs,.ohx-msearch,.ohx-mfoot,.ohx-panel").forEach((e) => e.remove());
      document.querySelectorAll(".ohx-hub").forEach((e) => e.classList.remove("ohx-hub"));
      document.querySelectorAll(".ohx-icon-ctl").forEach((e) => e.classList.remove("ohx-icon-ctl"));
      window.__openhopExtras = null;
    },
  };
})();
"""


def script() -> str:
    return (JS.replace("__PORT__", str(PORT)).replace("__LUCIDE__", LUCIDE)
            .replace("__HUB__", "true" if HUB_ENABLED else "false"))


def version() -> str:
    return hashlib.sha256(script().encode("utf-8")).hexdigest()[:12]
