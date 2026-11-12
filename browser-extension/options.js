// Settings: the app's address. Saving requests host permission for that one
// origin (the manifest only lists it as optional) and drops the permission
// for any address used before, so the extension can only ever reach the app.

const form = document.getElementById('settings');
const addressInput = document.getElementById('address');
const result = document.getElementById('result');

function show(message, kind) {
  result.textContent = message;
  result.className = 'status' + (kind ? ' status--' + kind : '');
}

getAppOrigin().then(origin => { if (origin) addressInput.value = origin; });

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const origin = appOrigin(addressInput.value);
  if (!origin) {
    show('Enter an address starting with http:// or https://', 'error');
    return;
  }
  addressInput.value = origin;

  // Must be asked from the click itself, before any other await.
  const granted = await chrome.permissions.request({ origins: [origin + '/*'] });
  if (!granted) {
    show(`Not saved: the extension needs permission to contact ${origin}.`, 'error');
    return;
  }

  const previous = await getAppOrigin();
  await chrome.storage.local.set({ appOrigin: origin });
  if (previous && previous !== origin) {
    try {
      await chrome.permissions.remove({ origins: [previous + '/*'] });
    } catch (e) {
      // Already gone; nothing to tidy up
    }
  }

  show('Saved. Testing connection…');
  const app = await checkApp(origin);
  show(app.ok ? `Saved. Connected to Book Database v${app.version}.` : `Saved, but: ${app.message}`,
       app.ok ? 'ok' : 'error');
});
