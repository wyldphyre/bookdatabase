// Shared by the popup and the settings page.

// Sent with every request; the app's /api/ refuses requests without it, which
// is what keeps ordinary web pages from calling those endpoints.
const CLIENT_HEADER = { 'X-Book-Database-Client': 'extension' };

// Book pages the app can read. Kept in step with _BOOK_PAGES in scrapers.py;
// the app checks again anyway, this just avoids a round trip on other pages.
const BOOK_PAGES = [
  { source: 'Goodreads', host: /(^|\.)goodreads\.com$/, path: /^\/(?:[a-z]{2}\/)?book\/show\/\d+/ },
  { source: 'Amazon', host: /(^|\.)amazon\.[a-z.]+$/, path: /\/(?:dp|gp\/product)\/[A-Za-z0-9]{10}/ },
  { source: 'Kobo', host: /(^|\.)kobo\.com$/, path: /\/(?:ebook|audiobook)\/[^/]+/ },
];

function bookPageSource(url) {
  let parsed;
  try { parsed = new URL(url); } catch (e) { return null; }
  if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') return null;
  const page = BOOK_PAGES.find(p => p.host.test(parsed.hostname) && p.path.test(parsed.pathname));
  return page ? page.source : null;
}

// The configured app address as an origin ("http://192.168.1.20:5001"), or
// null if it isn't a usable http(s) address.
function appOrigin(address) {
  let parsed;
  try { parsed = new URL(address.trim()); } catch (e) { return null; }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return null;
  return parsed.origin;
}

async function getAppOrigin() {
  const { appOrigin: origin } = await chrome.storage.local.get('appOrigin');
  return origin || null;
}

function hasHostPermission(origin) {
  return chrome.permissions.contains({ origins: [origin + '/*'] });
}

// fetch with a timeout, so an unreachable address fails in seconds rather
// than leaving the popup spinning.
async function appFetch(origin, path, options = {}, timeoutMs = 8000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(origin + path, {
      ...options,
      headers: { ...CLIENT_HEADER, ...(options.headers || {}) },
      signal: controller.signal,
      credentials: 'omit',
    });
  } finally {
    clearTimeout(timer);
  }
}

// { ok: true, version } if the address answers as the Book Database,
// otherwise { ok: false, message } saying what went wrong.
async function checkApp(origin) {
  let response;
  try {
    response = await appFetch(origin, '/api/status', {}, 5000);
  } catch (e) {
    return { ok: false, message: `Can't reach ${origin}. Check the app is running and the address is right.` };
  }
  if (response.status === 404) {
    return { ok: false, message: `${origin} answered, but not as a Book Database new enough for this extension (v1.13 or later).` };
  }
  let body = null;
  try { body = await response.json(); } catch (e) { /* not JSON */ }
  if (!response.ok || !body || body.app !== 'Book Database') {
    return { ok: false, message: `${origin} answered, but doesn't look like the Book Database.` };
  }
  return { ok: true, version: body.version };
}
