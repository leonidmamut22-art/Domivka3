# Сайт (витрина) — отдельный проект, бот от него не зависит

Сайт использует Supabase (бесплатно). Бот его НЕ использует: у бота своя база.

1. supabase.com → New project. SQL Editor → запустить `supabase/schema.sql`, затем `supabase/migration_002_telegram_admin.sql`.
2. Settings → API: скопировать Project URL и anon key → вписать вверху скрипта в `index.html` и `admin.html`. В `index.html` ещё впишите TG_BOT (имя бота без @).
3. ИИ на сайте (необязательно): `supabase functions deploy api --no-verify-jwt` и `supabase secrets set GEMINI_API_KEY=...`.
4. Загрузить папку website на app.netlify.com/drop. Админка: адрес-сайта/admin.html.
5. Себя админом сделать: зарегистрироваться на сайте, подтвердить email, в SQL Editor:
   `update profiles set is_admin=true where id=(select id from auth.users where email='ВАШ@EMAIL');`

Сайт и бот связаны только ссылками: на сайте кнопка «Telegram-бот», в боте (SITE_URL в .env) кнопка «Сайт».
Объявления и пользователи у них раздельные.
