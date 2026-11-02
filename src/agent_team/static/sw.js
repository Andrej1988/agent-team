// agent-team service worker: show pushes, focus/open the app on tap.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = { body: e.data && e.data.text() }; }
  e.waitUntil(self.registration.showNotification(d.title || "agent-team", {
    body: d.body || "",
    icon: "/static/icon-192.png",
    // Android draws the badge from its alpha channel only: white on transparent.
    badge: "/static/badge-96.png",
    data: { url: d.url || "/" },
  }));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = new URL((e.notification.data && e.notification.data.url) || "/", self.location.origin).href;
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const w = wins.find((c) => c.url.startsWith(self.location.origin) && "focus" in c);
    if (w) {
      // Focus first, while the tap still counts as a user gesture — Android drops a
      // focus() that comes after an awaited navigate(). Then route: navigate() fails
      // on a window this worker doesn't control, so the page routes itself instead.
      try {
        const f = (await w.focus()) || w;
        try {
          if (!(await f.navigate(url))) f.postMessage({ type: "open", url });
        } catch {
          f.postMessage({ type: "open", url });
        }
        return;
      } catch { /* focus refused: open a window below */ }
    }
    return self.clients.openWindow(url);
  })());
});
