// Persistence for cycles and picks. Two interchangeable backends:
//   local - localStorage on this device (default, zero setup)
//   cloud - Supabase, so phone and laptop share the same marks
//
// Shapes (camelCase everywhere in the app):
//   cycle { id, startedOn }
//   pick  { id, cycleId, symbol, pickedOn, status: 'picked'|'invested', entryPrice, qty, investedOn }

const SUPABASE_ESM = 'https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.45.4/+esm';
const LOCAL_KEY = 'stocks.v1';

export async function createStore(cfg, today) {
  const cloud = cfg.supabaseUrl && cfg.supabaseAnonKey && cfg.loginEmail;
  return cloud ? cloudStore(cfg, today) : localStore(today);
}

// ---------------------------------------------------------------- local ----

function localStore(today) {
  const read = () => {
    try {
      const s = JSON.parse(localStorage.getItem(LOCAL_KEY));
      if (s && Array.isArray(s.cycles) && Array.isArray(s.picks)) return s;
    } catch { /* fall through to fresh state */ }
    return { nextId: 2, cycles: [{ id: 1, startedOn: today() }], picks: [] };
  };
  const write = (s) => localStorage.setItem(LOCAL_KEY, JSON.stringify(s));

  return {
    mode: 'local',
    async needsLogin() { return false; },
    async login() {},
    async logout() {},
    async load() { return read(); },
    async startCycle() {
      const s = read(); const c = { id: s.nextId++, startedOn: today() };
      s.cycles.push(c); write(s); return c;
    },
    async addPick(cycleId, symbol, pickedOn) {
      const s = read();
      if (s.picks.some((p) => p.cycleId === cycleId && p.symbol === symbol)) throw new Error('already picked this cycle');
      const p = { id: s.nextId++, cycleId, symbol, pickedOn, status: 'picked', entryPrice: null, qty: null, investedOn: null };
      s.picks.push(p); write(s); return p.id;
    },
    async removePick(id) { const s = read(); s.picks = s.picks.filter((p) => p.id !== id); write(s); },
    async setInvested(id, { entryPrice, qty, investedOn }) {
      const s = read(); const p = s.picks.find((x) => x.id === id);
      if (!p) throw new Error('pick not found');
      Object.assign(p, { status: 'invested', entryPrice, qty, investedOn }); write(s);
    },
    async clearInvested(id) {
      const s = read(); const p = s.picks.find((x) => x.id === id);
      if (!p) throw new Error('pick not found');
      Object.assign(p, { status: 'picked', entryPrice: null, qty: null, investedOn: null }); write(s);
    },
  };
}

// ---------------------------------------------------------------- cloud ----

async function cloudStore(cfg, today) {
  const { createClient } = await import(SUPABASE_ESM);
  const sb = createClient(cfg.supabaseUrl, cfg.supabaseAnonKey);
  const check = ({ data, error }) => { if (error) throw new Error(error.message); return data; };

  const toPick = (r) => ({
    id: r.id, cycleId: r.cycle_id, symbol: r.symbol, pickedOn: r.picked_on, status: r.status,
    entryPrice: r.entry_price === null ? null : Number(r.entry_price), qty: r.qty, investedOn: r.invested_on,
  });
  const toCycle = (r) => ({ id: r.id, startedOn: r.started_on });

  return {
    mode: 'cloud',
    async needsLogin() { return !(await sb.auth.getSession()).data.session; },
    async login(passcode) {
      const { error } = await sb.auth.signInWithPassword({ email: cfg.loginEmail, password: passcode });
      if (error) throw new Error('Wrong passcode');
    },
    async logout() { await sb.auth.signOut(); },
    async load() {
      let cycles = check(await sb.from('cycles').select('*').order('id'));
      if (!cycles.length) cycles = check(await sb.from('cycles').insert({ started_on: today() }).select());
      const picks = check(await sb.from('picks').select('*').order('id'));
      return { cycles: cycles.map(toCycle), picks: picks.map(toPick) };
    },
    async startCycle() {
      return toCycle(check(await sb.from('cycles').insert({ started_on: today() }).select().single()));
    },
    async addPick(cycleId, symbol, pickedOn) {
      return check(await sb.from('picks').insert({ cycle_id: cycleId, symbol, picked_on: pickedOn }).select('id').single()).id;
    },
    async removePick(id) { check(await sb.from('picks').delete().eq('id', id)); },
    async setInvested(id, { entryPrice, qty, investedOn }) {
      check(await sb.from('picks').update({ status: 'invested', entry_price: entryPrice, qty, invested_on: investedOn }).eq('id', id));
    },
    async clearInvested(id) {
      check(await sb.from('picks').update({ status: 'picked', entry_price: null, qty: null, invested_on: null }).eq('id', id));
    },
  };
}
