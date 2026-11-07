// Call Companion - MiniCRM session relay (Manifest V3 service worker)
//
// Reads the MiniCRM cookies through the chrome.cookies API (not the encrypted cookie file, so Chrome's
// app-bound encryption is irrelevant) and POSTs them to the Call Companion app on THIS computer only.
// Nothing is ever sent to a remote host. Port must match config.json -> local_api.port.

const APP_URL = "http://127.0.0.1:48653/ext/session";
const COOKIE_DOMAIN = "minicrm.hu";
const ALARM = "push-session";

let debounceTimer = null;

async function setBadge(ok) {
  try {
    await chrome.action.setBadgeText({ text: ok ? "" : "!" });
    await chrome.action.setBadgeBackgroundColor({ color: "#d20f39" });
    await chrome.action.setTitle({
      title: ok ? "Call Companion relay: connected" : "Call Companion relay: app not reachable"
    });
  } catch (_) { /* badge is cosmetic */ }
}

async function pushSession(reason) {
  let cookies;
  try {
    cookies = await chrome.cookies.getAll({ domain: COOKIE_DOMAIN });
  } catch (_) {
    return;
  }
  const body = {
    v: 1,
    reason,
    userAgent: navigator.userAgent,
    cookies: cookies.map((c) => ({
      name: c.name,
      value: c.value,
      domain: c.domain,
      path: c.path,
      secure: c.secure,
      expirationDate: c.expirationDate || null
    }))
  };
  try {
    const r = await fetch(APP_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    await setBadge(r.ok);
  } catch (_) {
    await setBadge(false); // app not running yet; the next alarm retries
  }
}

function ensureAlarm() {
  chrome.alarms.get(ALARM, (a) => {
    if (!a) chrome.alarms.create(ALARM, { periodInMinutes: 1 });
  });
}

// Listeners must be registered synchronously at the top level of a MV3 service worker.
chrome.runtime.onInstalled.addListener(() => { ensureAlarm(); pushSession("installed"); });
chrome.runtime.onStartup.addListener(() => { ensureAlarm(); pushSession("startup"); });
chrome.alarms.onAlarm.addListener((a) => { if (a.name === ALARM) pushSession("alarm"); });

// Push right after the user logs in / the session cookie changes (debounced).
chrome.cookies.onChanged.addListener(({ cookie }) => {
  if (!cookie.domain.endsWith(COOKIE_DOMAIN)) return;
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(() => pushSession("cookie-change"), 1500);
});

ensureAlarm();
