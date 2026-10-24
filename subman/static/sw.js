// Videotecario: recibe los avisos (campanita) y los enseña como notificación del móvil u ordenador.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));
self.addEventListener("push", e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (x) { d = {title: e.data ? e.data.text() : ""}; }
  e.waitUntil(self.registration.showNotification(d.title || "Videotecario", {
    body: d.body || "", icon: "/icon-192.png", badge: "/icon-192.png", data: {url: d.url || "/"}, tag: d.tag || undefined}));
});
self.addEventListener("notificationclick", e => {
  e.notification.close();
  const url = new URL((e.notification.data && e.notification.data.url) || "/", self.location.origin).href;
  e.waitUntil(self.clients.matchAll({type: "window", includeUncontrolled: true}).then(list => {
    for (const c of list) if (c.url.startsWith(self.location.origin)) { c.focus(); c.postMessage("avisos"); return; }
    return self.clients.openWindow(url);
  }));
});
