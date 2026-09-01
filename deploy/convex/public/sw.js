/* Push-only service worker: shows timer notifications (see convex/push.ts)
   and focuses/opens the app on tap. No fetch handler on purpose — assets
   keep loading straight from the network, so deploys stay instant. */
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { /* opaque payload */ }
  e.waitUntil(self.registration.showNotification(d.title || "Oriel", {
    body: d.body || "",
    tag: d.tag || "oriel-timer",
    icon: "/icon-512.png",
    badge: "/icon-512.png",
    data: { url: d.url || "/" },
  }));
});

/* Focus an open window as-is; only a fresh window navigates to the
   notification's target (e.g. reminders open the agenda sheet). */
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || "/";
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(
    (ws) => (ws[0] ? ws[0].focus() : self.clients.openWindow(url)),
  ));
});
