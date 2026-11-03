import { createClient } from 'https://esm.sh/@supabase/supabase-js@2';

const sb = createClient(Deno.env.get('SUPABASE_URL')!, Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!);
const TG = Deno.env.get('TG_TOKEN')!;
const GK = Deno.env.get('GEMINI_API_KEY')!;
const MODEL = Deno.env.get('GEMINI_MODEL') ?? 'gemini-2.5-flash';
const cors = { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type' };
const esc = (s: unknown) => String(s ?? '').replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]!));
const out = (o: unknown, s = 200) => new Response(JSON.stringify(o), { status: s, headers: { ...cors, 'content-type': 'application/json' } });

const tg = (chat: number, text: string, markup?: unknown) =>
  fetch(`https://api.telegram.org/bot${TG}/sendMessage`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ chat_id: chat, text, parse_mode: 'HTML', reply_markup: markup }),
  });

const ai = async (prompt: string) => {
  const r = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${MODEL}:generateContent?key=${GK}`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ contents: [{ parts: [{ text: prompt }] }] }),
  });
  const j = await r.json();
  return (j.candidates?.[0]?.content?.parts?.[0]?.text ?? '') as string;
};

Deno.serve(async req => {
  if (req.method === 'OPTIONS') return new Response('ok', { headers: cors });
  const b = await req.json().catch(() => ({}));

  // 1) AI-підбір житла (відкритий, з обмеженням довжини)
  if (b.action === 'search') {
    const text = String(b.text ?? '').slice(0, 300);
    const raw = await ai(`Ти помічник сервісу оренди квартир в Україні. Користувач пише: "${text}".
Доступні райони: ${JSON.stringify(b.districts ?? [])}.
Поверни ТІЛЬКИ JSON без markdown: {"district":рядок з доступних районів або null,"max_price":число грн за ніч або null,"rooms":число або null,"guests":число або null,"month":true якщо довгострокова оренда,"summary":коротке пояснення українською в одне речення}`);
    try { return out(JSON.parse(raw.replace(/```json|```/g, '').trim())); } catch { return out({ error: 'ai' }, 502); }
  }

  // Далі потрібна авторизація
  const jwt = req.headers.get('authorization')?.replace('Bearer ', '') ?? '';
  const { data: { user } } = await sb.auth.getUser(jwt);
  if (!user) return out({ error: 'auth' }, 401);

  // 2) Опис оголошення від AI
  if (b.action === 'describe') {
    const t = await ai(`Напиши привабливий, чесний опис оголошення про оренду житла українською, 70-110 слів, без вигаданих фактів, без емодзі і без списків.
Дані: ${JSON.stringify(b.data).slice(0, 800)}`);
    return out({ text: t.trim() });
  }

  // 3) Нове бронювання → Telegram орендодавцю
  if (b.action === 'booking') {
    const { data: bk } = await sb.from('bookings').select('*,listings(title,owner)').eq('id', b.booking_id).eq('guest', user.id).single();
    if (!bk) return out({ error: 'not found' }, 404);
    const owner = bk.listings.owner;
    if (!owner) return out({ host: null });
    const [{ data: h }, { data: g }] = await Promise.all([
      sb.from('profiles').select('full_name,phone,tg_chat_id').eq('id', owner).single(),
      sb.from('profiles').select('full_name,phone').eq('id', user.id).single(),
    ]);
    if (h?.tg_chat_id) {
      await tg(h.tg_chat_id, `🏠 <b>Нова заявка</b>\n${esc(bk.listings.title)}\n📅 ${bk.check_in} → ${bk.check_out}\n👥 Гостей: ${bk.guests}\n💰 ${bk.total} ₴\n👤 ${esc(g?.full_name)} ${esc(g?.phone)}\n\nПідтвердьте або відхиліть:`, { inline_keyboard: [[{ text: '✅ Підтвердити', callback_data: `bs:${bk.id}:c` }, { text: '❌ Відхилити', callback_data: `bs:${bk.id}:x` }]] });
    }
    return out({ host: { name: h?.full_name, phone: h?.phone } });
  }
  return out({ error: 'unknown action' }, 400);
});
