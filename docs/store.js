// Thin client for the server's JSON API. All state lives server-side (SQLite).
//
// Shapes (camelCase everywhere in the app):
//   cycle { id, startedOn }
//   pick  { id, cycleId, symbol, pickedOn, status: 'picked'|'ignored'|'invested', entryPrice, qty, investedOn, sells: [sell] }
//   sell  { id, qty, price, soldOn }

async function call(method, path, body) {
  const headers = { 'X-App': '1' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  let res;
  try {
    res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: 'same-origin' });
  } catch {
    throw new Error('Can\'t reach the server. Check your connection.');
  }
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const err = new Error((data && data.error) || `Request failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return data;
}

export function createStore() {
  return {
    async needsLogin() { return !(await call('GET', '/api/session')).authed; },
    login: (passcode) => call('POST', '/api/login', { passcode }),
    logout: () => call('POST', '/api/logout'),
    load: () => call('GET', '/api/state'),
    startCycle: () => call('POST', '/api/cycles'),
    addPick: async (cycleId, symbol, pickedOn) => (await call('POST', '/api/picks', { cycleId, symbol, pickedOn })).id,
    removePick: (id) => call('DELETE', `/api/picks/${id}`),
    setInvested: (id, { entryPrice, qty, investedOn }) => call('PUT', `/api/picks/${id}/invest`, { entryPrice, qty, investedOn }),
    clearInvested: (id) => call('DELETE', `/api/picks/${id}/invest`),
    setIgnored: (id, ignored) => call(ignored ? 'POST' : 'DELETE', `/api/picks/${id}/ignore`),
    sellHolding: (symbol, { qty, price, soldOn }) => call('POST', `/api/holdings/${encodeURIComponent(symbol)}/sells`, { qty, price, soldOn }),
    addSell: async (pickId, { qty, price, soldOn }) => (await call('POST', `/api/picks/${pickId}/sells`, { qty, price, soldOn })).id,
    removeSell: (id) => call('DELETE', `/api/sells/${id}`),
  };
}
