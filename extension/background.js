// Call Companion – background service worker (Manifest V3)
// Pushes the MiniCRM session cookies (+ the logged-in user's name) to the local app.
// If the app is not reachable (not started yet, restarting) it keeps retrying every 15-30 s.

const APP_URL = "http://127.0.0.1:48653";      // must match config.json -> local_api host/port
const MINICRM_URL = "https://r3.minicrm.hu/";
const RETRY_MIN_S = 15;                         // retry delay is random in [15, 30] s
const RETRY_MAX_S = 30;                         // (Chrome clamps alarms of packed extensions to >= 30 s)
const HEARTBEAT_MIN = 5;                        // periodic re-push; the app flags the extension as stale after 12 min
const COOKIE_DEBOUNCE_MS = 1500;
const REQUEST_TIMEOUT_MS = 4000;

let inFlight = null;       // promise of the push in progress
let queuedReason = null;   // a push was requested while another was running
let debounceTimer = null;

async function readCookies() {
  const all = await chrome.cookies.getAll({ url: MINICRM_URL });
  return all.map(c => ({ name: c.name, value: c.value, domain: c.domain, path: c.path, secure: c.secure }));
}

async function getUserName() {
  const { userName = "" } = await chrome.storage.local.get("userName");
  return userName;
}

async function scheduleRetry() {
  const delayS = RETRY_MIN_S + Math.random() * (RETRY_MAX_S - RETRY_MIN_S);
  await chrome.alarms.create("retry", { delayInMinutes: delayS / 60 });
}

async function doPush(reason) {
  const body = JSON.stringify({
    reason,
    cookies: await readCookies(),
    userAgent: navigator.userAgent,
    userName: await getUserName(),
  });
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), REQUEST_TIMEOUT_MS);
  try {
    // text/plain keeps this a "simple" request (no CORS preflight; the app answers OPTIONS with 403).
    const r = await fetch(APP_URL + "/ext/session", {
      method: "POST",
      headers: { "Content-Type": "text/plain;charset=UTF-8" },
      body,
      signal: ctl.signal,
    });
    if (!r.ok) throw new Error("HTTP " + r.status);
  } finally {
    clearTimeout(timer);
  }
}

function pushSession(reason) {
  if (inFlight) {
    queuedReason = reason;
    return inFlight;
  }
  inFlight = (async () => {
    let ok = false;
    try {
      await doPush(reason);
      ok = true;
      await chrome.alarms.clear("retry");
      console.log(`[CallCompanion] pushed session (${reason})`);
    } catch (e) {
      console.warn(`[CallCompanion] app not reachable (${reason}): ${e.message || e} - retrying`);
      await scheduleRetry();
    } finally {
      inFlight = null;
    }
    if (queuedReason) {
      const next = queuedReason;
      queuedReason = null;
      pushSession(next);
    }
    return ok;
  })();
  return inFlight;
}

async function ensureHeartbeat() {
  if (!(await chrome.alarms.get("heartbeat"))) {
    await chrome.alarms.create("heartbeat", { periodInMinutes: HEARTBEAT_MIN });
  }
}

chrome.runtime.onInstalled.addListener(() => { ensureHeartbeat(); pushSession("installed"); });
chrome.runtime.onStartup.addListener(() => { ensureHeartbeat(); pushSession("startup"); });

chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === "retry") pushSession("retry");
  else if (alarm.name === "heartbeat") pushSession("heartbeat");
});

chrome.cookies.onChanged.addListener(change => {
  if (!change.cookie.domain.endsWith("minicrm.hu")) return;
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(() => pushSession("cookies"), COOKIE_DEBOUNCE_MS);
});

// The content script reports the user's name read from the MiniCRM page.
chrome.runtime.onMessage.addListener((msg, sender) => {
  if (sender.id !== chrome.runtime.id || !msg || msg.type !== "userName") return;
  if (typeof msg.name !== "string") return;
  chrome.storage.local.get("userName").then(({ userName }) => {
    if (userName === msg.name) return;
    chrome.storage.local.set({ userName: msg.name }).then(() => pushSession("user"));
  });
});

ensureHeartbeat();   // service worker (re)started for any reason: make sure the heartbeat exists
