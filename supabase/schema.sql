-- Run once in Supabase: Dashboard -> SQL Editor -> New query -> paste -> Run.
-- Every row belongs to the signed-in user; row level security blocks everyone else,
-- which is what makes it safe for the anon key to sit in the public page.

create table if not exists public.cycles (
  id         bigint generated always as identity primary key,
  user_id    uuid not null default auth.uid() references auth.users (id) on delete cascade,
  started_on date not null default current_date
);

create table if not exists public.picks (
  id          bigint generated always as identity primary key,
  user_id     uuid not null default auth.uid() references auth.users (id) on delete cascade,
  cycle_id    bigint not null references public.cycles (id) on delete cascade,
  symbol      text not null,
  picked_on   date not null,
  status      text not null default 'picked' check (status in ('picked', 'invested')),
  entry_price numeric(14, 2),
  qty         integer,
  invested_on date,
  unique (cycle_id, symbol),
  check (status = 'picked' or (entry_price > 0 and qty > 0 and invested_on is not null))
);

create index if not exists picks_user_cycle on public.picks (user_id, cycle_id);

alter table public.cycles enable row level security;
alter table public.picks  enable row level security;

drop policy if exists "own cycles" on public.cycles;
create policy "own cycles" on public.cycles
  for all using (user_id = auth.uid()) with check (user_id = auth.uid());

drop policy if exists "own picks" on public.picks;
create policy "own picks" on public.picks
  for all using (user_id = auth.uid()) with check (user_id = auth.uid());
