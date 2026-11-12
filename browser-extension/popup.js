// The toolbar popup: check the app, read the current tab's book page, show a
// preview and open the app's Add Book form filled in from it. Nothing is
// saved here; the form is where you review and save.
//
// Everything that comes back from a page or the app is put on screen with
// textContent, never as HTML.

const statusEl = document.getElementById('status');

function setStatus(message, kind) {
  statusEl.textContent = message;
  statusEl.className = 'status' + (kind ? ' status--' + kind : '');
}

function openTab(url) {
  chrome.tabs.create({ url });
  window.close();
}

document.getElementById('open-settings').addEventListener('click', () => {
  chrome.runtime.openOptionsPage();
  window.close();
});

// Only ever open addresses on the configured app, whatever the response says.
function onApp(origin, url) {
  try { return new URL(url).origin === origin; } catch (e) { return false; }
}

function showPreview(origin, capture) {
  document.getElementById('preview-title').textContent = capture.title || '';
  document.getElementById('preview-authors').textContent = (capture.authors || []).join(', ');
  const series = capture.series_name
    ? capture.series_name + (capture.series_number ? ' #' + capture.series_number : '')
    : (capture.series_number ? 'Book ' + capture.series_number : '');
  document.getElementById('preview-series').textContent = series;

  const cover = document.getElementById('preview-cover');
  if (capture.cover_url && /^https:\/\//.test(capture.cover_url)) {
    cover.addEventListener('error', () => { cover.hidden = true; });
    cover.src = capture.cover_url;
    cover.hidden = false;
  }

  const existing = (capture.existing || []).filter(book => onApp(origin, book.url));
  if (existing.length) {
    const list = document.getElementById('existing-list');
    for (const book of existing) {
      const item = document.createElement('li');
      const link = document.createElement('a');
      link.href = '#';
      link.textContent = book.title + (book.owned ? '' : ' (not owned)');
      link.addEventListener('click', e => { e.preventDefault(); openTab(book.url); });
      item.appendChild(link);
      list.appendChild(item);
    }
    document.getElementById('existing').hidden = false;
  }

  for (const [id, url] of [['add-owned', capture.add_owned_url], ['add-not-owned', capture.add_not_owned_url]]) {
    const button = document.getElementById(id);
    if (onApp(origin, url)) {
      button.addEventListener('click', () => openTab(url));
    } else {
      button.disabled = true;
    }
  }
  document.getElementById('preview').hidden = false;
}

// Runs in the book page's tab (on click, via activeTab): the page as the
// browser has rendered it, including parts that load after the first paint.
function readPage() {
  return { url: location.href, html: document.documentElement.outerHTML };
}

async function main() {
  const origin = await getAppOrigin();
  if (!origin) {
    setStatus('Set the Book Database address in Settings first.', 'error');
    return;
  }
  if (!(await hasHostPermission(origin))) {
    setStatus(`This extension hasn't been allowed to contact ${origin}. Save the address again in Settings to grant it.`, 'error');
    return;
  }

  const app = await checkApp(origin);
  if (!app.ok) {
    setStatus(app.message, 'error');
    return;
  }

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const source = tab && bookPageSource(tab.url || '');
  if (!source) {
    setStatus(`Connected to Book Database v${app.version}. Open a book's page on Goodreads, Amazon or Kobo to add it.`, 'ok');
    return;
  }

  setStatus(`Reading this ${source} page…`);
  let page;
  try {
    const [result] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: readPage });
    page = result && result.result;
  } catch (e) {
    setStatus(`Couldn't read this page: ${e.message}`, 'error');
    return;
  }
  if (!page || !page.html) {
    setStatus("Couldn't read this page. Try reloading it.", 'error');
    return;
  }

  let response, body = null;
  try {
    response = await appFetch(origin, '/api/capture', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(page),
    }, 30000);
    body = await response.json();
  } catch (e) {
    setStatus(response ? 'The app sent back something unexpected.' : `Lost contact with ${origin}.`, 'error');
    return;
  }
  if (!response.ok) {
    setStatus(body && body.error ? body.error : `The app couldn't read this page (error ${response.status}).`, 'error');
    return;
  }

  setStatus(`From ${source}. Opens the Add Book form to review before saving.`, 'ok');
  showPreview(origin, body);
}

main();
