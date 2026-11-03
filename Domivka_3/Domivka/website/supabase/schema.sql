-- Domivka: вставте весь файл в Supabase → SQL Editor → Run
create extension if not exists btree_gist;

create table profiles(
  id uuid primary key references auth.users on delete cascade,
  role text not null default 'guest' check(role in('guest','host')),
  full_name text, phone text, tg_chat_id bigint,
  tg_token uuid not null default gen_random_uuid());

create table listings(
  id uuid primary key default gen_random_uuid(),
  owner uuid references profiles(id) on delete cascade,
  title text not null check(char_length(title) between 5 and 120),
  city text not null, district text not null,
  price_night int not null check(price_night>0),
  price_month int check(price_month>0),
  rooms int not null default 1, guests int not null default 2,
  description text default '', photos text[] default '{}', amenities text[] default '{}',
  discount_pct int not null default 0 check(discount_pct between 0 and 70),
  active boolean not null default true, created_at timestamptz default now());

create table bookings(
  id uuid primary key default gen_random_uuid(),
  listing_id uuid not null references listings on delete cascade,
  guest uuid not null references profiles on delete cascade,
  check_in date not null, check_out date not null,
  guests int not null default 1, total int not null default 0,
  status text not null default 'pending' check(status in('pending','confirmed','cancelled')),
  created_at timestamptz default now(),
  check(check_out>check_in),
  exclude using gist (listing_id with =, daterange(check_in,check_out) with &&) where (status<>'cancelled'));

create table favorites(
  user_id uuid references profiles on delete cascade,
  listing_id uuid references listings on delete cascade,
  primary key(user_id,listing_id));

-- профіль створюється автоматично при реєстрації
create function handle_new_user() returns trigger language plpgsql security definer set search_path=public as $$
begin
  insert into profiles(id,role,full_name,phone) values(new.id,
    case when new.raw_user_meta_data->>'role'='host' then 'host' else 'guest' end,
    left(new.raw_user_meta_data->>'full_name',80), left(new.raw_user_meta_data->>'phone',20));
  return new;
end $$;
create trigger on_auth_user after insert on auth.users for each row execute function handle_new_user();

-- ціна рахується на сервері, підробити з браузера неможливо
create function price_booking() returns trigger language plpgsql security definer set search_path=public as $$
declare l listings; n int;
begin
  select * into l from listings where id=new.listing_id and active;
  if not found then raise exception 'listing unavailable'; end if;
  if new.check_in<current_date then raise exception 'past date'; end if;
  n:=new.check_out-new.check_in;
  if n>=28 and l.price_month is not null then
    new.total:=round(l.price_month*n/30.0*(100-l.discount_pct)/100.0);
  else new.total:=round(n*l.price_night*(100-l.discount_pct)/100.0); end if;
  new.status:='pending';
  return new;
end $$;
create trigger bk_price before insert on bookings for each row execute function price_booking();

alter table profiles enable row level security;
alter table listings enable row level security;
alter table bookings enable row level security;
alter table favorites enable row level security;

create policy p_sel on profiles for select using (id=auth.uid() or exists(
  select 1 from bookings b join listings l on l.id=b.listing_id where b.guest=profiles.id and l.owner=auth.uid()));
create policy p_upd on profiles for update using (id=auth.uid());

create policy l_sel on listings for select using (active or owner=auth.uid());
create policy l_ins on listings for insert with check (owner=auth.uid() and exists(select 1 from profiles where id=auth.uid() and role='host'));
create policy l_upd on listings for update using (owner=auth.uid());
create policy l_del on listings for delete using (owner=auth.uid());

create policy b_sel on bookings for select using (guest=auth.uid() or exists(select 1 from listings l where l.id=listing_id and l.owner=auth.uid()));
create policy b_ins on bookings for insert with check (guest=auth.uid());
create policy b_upd on bookings for update using (exists(select 1 from listings l where l.id=listing_id and l.owner=auth.uid()));
revoke update on bookings from authenticated;
grant update(status) on bookings to authenticated;

create policy f_all on favorites for all using (user_id=auth.uid()) with check (user_id=auth.uid());

insert into storage.buckets(id,name,public) values('photos','photos',true) on conflict do nothing;
create policy ph_read on storage.objects for select using (bucket_id='photos');
create policy ph_up on storage.objects for insert to authenticated with check (bucket_id='photos' and (storage.foldername(name))[1]=auth.uid()::text);

-- демо-оголошення (без власника; видаліть, коли з'являться справжні)
insert into listings(title,city,district,price_night,price_month,rooms,guests,description,amenities,discount_pct) values
('Світла студія біля Театральної площі','Луцьк','Центр',900,17000,1,2,'Тиха студія в серці міста, 5 хвилин до замку Любарта.','{wifi,кондиціонер,кухня}',10),
('2-кімнатна з видом на Стир','Луцьк','Центр',1400,26000,2,4,'Простора квартира з балконом і видом на річку.','{wifi,балкон,пральна машина}',0),
('Затишна 1-кімнатна на 33-му','Луцьк','33-й мікрорайон',650,11000,1,2,'Свіжий ремонт, поруч супермаркет і зупинка.','{wifi,кухня}',0),
('Новобудова в Гнідаві','Луцьк','Гнідава',750,12500,1,3,'Нова квартира, паркінг у дворі, тихий район.','{wifi,паркінг}',15),
('Лофт на Подолі','Київ','Поділ',2200,42000,2,4,'Лофт з високими стелями біля Контрактової площі.','{wifi,кондиціонер,кухня}',0),
('Квартира на Печерську з видом на Лавру','Київ','Печерськ',2800,52000,2,3,'Панорамні вікна, метро Арсенальна за 7 хвилин.','{wifi,кондиціонер,паркінг}',5),
('Студія на Оболоні біля набережної','Київ','Оболонь',1300,24000,1,2,'Прогулянки вздовж Дніпра, метро поруч.','{wifi,кухня}',0),
('3-кімнатна для родини, Лівий берег','Київ','Лівий берег',1900,36000,3,6,'Дві спальні та вітальня, дитячий майданчик у дворі.','{wifi,пральна машина,паркінг}',10);
