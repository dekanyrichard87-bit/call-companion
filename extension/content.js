// Call Companion – content script on r3.minicrm.hu
// Wakes the background worker every 5 s. The worker only acts while the local app is not known to be reachable,
// so the session reaches the app within seconds of the app starting even when Chrome was running first.
// This script reads nothing from the page.
(() => {
  const POKE_MS = 5000;
  const pokeTimer = setInterval(() => {
    try { chrome.runtime.sendMessage({ type: "poke" }); } catch { clearInterval(pokeTimer); }   // extension reloaded
  }, POKE_MS);
})();
