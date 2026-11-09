// Call Companion – background service worker (Manifest V3)
// Pushes the MiniCRM session cookies to the local app.
// If the app is not reachable (not started yet, restarting) it retries every 2 s for the first 2 minutes
// (so the session arrives within seconds of the app starting), then every 15-30 s via an alarm; a content
// script on an open MiniCRM tab additionally "pokes" this worker every 5 s while the app is not known to be up.

const APP_URL = "http://127.0.0.1:48653";      // must match config.json -> local_api host/port
const MINICRM_URL = "https://r3.minicrm.hu/";
const RETRY_MIN_S = 15;                         // retry delay is random in [15, 30] s
const RETRY_MAX_S = 30;                         // (Chrome clamps alarms of packed extensions to >= 30 s)
const HEARTBEAT_MIN = 5;                        // periodic re-push; the app flags the extension as stale after 12 min
const COOKIE_DEBOUNCE_MS = 1500;
const REQUEST_TIMEOUT_MS = 4000;
const FAST_RETRY_MS = 2000;                     // quick retries right after a failed push ...
const FAST_WINDOW_MS = 120000;                  // ... for this long, then only the alarm (and pokes) keep trying

let inFlight = null;       // promise of the push in progress
let queuedReason = null;   // a push was requested while another was running
let debounceTimer = null;
let appUp = null;          // null = unknown yet, true = last push worked, false = last push failed
let fastLoop = false;      // a quick-retry loop is running

async function readCookies() {
  const all = await chrome.cookies.getAll({ url: MINICRM_URL });
  return all.map(c => ({ name: c.name, value: c.value, domain: c.domain, path: c.path, secure: c.secure }));
}

async function scheduleRetry() {
  const delayS = RETRY_MIN_S + Math.random() * (RETRY_MAX_S - RETRY_MIN_S);
  await chrome.alarms.create("retry", { delayInMinutes: delayS / 60 });
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

// Quick retries after a failed push. Any extension API call resets the worker's idle timer, so the worker
// stays alive while this loop runs. Not started for the alarm's own retries (no endless 2 s polling when the
// app has simply been quit).
async function fastRetry() {
  if (fastLoop) return;
  fastLoop = true;
  const until = Date.now() + FAST_WINDOW_MS;
  try {
    while (Date.now() < until) {
      await sleep(FAST_RETRY_MS);
      await chrome.runtime.getPlatformInfo();
      if (await pushSession("retry-fast")) return;
    }
  } finally {
    fastLoop = false;
  }
}

async function doPush(reason) {
  const cookies = await readCookies();
  const body = JSON.stringify({ reason, cookies, userAgent: navigator.userAgent });
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
      appUp = true;
      await chrome.alarms.clear("retry");
      console.log(`[CallCompanion] pushed session (${reason})`);
    } catch (e) {
      appUp = false;
      if (reason !== "poke" && reason !== "retry-fast") {       // those two repeat every few seconds: stay quiet
        console.warn(`[CallCompanion] app not reachable (${reason}): ${e.message || e} - retrying`);
      }
      await scheduleRetry();
      if (!["retry", "retry-fast", "poke"].includes(reason)) fastRetry();
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

// The content script on an open MiniCRM tab pokes this worker every few seconds (content.js): while the app is
// not known to be up, a poke triggers an immediate push (and keeps the worker alive).
chrome.runtime.onMessage.addListener((msg, sender) => {
  if (sender.id !== chrome.runtime.id || !msg || msg.type !== "poke") return;
  if (appUp !== true && !inFlight && !fastLoop) pushSession("poke");
});

ensureHeartbeat();   // service worker (re)started for any reason: make sure the heartbeat exists
