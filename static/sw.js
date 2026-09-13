self.addEventListener('push', (event) => {
  let payload = {};
  try { payload = event.data ? event.data.json() : {}; } catch (_) {
    payload = { body: event.data ? event.data.text() : '' };
  }
  const title = payload.title || 'Cloudy 发来一条消息';
  event.waitUntil(self.registration.showNotification(title, {
    body: payload.body || '',
    icon: '/icons/dwell-bunny-192.png',
    badge: '/icons/dwell-bunny-64.png',
    tag: 'dwell-heartbeat',
    renotify: true,
    data: { url: payload.url || '/?from=push' },
  }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = new URL(event.notification.data?.url || '/', self.location.origin).href;
  event.waitUntil((async () => {
    const clientsList = await clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const client of clientsList) {
      if ('navigate' in client) await client.navigate(target);
      if ('focus' in client) return client.focus();
    }
    return clients.openWindow(target);
  })());
});
