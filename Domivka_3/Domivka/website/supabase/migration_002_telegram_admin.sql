-- Запустіть ПІСЛЯ schema.sql: Supabase → SQL Editor → Run

alter table profiles add column if not exists is_admin boolean not null default false;
alter table profiles add column if not exists banned boolean not null default false;
alter table profiles add column if not exists bot_state jsonb not null default '{}';
alter table listings add column if not exists blocked boolean not null default false;
create unique index if not exists profiles_tg_uniq on profiles(tg_chat_id) where tg_chat_id is not null;

create function is_admin() returns boolean language sql stable security definer set search_path=public as
$$ select coalesce((select is_admin from profiles where id=auth.uid()),false) $$;
create function is_banned(u uuid) returns boolean language sql stable security definer set search_path=public as
$$ select coalesce((select banned from profiles where id=u),false) $$;

-- ВИПРАВЛЕННЯ БЕЗПЕКИ: користувач не може сам собі змінити роль адміна, бан чи Telegram-прив'язку
revoke update on profiles from authenticated;
grant update(full_name,phone,role) on profiles to authenticated;
revoke update on listings from authenticated;
grant update(title,city,district,price_night,price_month,rooms,guests,description,photos,amenities,discount_pct,active) on listings to authenticated;

drop policy p_sel on profiles;
create policy p_sel on profiles for select using (id=auth.uid() or is_admin() or exists(
  select 1 from bookings b join listings l on l.id=b.listing_id where b.guest=profiles.id and l.owner=auth.uid()));

drop policy l_sel on listings;
create policy l_sel on listings for select using ((active and not blocked) or owner=auth.uid() or is_admin());
drop policy l_ins on listings;
create policy l_ins on listings for insert with check (owner=auth.uid() and not is_banned(auth.uid()) and exists(select 1 from profiles where id=auth.uid() and role='host'));

drop policy b_sel on bookings;
create policy b_sel on bookings for select using (guest=auth.uid() or is_admin() or exists(select 1 from listings l where l.id=listing_id and l.owner=auth.uid()));
drop policy b_ins on bookings;
create policy b_ins on bookings for insert with check (guest=auth.uid() and not is_banned(auth.uid()));

-- нові бронювання не приймаються на заблоковане житло
create or replace function price_booking() returns trigger language plpgsql security definer set search_path=public as $$
declare l listings; n int;
begin
  select * into l from listings where id=new.listing_id and active and not blocked;
  if not found then raise exception 'listing unavailable'; end if;
  if is_banned(new.guest) then raise exception 'banned'; end if;
  if new.check_in<current_date then raise exception 'past date'; end if;
  n:=new.check_out-new.check_in;
  if n>=28 and l.price_month is not null then
    new.total:=round(l.price_month*n/30.0*(100-l.discount_pct)/100.0);
  else new.total:=round(n*l.price_night*(100-l.discount_pct)/100.0); end if;
  new.status:='pending';
  return new;
end $$;

-- зайняті дати для сайту (без персональних даних)
create function booked_ranges(lid uuid) returns table(check_in date, check_out date) language sql stable security definer set search_path=public as
$$ select check_in,check_out from bookings where listing_id=lid and status<>'cancelled' and check_out>=current_date $$;

-- чати (працюють через бота; прямий доступ з браузера закрито)
create table conversations(
  id uuid primary key default gen_random_uuid(),
  listing_id uuid not null references listings on delete cascade,
  guest uuid not null references profiles on delete cascade,
  unique(listing_id,guest));
create table messages(
  id bigserial primary key,
  conv_id uuid not null references conversations on delete cascade,
  sender uuid not null references profiles on delete cascade,
  body text not null check(char_length(body)<=2000),
  created_at timestamptz default now());
alter table conversations enable row level security;
alter table messages enable row level security;
create policy c_admin on conversations for select using (is_admin());
create policy m_admin on messages for select using (is_admin());

-- дії адміна (тільки через ці функції)
create function admin_block_listing(lid uuid, val boolean) returns void language plpgsql security definer set search_path=public as
$$ begin if not is_admin() then raise exception 'forbidden'; end if; update listings set blocked=val where id=lid; end $$;
create function admin_ban_user(uid uuid, val boolean) returns void language plpgsql security definer set search_path=public as
$$ begin if not is_admin() then raise exception 'forbidden'; end if; update profiles set banned=val where id=uid and not is_admin;
  if val then update listings set blocked=true where owner=uid; end if; end $$;
revoke execute on function admin_block_listing, admin_ban_user from public, anon;
grant execute on function admin_block_listing, admin_ban_user to authenticated;

-- ЗРОБИТИ СЕБЕ АДМІНОМ: зареєструйтесь на сайті, потім виконайте (з вашим email):
-- update profiles set is_admin=true where id=(select id from auth.users where email='ВАШ@EMAIL');
