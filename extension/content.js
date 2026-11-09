// Call Companion – content script on r3.minicrm.hu
// Reads the logged-in user's display name from the page and hands it to the background worker.
//
// IMPORTANT: the selectors below are educated guesses - the MiniCRM DOM has not been inspected yet.
// To fix them: in MiniCRM right-click your name in the top bar > Inspect > right-click the element
// in DevTools > Copy > Copy selector, then put that selector FIRST in SELECTORS and reload the extension.
// Until a selector matches, nothing is sent and the status window simply greets without a name.
(() => {
  const SELECTORS = [
    "[data-user-name]",
    "#user-menu .user-name",
    ".user-menu .user-name",
    ".navbar .user-name",
    ".header .user-name",
    ".profile-name",
    ".username",
    ".user-name",
  ];
  const NOT_A_NAME = /^(minicrm|profil|beállítások|kilépés|kijelentkezés|menü|menu)$/i;
  const FAST_MS = 3000, FAST_TRIES = 30, SLOW_MS = 60000;   // look often for 90 s after load, then once a minute

  let last = null;
  let tries = 0;
  let timer = null;

  function plausibleName(raw) {
    if (typeof raw !== "string") return null;
    const t = raw.replace(/\s+/g, " ").trim();
    if (t.length < 2 || t.length > 60) return null;
    if (/[0-9@<>{}\/\\]/.test(t) || NOT_A_NAME.test(t)) return null;
    if (t.split(" ").length > 5) return null;
    return t;
  }

  function findName() {
    for (const sel of SELECTORS) {
      let el;
      try { el = document.querySelector(sel); } catch { continue; }
      if (!el) continue;
      const name = plausibleName(el.getAttribute("data-user-name") || el.textContent);
      if (name) return name;
    }
    return null;
  }

  function tick() {
    tries++;
    const name = findName();
    if (name && name !== last) {
      try {
        chrome.runtime.sendMessage({ type: "userName", name });
        last = name;
      } catch {
        clearInterval(timer);   // extension was reloaded: this orphaned script must stop
        return;
      }
    }
    if (tries === FAST_TRIES) {
      clearInterval(timer);
      timer = setInterval(tick, SLOW_MS);
      if (!last) console.info("[CallCompanion] user name not found - adjust SELECTORS in content.js");
    }
  }

  timer = setInterval(tick, FAST_MS);
  tick();
})();
