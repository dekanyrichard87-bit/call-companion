// Call Companion – toolbar popup (v0.9.0): asks the local app for its state and offers "Szinkron most".
// Reads nothing from any web page; talks only to the app on 127.0.0.1 and to this extension's worker.
const APP_URL = "http://127.0.0.1:48653";
const $ = id => document.getElementById(id);

function setRow(id, cls, text) {
  const el = $(id);
  el.className = "row " + cls;
  el.querySelector(".val").textContent = text;
}

async function ping() {
  try {
    const r = await fetch(APP_URL + "/ext/ping", { method: "POST", headers: { "Content-Type": "text/plain;charset=UTF-8" },
                                                   body: "{}", cache: "no-store" });
    return r.ok ? await r.json() : null;
  } catch (e) {
    return null;
  }
}

async function refresh() {
  const st = await ping();
  const mine = chrome.runtime.getManifest();
  $("ver").textContent = "Bővítmény " + (mine.version_name || mine.version);
  if (!st) {
    setRow("app", "bad", "A Call Companion nem fut");
    setRow("crm", "", "–");
    setRow("sync", "", "–");
    $("hint").textContent = "Indítsa el a Call Companiont (Start menü). A bővítmény magától újra próbálkozik.";
    return;
  }
  setRow("app", "ok", "Kapcsolódva ✓" + (st.app ? " · " + st.app : ""));
  if (st.minicrm === true) setRow("crm", "ok", "bejelentkezve");
  else if (st.minicrm === false) setRow("crm", "bad", "jelentkezzen be a MiniCRM-be");
  else setRow("crm", "warn", "ellenőrzés…");
  setRow("sync", st.lastPush ? "ok" : "warn", st.lastPush || "még nem volt");
  const hints = [];
  if (st.minicrm === false) hints.push("Lépjen be a MiniCRM-be ebben a Chrome-ban, utána nyomja meg: Szinkron most.");
  if (st.extWanted && st.extWanted !== mine.version)
    hints.push("A programhoz újabb bővítmény tartozik (" + st.extWanted + "): chrome://extensions → ↻.");
  $("hint").textContent = hints.join(" ");
}

$("btn").addEventListener("click", async () => {
  const b = $("btn");
  b.disabled = true;
  b.textContent = "Szinkron…";
  let ok = false;
  try {
    const res = await chrome.runtime.sendMessage({ type: "sync" });
    ok = !!(res && res.ok);
  } catch (e) { ok = false; }
  b.disabled = false;
  b.textContent = ok ? "✓ Szinkronizálva" : "Nem sikerült – fut a program?";
  b.className = ok ? "done" : "";
  setTimeout(() => { b.textContent = "Szinkron most"; b.className = ""; }, 1800);
  setTimeout(refresh, 600);
});

refresh();
