// Manda la sesión de YouTube de este navegador a Videotecario cuando cambia y cada 6 horas.
// Sólo habla con la dirección de Videotecario que pongas, y con su código de emparejar.
const DEFAULT_URL = "http://192.168.1.121:8787";

async function netscape() {
  const all = await chrome.cookies.getAll({domain: "youtube.com"});
  const lines = ["# Netscape HTTP Cookie File"];
  for (const c of all) {
    const domain = c.hostOnly ? c.domain : (c.domain.startsWith(".") ? c.domain : "." + c.domain);
    lines.push([domain, c.hostOnly ? "FALSE" : "TRUE", c.path, c.secure ? "TRUE" : "FALSE",
      Math.round(c.expirationDate || 0), c.name, c.value].join("\t"));
  }
  return lines.join("\n") + "\n";
}

async function push() {
  const {url = DEFAULT_URL, code = ""} = await chrome.storage.local.get(["url", "code"]);
  if (!code) return {ok: false, error: "Falta el código"};
  const cookies = await netscape();
  if (!cookies.includes("SAPISID")) {
    await chrome.storage.local.set({last: {ok: false, error: "No has entrado en YouTube en este navegador", at: Date.now()}});
    return {ok: false};
  }
  let res;
  try {
    const r = await fetch(url.replace(/\/$/, "") + "/api/yt/push", {
      method: "POST", headers: {"Content-Type": "application/json", "X-Videotecario-Key": code}, body: JSON.stringify({cookies})});
    res = await r.json().catch(() => ({ok: false, error: "Respuesta rara (" + r.status + ")"}));
  } catch (e) {
    res = {ok: false, error: "Videotecario no responde"};
  }
  await chrome.storage.local.set({last: {...res, at: Date.now()}});
  chrome.action.setBadgeText({text: res.ok ? "" : "!"});
  chrome.action.setBadgeBackgroundColor({color: "#d33"});
  return res;
}

let timer = null;
chrome.cookies.onChanged.addListener(({cookie}) => {
  if (!cookie.domain.includes("youtube.com")) return;
  clearTimeout(timer);
  timer = setTimeout(push, 60 * 1000);  // juntar los cambios de un minuto
});
chrome.runtime.onInstalled.addListener(() => chrome.alarms.create("push", {periodInMinutes: 360}));
chrome.runtime.onStartup.addListener(() => { chrome.alarms.create("push", {periodInMinutes: 360}); push(); });
chrome.alarms.onAlarm.addListener(a => { if (a.name === "push") push(); });
chrome.runtime.onMessage.addListener((msg, _s, reply) => { if (msg === "push") { push().then(reply); return true; } });
