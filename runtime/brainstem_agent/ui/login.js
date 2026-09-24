// Exchanges the one-time token (URL fragment: never sent to a server, never in a
// Referer) for an HttpOnly session cookie and this tab's CSRF secret, after removing
// the token from the address bar and the history entry.
const token = location.hash.slice(1);
history.replaceState(null, '', '/login');

const status = document.getElementById('login-status');
const problem = document.getElementById('login-error');

function fail(text) {
  status.textContent = 'Not signed in';
  problem.textContent = text;
  problem.hidden = false;
}

if (!/^[A-Za-z0-9_-]{20,128}$/.test(token)) {
  fail('This page needs a sign-in link from brainstem-agent open.');
} else {
  fetch('/v1/companion/session', {
    method: 'POST',
    credentials: 'same-origin',
    cache: 'no-store',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token }),
  }).then(async (response) => {
    const body = await response.json().catch(() => ({}));
    if (!response.ok || typeof body.csrf !== 'string') {
      throw new Error(body.error || `Sign-in failed (HTTP ${response.status}).`);
    }
    sessionStorage.setItem('bsa.csrf', body.csrf);
    status.textContent = 'Signed in';
    location.replace('/');
  }).catch((error) => fail(error.message));
}
