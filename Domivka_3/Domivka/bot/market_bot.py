"""Telegram-only marketplace flows backed by the same service as the Mini App.

Integration: setup(bot_module), then call on_message/on_callback before the
legacy catch-all handlers. Returning False leaves rental handling untouched.
"""
import html
import logging
import re
import secrets
import time

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, ReplyKeyboardMarkup, KeyboardButton

import db
import market

B = None
STATE = {}
LANG = {}
COMMANDS = {'start', 'market', 'rent', 'sell', 'import', 'orders', 'services', 'settings', 'moderate', 'admin', 'club', 'friends', 'cart', 'account', 'cancel'}


def setup(module):
    global B
    B = module


def esc(value):
    return html.escape(str(value or ''), quote=False)


def tr(uid, uk, ru):
    return ru if LANG.get(uid) == 'ru' else uk


def buttons(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, callback_data='mk:' + action)
        for label, action in row] for row in rows])


async def say(uid, text, rows=None):
    return await B.bot.send_message(uid, text, reply_markup=buttons(rows) if rows else None,
                                    disable_web_page_preview=True)


async def call(uid, path, data=None, method='GET'):
    return await market.dispatch(uid, path, data, method)


async def user(from_user):
    uid = from_user.id
    await db.ex('create table if not exists market_bot_preferences(user_id integer primary key, language text not null default \'uk\')')
    pref = await db.one('select language from market_bot_preferences where user_id=?', (uid,))
    if pref:
        LANG[uid] = pref['language']
    await db.ex('insert or ignore into users(id,name,username,role) values(?,?,?,?)',
                (uid, from_user.full_name, from_user.username, 'guest'))
    u = await db.one('select * from users where id=?', (uid,))
    if u.get('banned'):
        await say(uid, tr(uid, 'Ваш обліковий запис заблоковано.', 'Ваш аккаунт заблокирован.'))
        return None
    return u


def put(uid, **data):
    STATE[uid] = dict(data, expires=time.monotonic() + 1800)


def state(uid):
    s = STATE.get(uid, {})
    if s and s.get('expires', 0) < time.monotonic():
        STATE.pop(uid, None)
        return {}
    return s


async def services(uid):
    await start_screen(uid)


async def home(uid, query='', offset=0):
    put(uid, flow='browse', query=query)
    if hasattr(B, 'S'):
        B.S.pop(uid, None)
    data = await call(uid, '/listings', {'q': query, 'offset': offset, 'limit': 6})
    all_items = data.get('items', [])
    items = all_items[offset:offset+6]
    await say(uid, tr(uid, '<b>Маркет Domivka</b>\nПошук: /market назва товару\nНе переказуйте завдаток незнайомим людям. Обирайте огляд і оплату при отриманні.',
                     '<b>Маркет Domivka</b>\nПоиск: /market название товара\nНе переводите предоплату незнакомым людям. Выбирайте осмотр и оплату при получении.'), [
        [(tr(uid, '＋ Продати річ', '＋ Продать вещь'), 'sell'), (tr(uid, 'Мої замовлення', 'Мои заказы'), 'orders')],
        [(tr(uid, 'Мої оголошення', 'Мои объявления'), 'mine'), (tr(uid, 'Повідомлення', 'Сообщения'), 'chats')],
        [(tr(uid, 'Обране', 'Избранное'), 'favs'), (tr(uid, 'Сервіси', 'Сервисы'), 'services')]])
    if not items:
        return await say(uid, tr(uid, 'Оголошень за цим запитом поки немає.', 'Объявлений по этому запросу пока нет.'))
    for item in items:
        await listing(uid, item)
    if offset + 6 < len(all_items):
        put(uid, flow='browse', query=query)
        await say(uid, tr(uid, 'Продовжити перегляд?', 'Продолжить просмотр?'), [[(tr(uid, 'Наступні', 'Следующие'), f'page:{offset+6}')]])


async def listing(uid, item, full=False):
    lid = item['id']
    owner = item.get('seller_id', item.get('owner', 0))
    text = f"<b>{esc(item.get('title'))}</b>\n<b>{esc(item.get('price'))} грн</b> · {esc(item.get('city'))}"
    summary = text
    description = str(item.get('description', item.get('descr', '')) or '')
    if full:
        text += '\n\n' + esc(description[:3500])
    rows = [[(tr(uid, 'Детальніше', 'Подробнее'), f'item:{lid}'), (tr(uid, '♡ Зберегти', '♡ Сохранить'), f'fav:{lid}')]]
    if full:
        rows = [[(tr(uid, 'Купити', 'Купить'), f'buy:{lid}'), (tr(uid, 'Написати продавцю', 'Написать продавцу'), f'chat:{lid}')],
                [(tr(uid, 'До кошика', 'В корзину'), f'cartadd:{lid}')],
                [(tr(uid, 'Профіль продавця', 'Профиль продавца'), f'seller:{owner}'), (tr(uid, 'Поскаржитися', 'Пожаловаться'), f'report:{lid}')]]
        if int(owner or 0) == uid:
            rows = [[(tr(uid, 'Зняти з публікації', 'Снять с публикации'), f'deactivate:{lid}')]]
    photos = item.get('photos') or []
    if photos:
        photo = photos[0]
        if isinstance(photo, dict):
            photo = photo.get('file_id') or photo.get('url')
        if isinstance(photo, str) and photo:
            try:
                await B.bot.send_photo(uid, photo, caption=summary, reply_markup=buttons(rows))
                if full and description:
                    await say(uid, esc(description[:3500]))
                return
            except Exception:
                logging.info('Marketplace photo unavailable, showing text card')
    await say(uid, text, rows)


async def orders(uid):
    data = await call(uid, '/orders')
    items = data.get('items', [])
    if not items:
        return await say(uid, tr(uid, 'Замовлень поки немає.', 'Заказов пока нет.'))
    for item in items[:20]:
        await order_card(uid, item)


async def order_card(uid, item):
    oid = item['id']
    status = item.get('status', '')
    labels = {'pending': ('Очікує продавця', 'Ожидает продавца'), 'accepted': ('Підтверджено продавцем', 'Подтверждён продавцом'),
              'shipped': ('Оформлено відправлення', 'Оформлена отправка'), 'completed': ('Завершено', 'Завершён'), 'cancelled': ('Скасовано', 'Отменён')}
    label = tr(uid, *labels[status]) if status in labels else status
    rows = []
    if item.get('ttn'):
        rows.append([(tr(uid, 'Відстежити доставку', 'Отследить доставку'), f'track:{oid}')])
    if status in ('pending', 'accepted'):
        rows.append([(tr(uid, 'Скасувати', 'Отменить'), f'ocancel:{oid}')])
    seller = item.get('seller_id', item.get('seller'))
    buyer = item.get('buyer_id', item.get('buyer'))
    if seller == uid and status == 'pending':
        rows.append([(tr(uid, 'Підтвердити', 'Подтвердить'), f'ostate:{oid}:accepted')])
    if seller == uid and status == 'accepted' and item.get('carrier') != 'pickup':
        rows.append([(tr(uid, 'Створити ТТН', 'Создать ТТН'), f'waybill:{oid}')])
        rows.append([(tr(uid, 'Вказати ТТН', 'Указать ТТН'), f'ttn:{oid}')])
    if buyer == uid and status in ('shipped', 'accepted'):
        rows.append([(tr(uid, 'Товар отримано', 'Товар получен'), f'ostate:{oid}:completed')])
    if status == 'completed' and buyer == uid:
        rows.append([(tr(uid, 'Залишити відгук', 'Оставить отзыв'), f'review:{oid}')])
    await say(uid, f"<b>{tr(uid, 'Замовлення', 'Заказ')} #{oid}</b>\n{esc(item.get('title', item.get('listing_title', '')))}\n"
              f"{tr(uid, 'Статус', 'Статус')}: {esc(label)}\n{esc(item.get('price', item.get('total', '')))} грн\n"
              f"{tr(uid, 'Накладна', 'Накладная')}: {esc(item.get('ttn', item.get('tracking_number', '—')))}", rows)


async def settings(uid):
    await say(uid, tr(uid, '<b>Налаштування</b>\nМова команд бота. Тему можна змінити в мініаппі.',
                     '<b>Настройки</b>\nЯзык команд бота. Тему можно изменить в миниаппе.'), [[('Українська', 'lang:uk'), ('Русский', 'lang:ru')],
                     [(tr(uid, 'Сервіси', 'Сервисы'), 'services')]])


async def moderation(uid):
    if uid not in getattr(B, 'ADMINS', set()):
        await say(uid, tr(uid, 'Ця команда доступна лише адміністратору.', 'Эта команда доступна только администратору.'))
        return
    data = await call(uid, '/moderate')
    pending, reports = data.get('pending', []), data.get('items', [])
    await say(uid, tr(uid, '<b>Модерація Маркету</b>', '<b>Модерация Маркета</b>')
              + f"\n{tr(uid, 'На перевірці', 'На проверке')}: {len(pending)}\n{tr(uid, 'Відкриті скарги', 'Открытые жалобы')}: {len(reports)}")
    for entry in pending[:30]:
        await say(uid, f"<b>#{entry['id']} · {esc(entry['title'])}</b>\n{tr(uid, 'Продавець', 'Продавец')}: {esc(entry.get('seller_id'))}",
                  moderation_rows(uid, entry['id']))
    for report in reports[:30]:
        lid = report['listing_id']
        await say(uid, f"<b>{tr(uid, 'Скарга', 'Жалоба')} #{report['id']}</b>\n#{lid} · {esc(report.get('title'))}\n{esc(report.get('reason'))}",
                  moderation_rows(uid, lid))
    if len(pending) > 30 or len(reports) > 30:
        await say(uid, tr(uid, 'Показано перші 30 у кожному списку. Повторіть /moderate після обробки.', 'Показаны первые 30 в каждом списке. Повторите /moderate после обработки.'))


def moderation_rows(uid, lid):
    return [[(tr(uid, 'Переглянути', 'Посмотреть'), f'modview:{lid}')],
            [(tr(uid, 'Схвалити', 'Одобрить'), f'mod:{lid}:active'), (tr(uid, 'Заблокувати', 'Заблокировать'), f'mod:{lid}:blocked')]]


async def start_sell(uid):
    put(uid, flow='sell', step='title', data={'photos': []})
    await say(uid, tr(uid, '<b>Продати річ</b>\nВведіть назву товару (5–120 символів).\n/cancel — скасувати.',
                     '<b>Продать вещь</b>\nВведите название товара (5–120 символов).\n/cancel — отменить.'))


async def import_listing(uid, url):
    import listing_import
    import webapp
    if not url:
        return await say(uid, tr(uid, 'Команда: /import посилання-на-ваше-оголошення\nПідтримуються DOM.RIA, OLX та monomarket. Імпорт створює чернетку для перевірки.', 'Команда: /import ссылка-на-ваше-объявление\nПоддерживаются DOM.RIA, OLX и monomarket. Импорт создаёт черновик для проверки.'))
    try:
        d = await listing_import.draft(url, webapp.parse_listing_page)
    except ValueError as e:
        return await say(uid, esc(str(e)))
    data = {'title': d.get('title', ''), 'description': d.get('descr', ''), 'city': d.get('city', ''),
            'price': int(d['price']) if d.get('price') and d.get('currency') in ('UAH', 'грн', '') else '',
            'photos': d.get('photos', [])[:8]}
    put(uid, flow='sell', step='title', data=data)
    await say(uid, tr(uid, '<b>Імпортовано чернетку</b>\nПеревірте кожне поле. /keep — залишити імпортоване значення, /cancel — скасувати. Фотографії джерела збережено у чернетці; додавайте лише власні матеріали.',
                     '<b>Импортирован черновик</b>\nПроверьте каждое поле. /keep — оставить импортированное значение, /cancel — отменить. Фотографии источника сохранены в черновике; добавляйте только свои материалы.')
              + '\n\n' + esc(data['title']) + '\n' + tr(uid, 'Введіть назву або /keep.', 'Введите название или /keep.'))


async def sell_step(uid, m, s):
    text = (m.text or '').strip()
    step, data = s['step'], s['data']
    if text == '/keep' and step not in ('photos', 'confirm'):
        text = str(data.get(step, ''))
    next_step = {'title': ('city', 'У якому місті знаходиться товар?', 'В каком городе находится товар?'),
                 'city': ('price', 'Ціна у гривнях — лише число.', 'Цена в гривнах — только число.'),
                 'price': ('category', 'Оберіть категорію товару.', 'Выберите категорию товара.'),
                 'category': ('condition', 'Оберіть стан товару.', 'Выберите состояние товара.'),
                 'condition': ('description', 'Опишіть стан, комплектацію та всі недоліки (20–4000 символів).', 'Опишите состояние, комплект и все недостатки (20–4000 символов).'),
                 'description': ('photos', 'Надішліть до 8 реальних фото товару. Потім /done для перевірки.', 'Пришлите до 8 реальных фото товара. Затем /done для проверки.')}
    if step == 'photos':
        if m.photo:
            if len(data['photos']) >= 8:
                return await say(uid, tr(uid, 'Максимум 8 фото. Натисніть /done.', 'Максимум 8 фото. Нажмите /done.'))
            await market.register_upload(uid, m.photo[-1].file_id)
            data['photos'].append(m.photo[-1].file_id)
            return await say(uid, tr(uid, f"Фото додано: {len(data['photos'])}. /done — далі.", f"Фото добавлено: {len(data['photos'])}. /done — далее."))
        if text == '/done':
            if not data['photos']:
                return await say(uid, tr(uid, 'Додайте хоча б одне фото.', 'Добавьте хотя бы одно фото.'))
            s['step'] = 'confirm'
            return await say(uid, f"<b>{esc(data['title'])}</b>\n{esc(data['city'])} · {data['price']} грн\n{esc(data['description'][:3000])}\n"
                             + tr(uid, '\nПідтверджую, що маю право продавати цей товар і використовувати фото.', '\nПодтверждаю, что имею право продавать этот товар и использовать фото.'),
                             [[(tr(uid, 'Опублікувати', 'Опубликовать'), 'publish'), (tr(uid, 'Скасувати', 'Отменить'), 'cancel')]])
        return await say(uid, tr(uid, 'Надішліть фото або /done.', 'Пришлите фото или /done.'))
    valid = True
    if step == 'title':
        valid = 5 <= len(text) <= 120
    elif step == 'city':
        valid = 2 <= len(text) <= 80
    elif step == 'price':
        valid = bool(re.fullmatch(r'\d{1,8}', text)) and 1 <= int(text) <= 99999999
    elif step == 'category':
        valid = text in ('electronics', 'furniture', 'clothes', 'home', 'sport', 'kids', 'transport', 'other')
    elif step == 'condition':
        valid = text in ('new', 'like_new', 'used', 'for_parts')
    elif step == 'description':
        valid = 20 <= len(text) <= 4000
    else:
        return await say(uid, tr(uid, 'Підтвердіть публікацію кнопкою нижче.', 'Подтвердите публикацию кнопкой ниже.'))
    if not valid:
        return await say(uid, tr(uid, 'Перевірте формат і спробуйте ще раз.', 'Проверьте формат и попробуйте ещё раз.'))
    data[step] = int(text) if step == 'price' else text
    s['step'], uk, ru = next_step[step]
    hint = ''
    if data.get(s['step']) and s['step'] != 'photos':
        hint = '\n\n' + esc(data[s['step']]) + '\n/keep'
    rows = None
    if s['step'] == 'category':
        options = [('electronics', 'Техніка', 'Техника'), ('furniture', 'Меблі', 'Мебель'), ('clothes', 'Одяг', 'Одежда'), ('home', 'Для дому', 'Для дома'), ('sport', 'Спорт', 'Спорт'), ('kids', 'Дитяче', 'Детское'), ('transport', 'Транспорт', 'Транспорт'), ('other', 'Інше', 'Другое')]
        rows = [[(tr(uid, a, b), f'field:category:{key}')] for key, a, b in options]
    elif s['step'] == 'condition':
        options = [('new', 'Новий', 'Новый'), ('like_new', 'Як новий', 'Как новый'), ('used', 'Вживаний', 'Б/у'), ('for_parts', 'На запчастини', 'На запчасти')]
        rows = [[(tr(uid, a, b), f'field:condition:{key}')] for key, a, b in options]
    await say(uid, tr(uid, uk, ru) + hint, rows)


async def on_message(m):
    if not B or not m.from_user or m.chat.type != 'private':
        return False
    uid = m.from_user.id
    text = (m.text or '').strip()
    command = text.split()[0].split('@')[0][1:] if text.startswith('/') else ''
    argument = text.partition(' ')[2].strip() if text.startswith('/') else ''
    if command == 'start' and argument and not is_market_start(argument):
        STATE.pop(uid, None)
        return False
    command = REPLY_ACTIONS.get(text, command)
    s = state(uid)
    if command not in COMMANDS and not s:
        return False
    # Existing commands always escape marketplace forms and retain rental behavior.
    if command and command not in COMMANDS and command not in ('done', 'keep'):
        STATE.pop(uid, None)
        return False
    if not await user(m.from_user):
        return True
    try:
        if command in COMMANDS:
            STATE.pop(uid, None)
            if command == 'start':
                await start_screen(uid, argument)
            elif command == 'account':
                await account(uid)
            elif command == 'cart':
                await cart(uid)
            elif command == 'club':
                await club(uid)
            elif command == 'friends':
                await friends(uid)
            elif command == 'admin':
                await admin_home(uid)
            elif command == 'market':
                await home(uid, argument)
            elif command == 'rent':
                await B.home(uid, await db.one('select * from users where id=?', (uid,)))
            elif command == 'sell':
                await start_sell(uid)
            elif command == 'import':
                await import_listing(uid, text.partition(' ')[2].strip())
            elif command == 'orders':
                await orders(uid)
            elif command == 'settings':
                await settings(uid)
            elif command == 'moderate':
                await dual_moderation(uid)
            else:
                await services(uid)
            return True
        if s.get('flow') == 'business_form':
            await business_form_step(uid, text, s)
        elif s.get('flow') == 'seller_delivery':
            await sender_step(uid, text, s)
        elif s.get('flow') == 'sell':
            await sell_step(uid, m, s)
        elif s.get('flow') == 'chat' and text:
            await call(uid, f"/chats/{s['chat_id']}/messages", {'body': text}, 'POST')
            await say(uid, tr(uid, 'Повідомлення збережено. /market — повернутися.', 'Сообщение сохранено. /market — вернуться.'))
        elif s.get('flow') == 'order':
            await checkout_step(uid, text, s)
        elif s.get('flow') == 'parcel' and text:
            try:
                weight, length, width, height = (float(x.replace(',', '.')) for x in text.split())
                if not (.1 <= weight <= 1000 and all(1 <= x <= 500 for x in (length, width, height))):
                    raise ValueError()
            except ValueError:
                await say(uid, tr(uid, 'Введіть 4 числа: вага у кг та довжина, ширина, висота у см. Наприклад: 1 30 20 10', 'Введите 4 числа: вес в кг и длина, ширина, высота в см. Например: 1 30 20 10'))
                return True
            result = await market.delivery(uid, '/delivery/waybill', {'order_id': s['order_id'], 'parcel': {'weight': weight, 'length': length, 'width': width, 'height': height}}, 'POST')
            STATE.pop(uid, None)
            await order_card(uid, result['item'])
        elif s.get('flow') == 'report' and text:
            await call(uid, '/reports', {'listing_id': s['listing_id'], 'reason': text[:1000]}, 'POST')
            STATE.pop(uid, None)
            await say(uid, tr(uid, 'Скаргу надіслано на розгляд.', 'Жалоба отправлена на рассмотрение.'))
        elif s.get('flow') == 'review' and text:
            rating, _, body = text.partition(' ')
            if rating not in ('1', '2', '3', '4', '5'):
                await say(uid, tr(uid, 'Введіть оцінку 1–5 і текст.', 'Введите оценку 1–5 и текст.'))
            else:
                await call(uid, f"/orders/{s['order_id']}/review", {'rating': int(rating), 'text': body[:2000]}, 'POST')
                STATE.pop(uid, None)
                await say(uid, tr(uid, 'Дякуємо за відгук.', 'Спасибо за отзыв.'))
        elif s.get('flow') == 'ttn' and text:
            if not re.fullmatch(r'\d{13,14}', text):
                await say(uid, tr(uid, 'Введіть номер ТТН: 13–14 цифр.', 'Введите номер ТТН: 13–14 цифр.'))
            else:
                await call(uid, f"/orders/{s['order_id']}", {'status': 'shipped', 'ttn': text}, 'PATCH')
                STATE.pop(uid, None)
                await orders(uid)
        else:
            await home(uid, text)
    except market.MarketError as e:
        await say(uid, esc(str(e)))
    except Exception:
        logging.exception('Marketplace bot message failed')
        await say(uid, tr(uid, 'Не вдалося виконати дію. Спробуйте ще раз або /cancel.', 'Не удалось выполнить действие. Попробуйте ещё раз или /cancel.'))
    return True


async def checkout_step(uid, text, s):
    fields = [('recipient_name', 'Прізвище та ім’я одержувача.', 'Фамилия и имя получателя.'),
              ('recipient_phone', 'Телефон у форматі +380XXXXXXXXX.', 'Телефон в формате +380XXXXXXXXX.'),
              ('city', 'Місто доставки.', 'Город доставки.'),
              ('branch', 'Номер та адреса відділення або індекс Укрпошти.', 'Номер и адрес отделения или индекс Укрпочты.')]
    i = s['step']
    if i >= len(fields):
        return await say(uid, tr(uid, 'Підтвердіть замовлення кнопкою або /cancel.', 'Подтвердите заказ кнопкой или /cancel.'))
    key = fields[i][0]
    if not text or len(text) > 200 or (key == 'recipient_phone' and not re.fullmatch(r'\+380\d{9}', text)):
        return await say(uid, tr(uid, 'Перевірте дані та повторіть.', 'Проверьте данные и повторите.'))
    s['data'][key] = text
    if key in ('city', 'branch') and s['data']['delivery'] == 'nova_poshta' and not s.get('manual_delivery'):
        directory = '/delivery/cities' if key == 'city' else '/delivery/warehouses'
        lookup = {'q': text, 'seller_id': s['data']['seller_id']}
        if key == 'branch':
            lookup['city_ref'] = s['data']['city_ref']
        try:
            s['choices'] = (await market.delivery(uid, directory, lookup)).get('items', [])[:20]
            if s['choices']:
                return await say(uid, tr(uid, 'Оберіть із довідника перевізника:', 'Выберите из справочника перевозчика:'),
                                 [[(str(x['name'])[:55], f'directory:{n}')] for n, x in enumerate(s['choices'])])
        except market.MarketError:
            s['manual_delivery'] = True
            await say(uid, tr(uid, 'Довідник недоступний. Збережемо текстову адресу; продавець оформить ТТН у своєму кабінеті.', 'Справочник недоступен. Сохраним текстовый адрес; продавец оформит ТТН в своём кабинете.'))
        else:
            return await say(uid, tr(uid, 'Нічого не знайдено. Уточніть назву міста чи номер відділення.', 'Ничего не найдено. Уточните город или номер отделения.'))
    if i < len(fields) - 1:
        s['step'] += 1
        return await say(uid, tr(uid, fields[i+1][1], fields[i+1][2]))
    await confirm_checkout(uid, s)


async def confirm_checkout(uid, s):
    s['step'] = 4
    await say(uid, tr(uid, '<b>Перевірте замовлення</b>\nОплата при отриманні; доставка за тарифами перевізника. Гроші через Domivka не списуються. ТТН створюється продавцем після підтвердження.',
                     '<b>Проверьте заказ</b>\nОплата при получении; доставка по тарифам перевозчика. Деньги через Domivka не списываются. ТТН создаётся продавцом после подтверждения.')
              + '\n\n' + '\n'.join(esc(s['data'][k]) for k in ('recipient_name', 'recipient_phone', 'city', 'branch')), [[(tr(uid, 'Підтвердити', 'Подтвердить'), 'placeorder'), (tr(uid, 'Скасувати', 'Отменить'), 'cancel')]])


async def on_callback(c):
    if not B or not (c.data or '').startswith('mk:'):
        return False
    await c.answer()
    if not c.message or c.message.chat.type != 'private' or not await user(c.from_user):
        return True
    uid = c.from_user.id
    parts = c.data[3:].split(':')
    action = parts[0]
    try:
        if await business_callback(uid, action, parts):
            return True
        if action in ('services', 'cancel'):
            await services(uid)
        elif action == 'rent':
            STATE.pop(uid, None)
            await B.home(uid, await db.one('select * from users where id=?', (uid,)))
        elif action == 'home':
            STATE.pop(uid, None)
            await home(uid)
        elif action == 'page':
            await home(uid, state(uid).get('query', ''), int(parts[1]))
        elif action == 'settings':
            await settings(uid)
        elif action in ('mod', 'modview'):
            if uid not in getattr(B, 'ADMINS', set()):
                await say(uid, tr(uid, 'Доступ лише для адміністратора.', 'Доступ только для администратора.'))
                return True
            lid = int(parts[1])
            if action == 'modview':
                item = (await call(uid, '/listings/' + str(lid)))['item']
                await listing(uid, item, True)
                await say(uid, tr(uid, 'Рішення модератора:', 'Решение модератора:'), moderation_rows(uid, lid))
            else:
                if parts[2] not in ('active', 'blocked'):
                    return True
                await call(uid, '/moderate', {'listing_id': lid, 'status': parts[2]}, 'POST')
                await say(uid, tr(uid, 'Рішення збережено. /moderate — наступні оголошення.', 'Решение сохранено. /moderate — следующие объявления.'))
        elif action == 'lang':
            LANG[uid] = 'ru' if parts[1] == 'ru' else 'uk'
            await db.ex('insert into market_bot_preferences(user_id,language) values(?,?) on conflict(user_id) do update set language=excluded.language', (uid, LANG[uid]))
            await settings(uid)
        elif action == 'sell':
            await start_sell(uid)
        elif action == 'field':
            from types import SimpleNamespace
            s = state(uid)
            if s.get('flow') == 'sell' and s.get('step') == parts[1] and parts[1] in ('category', 'condition'):
                await sell_step(uid, SimpleNamespace(text=parts[2], photo=[]), s)
        elif action == 'publish':
            s = state(uid)
            if s.get('flow') != 'sell' or s.get('step') != 'confirm':
                return True
            if s.get('publishing'):
                return True
            s['publishing'] = True
            payload = dict(s['data'])
            payload['descr'] = payload.pop('description')
            try:
                result = await call(uid, '/listings', payload, 'POST')
            finally:
                s['publishing'] = False
            STATE.pop(uid, None)
            await listing(uid, result['item'], True)
        elif action == 'item':
            await listing(uid, (await call(uid, '/listings/' + parts[1]))['item'], True)
        elif action == 'fav':
            await call(uid, '/favorites', {'listing_id': int(parts[1])}, 'POST')
            await say(uid, tr(uid, 'Обране оновлено.', 'Избранное обновлено.'))
        elif action in ('mine', 'favs'):
            data = await call(uid, '/listings', {'mine': '1'}) if action == 'mine' else await call(uid, '/favorites')
            if not data.get('items'):
                await say(uid, tr(uid, 'Список порожній.', 'Список пуст.'))
            for item in data.get('items', [])[:20]:
                await listing(uid, item, True)
        elif action == 'deactivate':
            await call(uid, '/listings/' + parts[1], {}, 'DELETE')
            await say(uid, tr(uid, 'Оголошення приховано.', 'Объявление скрыто.'))
        elif action == 'seller':
            seller = (await call(uid, '/sellers/' + parts[1])).get('item', {})
            await say(uid, f"<b>{esc(seller.get('name'))}</b>\n{tr(uid, 'Завершені угоди', 'Завершённые сделки')}: {esc(seller.get('completed', 0))}\n"
                      f"{tr(uid, 'Рейтинг', 'Рейтинг')}: {esc(seller.get('rating') or '—')}\n"
                      + '\n'.join(f"{esc(r.get('rating'))}/5 · {esc(str(r.get('text', ''))[:400])}" for r in seller.get('reviews', [])[:5]),
                      [[(tr(uid, 'Заблокувати користувача', 'Заблокировать пользователя'), 'block:' + parts[1])]])
        elif action == 'block':
            await call(uid, '/blocks', {'user_id': int(parts[1])}, 'POST')
            await say(uid, tr(uid, 'Користувача заблоковано для спілкування.', 'Пользователь заблокирован для общения.'))
        elif action == 'report':
            put(uid, flow='report', listing_id=int(parts[1]))
            await say(uid, tr(uid, 'Опишіть причину скарги. /cancel — скасувати.', 'Опишите причину жалобы. /cancel — отменить.'))
        elif action == 'chat':
            result = await call(uid, '/chats', {'listing_id': int(parts[1])}, 'POST')
            await open_chat(uid, result['item']['id'])
        elif action == 'chats':
            items = (await call(uid, '/chats')).get('items', [])
            await say(uid, tr(uid, 'Ваші діалоги' if items else 'Діалогів поки немає.', 'Ваши диалоги' if items else 'Диалогов пока нет.'),
                      [[(str(x.get('title', x.get('listing_title', f"Чат #{x['id']}")))[:50], f"openchat:{x['id']}")] for x in items[:20]])
        elif action == 'openchat':
            await open_chat(uid, int(parts[1]))
        elif action == 'buy':
            await say(uid, tr(uid, 'Оберіть доставку. Оплата при отриманні; для самовивозу — після огляду.', 'Выберите доставку. Оплата при получении; для самовывоза — после осмотра.'),
                      [[('Нова пошта', 'delivery:' + parts[1] + ':nova_poshta')], [('Укрпошта', 'delivery:' + parts[1] + ':ukrposhta')],
                       [(tr(uid, 'Самовивіз', 'Самовывоз'), 'delivery:' + parts[1] + ':pickup')]])
        elif action == 'delivery':
            item = (await call(uid, '/listings/' + parts[1]))['item']
            put(uid, flow='order', step=0, data={'listing_id': int(parts[1]), 'delivery': parts[2],
                                               'seller_id': item['seller_id'],
                                               'payment': 'cash' if parts[2] == 'pickup' else 'cod', 'idempotency_key': secrets.token_hex(16)})
            await say(uid, tr(uid, 'Прізвище та ім’я одержувача. /cancel — скасувати.', 'Фамилия и имя получателя. /cancel — отменить.'))
        elif action == 'placeorder':
            s = state(uid)
            if s.get('flow') == 'order' and s.get('step') == 4:
                d = s['data']
                payload = {'listing_id': d['listing_id'], 'request_id': d['idempotency_key'],
                           'carrier': {'nova_poshta': 'nova', 'ukrposhta': 'ukr', 'pickup': 'pickup'}[d['delivery']],
                           'payment': 'pickup' if d['delivery'] == 'pickup' else 'cod',
                           'recipient': {'name': d['recipient_name'], 'phone': d['recipient_phone'], 'city': d['city'], 'branch': d['branch'], 'city_ref': d.get('city_ref', ''), 'warehouse_ref': d.get('warehouse_ref', '')}}
                if d.get('checkout_seller_id'):
                    payload.pop('listing_id', None)
                    payload.update(seller_id=d['checkout_seller_id'], expected_total=d['expected_total'])
                    result = await cart_call(uid, '/checkout', payload, 'POST')
                else:
                    result = await call(uid, '/orders', payload, 'POST')
                STATE.pop(uid, None)
                if result.get('group'):
                    group = result['group']
                    await say(uid, tr(uid, '<b>Замовлення продавця оформлено</b>', '<b>Заказ продавца оформлен</b>') + '\n' + '\n'.join('• ' + esc(x.get('title')) for x in group.get('items', [])) + f"\n{tr(uid, 'Разом', 'Всего')}: {esc(group.get('total', group.get('subtotal')))} грн")
                await order_card(uid, result['item'])
        elif action == 'orders':
            await orders(uid)
        elif action == 'ocancel':
            await call(uid, f'/orders/{parts[1]}', {'status': 'cancelled'}, 'PATCH')
            await orders(uid)
        elif action == 'ostate':
            await call(uid, f'/orders/{parts[1]}', {'status': parts[2]}, 'PATCH')
            await orders(uid)
        elif action == 'waybill':
            cfg = await market.delivery(uid, '/delivery/settings')
            if not cfg['item'].get('sender') or cfg['item'].get('mode') in ('central_not_configured', 'approval_required'):
                await say(uid, tr(uid, 'Заповніть дані відправника: ім’я, телефон та відділення. API-ключ не потрібен. Якщо підключення перевізника ще перевіряється, можна оформити ТТН у кабінеті Нової пошти та вказати її вручну.', 'Заполните данные отправителя: имя, телефон и отделение. API-ключ не нужен. Если подключение перевозчика ещё проверяется, можно оформить ТТН в кабинете Новой почты и указать её вручную.'), [[(tr(uid, 'Дані відправника', 'Данные отправителя'), 'sender')]])
            else:
                put(uid, flow='parcel', order_id=int(parts[1]))
                await say(uid, tr(uid, 'Введіть вагу у кг та довжину, ширину, висоту у см через пробіл. Наприклад: 1 30 20 10. Наступне повідомлення створить реальну ТТН після перевірки даних відправника. /cancel — скасувати.', 'Введите вес в кг и длину, ширину, высоту в см через пробел. Например: 1 30 20 10. Следующее сообщение создаст реальную ТТН после проверки данных отправителя. /cancel — отменить.'))
        elif action == 'directory':
            s = state(uid)
            if s.get('flow') != 'order' or s.get('step') not in (2, 3):
                return True
            choice = s.pop('choices')[int(parts[1])]
            if s['step'] == 2:
                s['data'].update(city=choice['name'], city_ref=choice['ref'])
                s['step'] = 3
                await say(uid, tr(uid, 'Введіть номер чи назву відділення Нової пошти.', 'Введите номер или название отделения Новой почты.'))
            else:
                s['data'].update(branch=choice['name'], warehouse_ref=choice['ref'])
                await confirm_checkout(uid, s)
        elif action == 'ttn':
            put(uid, flow='ttn', order_id=int(parts[1]))
            await say(uid, tr(uid, 'Введіть реальний номер ТТН від перевізника.', 'Введите реальный номер ТТН от перевозчика.'))
        elif action == 'review':
            put(uid, flow='review', order_id=int(parts[1]))
            await say(uid, tr(uid, 'Надішліть оцінку 1–5 і відгук одним повідомленням: 5 Все добре.', 'Пришлите оценку 1–5 и отзыв одним сообщением: 5 Всё хорошо.'))
    except market.MarketError as e:
        await say(uid, esc(str(e)))
    except (ValueError, KeyError, IndexError):
        await say(uid, tr(uid, 'Ця дія вже недоступна. Відкрийте /market.', 'Это действие уже недоступно. Откройте /market.'))
    except Exception:
        logging.exception('Marketplace bot callback failed')
        await say(uid, tr(uid, 'Не вдалося виконати дію. Спробуйте ще раз.', 'Не удалось выполнить действие. Попробуйте ещё раз.'))
    return True


async def open_chat(uid, chat_id):
    data = await call(uid, f'/chats/{chat_id}/messages')
    for message in data.get('items', [])[-10:]:
        author = tr(uid, 'Ви', 'Вы') if message.get('sender', message.get('sender_id')) == uid else tr(uid, 'Співрозмовник', 'Собеседник')
        await say(uid, f"<b>{author}</b>\n{esc(message.get('body', message.get('text', '')))}")
    put(uid, flow='chat', chat_id=chat_id)
    await say(uid, tr(uid, 'Напишіть повідомлення. Історія доступна також у мініаппі. /market — вийти.',
                     'Напишите сообщение. История доступна также в миниаппе. /market — выйти.'),
              [[(tr(uid, 'Оновити', 'Обновить'), f'openchat:{chat_id}')]])


# ---- Domivka 4 entry point and shared business cabinet ----
REPLY_ACTIONS = {
    '🏡 Нерухомість': 'rent', '🏡 Недвижимость': 'rent', '🛍 Маркет': 'market',
    '📦 Мої покупки': 'orders', '📦 Мои покупки': 'orders',
    '＋ Продати': 'sell', '＋ Продать': 'sell', '🛒 Кошик': 'cart', '🛒 Корзина': 'cart',
    '✨ Клуб': 'club', '🤝 Друзі': 'friends', '🤝 Друзья': 'friends',
    '👤 Кабінет': 'account', '👤 Кабинет': 'account', '⚙️ Налаштування': 'settings', '⚙️ Настройки': 'settings',
    '🛡 Адмін': 'admin', '🛡 Админ': 'admin', '🏠 Головна': 'services', '🏠 Главная': 'services'
}


async def business_call(uid, path, data=None, method='GET'):
    import business
    try:
        return await business.dispatch(uid, path, data, method)
    except business.BusinessError as e:
        raise market.MarketError(str(e), e.status) from None


async def role(uid):
    try:
        import business
        value = business.role(uid)
        return value
    except (ImportError, AttributeError, RuntimeError):
        return 'owner' if uid in getattr(B, 'ADMINS', set()) else None


def is_market_start(argument):
    return argument in ('market', 'services', 'club', 'friends', 'cart') or argument.startswith(('market_', 'm_', 'ref_'))


async def start_screen(uid, argument=''):
    STATE.pop(uid, None)
    if hasattr(B, 'S'):
        B.S.pop(uid, None)
    if argument.startswith(('market_', 'm_')):
        lid = argument.partition('_')[2]
        if lid.isdigit():
            await listing(uid, (await call(uid, '/listings/' + lid))['item'], True)
            return
    if argument.startswith('ref_'):
        await business_call(uid, '/referral', {'code': argument[4:]}, 'POST')
    elif argument == 'market':
        return await home(uid)
    elif argument == 'club':
        return await club(uid)
    elif argument == 'friends':
        return await friends(uid)
    elif argument == 'cart':
        return await cart(uid)
    r = await role(uid)
    labels = [
        [('🏡 Нерухомість', '🏡 Недвижимость'), ('🛍 Маркет', '🛍 Маркет')],
        [('📦 Мої покупки', '📦 Мои покупки'), ('＋ Продати', '＋ Продать')],
        [('🛒 Кошик', '🛒 Корзина'), ('✨ Клуб', '✨ Клуб'), ('🤝 Друзі', '🤝 Друзья')],
        [('👤 Кабінет', '👤 Кабинет'), ('⚙️ Налаштування', '⚙️ Настройки')]
    ]
    if r:
        labels.append([('🛡 Адмін', '🛡 Админ')])
    keyboard = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=tr(uid, uk, ru)) for uk, ru in row] for row in labels],
                                   resize_keyboard=True, is_persistent=True,
                                   input_field_placeholder=tr(uid, 'Оберіть сервіс або відкрийте меню', 'Выберите сервис или откройте меню'))
    await B.bot.send_message(uid, tr(uid,
        '<b>Domivka 4</b>\n\nВаш дім. Ваші знахідки.\nОрендуйте житло або дайте речам нову історію — в одному місці.\n\n🏡 <b>Нерухомість</b> — квартири, будинки та приміщення.\n🛍 <b>Маркет</b> — речі від людей поруч.\n\nУсе доступно тут, у боті, та в мініаппі.',
        '<b>Domivka 4</b>\n\nВаш дом. Ваши находки.\nАрендуйте жильё или дайте вещам новую историю — в одном месте.\n\n🏡 <b>Недвижимость</b> — квартиры, дома и помещения.\n🛍 <b>Маркет</b> — вещи от людей рядом.\n\nВсё доступно здесь, в боте, и в миниаппе.'), reply_markup=keyboard)
    inline = [[InlineKeyboardButton(text=tr(uid, '🏡 Знайти нерухомість', '🏡 Найти недвижимость'), callback_data='mk:rent')],
              [InlineKeyboardButton(text=tr(uid, '🛍 Відкрити маркет', '🛍 Открыть маркет'), callback_data='mk:home')]]
    app_url = getattr(B, 'WEBAPP_URL', '')
    if app_url.startswith('https://'):
        inline.append([InlineKeyboardButton(text=tr(uid, 'Відкрити мініапп ↗', 'Открыть миниапп ↗'), web_app=WebAppInfo(url=app_url))])
    await B.bot.send_message(uid, tr(uid, 'З чого почнемо?', 'С чего начнём?'), reply_markup=InlineKeyboardMarkup(inline_keyboard=inline))


async def account(uid):
    STATE.pop(uid, None)
    p = (await business_call(uid, '/profile')).get('item', {})
    listings = (await call(uid, '/listings', {'mine': '1'})).get('items', [])
    all_orders = (await call(uid, '/orders')).get('items', [])
    purchases = sum(x.get('buyer_id') == uid for x in all_orders)
    sales = sum(x.get('seller_id') == uid for x in all_orders)
    await say(uid, tr(uid, '<b>Ваш кабінет</b>', '<b>Ваш кабинет</b>')
              + f"\n{tr(uid, 'Оголошення', 'Объявления')}: {len(listings)}"
              + f"\n{tr(uid, 'Покупки / продажі', 'Покупки / продажи')}: {purchases} / {sales}"
              + f"\n{tr(uid, 'Рівень клубу', 'Уровень клуба')}: {esc(p.get('level_name', p.get('level', '—')))}"
              + f"\n{tr(uid, 'Бали', 'Баллы')}: {esc(p.get('points', 0))}",
              [[(tr(uid, 'Мої оголошення', 'Мои объявления'), 'mine'), (tr(uid, 'Замовлення', 'Заказы'), 'orders')],
               [(tr(uid, 'Повідомлення', 'Сообщения'), 'chats'), (tr(uid, 'Обране', 'Избранное'), 'favs')],
               [(tr(uid, 'Дані відправника', 'Данные отправителя'), 'sender')],
               [(tr(uid, 'Клуб і корисні пропозиції', 'Клуб и полезные предложения'), 'club')],
               [(tr(uid, 'Головна', 'Главная'), 'services')]])


async def club(uid):
    STATE.pop(uid, None)
    p = (await business_call(uid, '/profile')).get('item', {})
    offer_data = await business_call(uid, '/offers')
    offers = offer_data.get('items', [])
    level = p.get('level_name', p.get('level', '—'))
    text = tr(uid, '<b>Клуб Domivka</b>', '<b>Клуб Domivka</b>') + f"\n{esc(level)} · {esc(p.get('points', 0))} {tr(uid, 'балів', 'баллов')}"
    perks = p.get('perks') or []
    if perks:
        text += '\n\n' + '\n'.join('• ' + esc(x.get('title', x.get('kind', ''))) + ' · ' + esc(x.get('until', '')) if isinstance(x, dict) else '• ' + esc(x) for x in perks[:8])
    text += tr(uid, '\n\nБали відкривають функції сервісу. Це не гроші та не обіцянка доходу. Щоденний бонус обмежений; запрошення зараховується після справжньої завершеної угоди друга.',
               '\n\nБаллы открывают функции сервиса. Это не деньги и не обещание дохода. Ежедневный бонус ограничен; приглашение засчитывается после настоящей завершённой сделки друга.')
    await say(uid, text, [[(tr(uid, 'Щоденна користь', 'Ежедневная польза'), 'daily'), (tr(uid, 'Мої підбірки', 'Мои подборки'), 'feed')],
                          [(tr(uid, 'Запросити друга', 'Пригласить друга'), 'friends')]])
    for offer in offers[:12]:
        oid = offer['id']
        cost = offer.get('point_cost', 0)
        stars = offer.get('price_stars', 0)
        rows = [[(tr(uid, 'Активувати за бали', 'Активировать за баллы'), f"redeem:{oid}:{secrets.token_hex(8)}")]] if cost else None
        if stars:
            rows = [[(tr(uid, 'Придбати за Stars', 'Купить за Stars'), f'buyperk:{oid}')]]
        price_text = f'{stars} Stars · {offer.get("duration_days", 0)} ' + tr(uid, 'днів, разова покупка', 'дней, разовая покупка') if stars else f'{cost} ' + tr(uid, 'балів', 'баллов')
        if offer.get('kind') == 'plan':
            price_text = f"{offer.get('price_uah', 0)} грн · {offer.get('duration_days', 0)} " + tr(uid, 'днів, інформаційний тариф', 'дней, информационный тариф')
        await say(uid, f"<b>{esc(offer.get('title'))}</b>\n{esc(offer.get('body'))}\n{esc(price_text)}" +
                  (tr(uid, '\nОплату карткою ще не підключено.', '\nОплата картой ещё не подключена.') if offer.get('price_uah') else ''), rows)


async def friends(uid):
    p = (await business_call(uid, '/profile')).get('item', {})
    code = str(p.get('referral_code', ''))
    username = getattr(B, 'BOT_USERNAME', '')
    from urllib.parse import quote
    link = f'https://t.me/{username}?start=ref_{quote(code, safe="")}' if username and code else ''
    text = tr(uid, '<b>Краще разом</b>\nЗапросіть друга до Domivka.', '<b>Лучше вместе</b>\nПригласите друга в Domivka.')
    text += f"\n\n{tr(uid, 'Код', 'Код')}: <code>{esc(code)}</code>"
    if link:
        text += '\n' + esc(link)
    text += f"\n\n{tr(uid, 'Зараховані запрошення', 'Засчитанные приглашения')}: {esc(p.get('qualified_referrals', 0))}"
    text += tr(uid, '\nБонус — лише після завершеної реальної угоди. Самозапрошення та повторне призначення друга не зараховуються.',
               '\nБонус — только после завершённой реальной сделки. Самоприглашения и повторное назначение друга не засчитываются.')
    await say(uid, text, [[(tr(uid, 'Ввести код друга', 'Ввести код друга'), 'referralcode')]])


async def cart_call(uid, path='/', data=None, method='GET'):
    import cart as cart_service
    return await cart_service.dispatch(uid, path, data, method)


async def cart(uid):
    STATE.pop(uid, None)
    data = await cart_call(uid)
    groups = data.get('groups', [])
    if not groups:
        return await say(uid, tr(uid, 'Кошик поки порожній. Додавайте речі з карток оголошень.', 'Корзина пока пуста. Добавляйте вещи из карточек объявлений.'),
                         [[(tr(uid, 'Переглянути маркет', 'Посмотреть маркет'), 'home')]])
    await say(uid, tr(uid, '<b>Ваш кошик</b>\nЗамовлення та доставка оформлюються окремо для кожного продавця. Речі резервуються лише після підтвердження покупки.',
                     '<b>Ваша корзина</b>\nЗаказ и доставка оформляются отдельно для каждого продавца. Вещи резервируются только после подтверждения покупки.'))
    for group in groups[:20]:
        seller_id = group['seller_id']
        lines = [f"<b>{esc(group.get('seller_name'))}</b>"]
        rows = []
        for item in group.get('items', [])[:30]:
            lines.append(f"• {esc(item.get('title'))} — {esc(item.get('price'))} грн")
            lid = item.get('listing_id', item.get('id'))
            rows.append([(tr(uid, 'Переглянути', 'Посмотреть') + f' #{lid}', f'item:{lid}'), (tr(uid, 'Прибрати', 'Убрать'), f'cartremove:{lid}')])
        lines.append(f"\n{tr(uid, 'Разом за товари', 'Всего за товары')}: {esc(group.get('subtotal', 0))} грн")
        rows.append([(tr(uid, 'Оформити у цього продавця', 'Оформить у этого продавца'), f'cartbuy:{seller_id}')])
        await say(uid, '\n'.join(lines), rows)


async def admin_home(uid):
    STATE.pop(uid, None)
    r = await role(uid)
    if not r:
        return await say(uid, tr(uid, 'Доступ лише адміністрації.', 'Доступ только администрации.'))
    data = (await business_call(uid, '/admin/dashboard')).get('item', {})
    text = tr(uid, '<b>Панель керування · Domivka 4</b>', '<b>Панель управления · Domivka 4</b>') + '\n' + esc(r)
    metrics = [('users', 'Користувачі', 'Пользователи'), ('market_listings', 'Оголошення маркету', 'Объявления маркета'),
               ('rental_listings', 'Об’єкти нерухомості', 'Объекты недвижимости'), ('completed_orders', 'Завершені угоди', 'Завершённые сделки'),
               ('confirmed_revenue', 'Підтверджені надходження', 'Подтверждённые поступления')]
    for key, uk, ru in metrics:
        if key in data:
            text += f'\n{tr(uid, uk, ru)}: {esc(data[key])}'
    market_stats = data.get('market', {})
    rental_stats = data.get('rentals', {})
    advertising = data.get('advertising', {})
    if market_stats:
        text += f"\n\n{tr(uid, 'Маркет: активні / завершені угоди', 'Маркет: активные / завершённые сделки')}: {esc(market_stats.get('active_listings', 0))} / {esc(market_stats.get('completed_orders', 0))}"
    if rental_stats:
        text += f"\n{tr(uid, 'Нерухомість: активні / проживання', 'Недвижимость: активные / проживания')}: {esc(rental_stats.get('active_listings', 0))} / {esc(rental_stats.get('completed_stays', 0))}"
    if advertising:
        text += f"\n{tr(uid, 'Реклама: узгоджено / звірено надходжень', 'Реклама: согласовано / сверено поступлений')}: {esc(advertising.get('booked_amount', 0))} / {esc(advertising.get('reconciled_receipts', 0))} грн"
    stars = data.get('stars', {})
    if stars:
        text += f"\nStars: {esc(stars.get('settled', 0))} {tr(uid, 'оплачено', 'оплачено')} · {esc(stars.get('refunded', 0))} {tr(uid, 'повернено', 'возвращено')}"
    text += tr(uid, '\n\nОборот угод не є доходом платформи. Оплата карткою не підключена.', '\n\nОборот сделок не является доходом платформы. Оплата картой не подключена.')
    rows = []
    if r in ('owner', 'admin', 'moderator'):
        rows.append([(tr(uid, 'Модерація двох сервісів', 'Модерация двух сервисов'), 'bizmoderate')])
    if r in ('owner', 'admin', 'marketing'):
        rows += [[(tr(uid, 'Кампанії', 'Кампании'), 'campaigns'), (tr(uid, 'Тарифи й бонуси', 'Тарифы и бонусы'), 'offers')]]
    if r == 'owner':
        rows.append([(tr(uid, 'Підтвердити угоду для запрошення', 'Подтвердить сделку для приглашения'), 'newform:qualify')])
        rows.append([(tr(uid, 'Повернення Stars', 'Возврат Stars'), 'newform:refund')])
        rows += [[(tr(uid, 'Адміністратори', 'Администраторы'), 'delegates')], [(tr(uid, 'Підтвердити надходження', 'Подтвердить поступление'), 'receipt')]]
    rows.append([(tr(uid, 'Головна', 'Главная'), 'services')])
    await say(uid, text, rows)


async def dual_moderation(uid):
    if await role(uid) not in ('owner', 'admin', 'moderator'):
        return await say(uid, tr(uid, 'Немає прав модератора.', 'Нет прав модератора.'))
    data = await business_call(uid, '/admin/moderation')
    item = data.get('item', data)
    # Both arrays stay separate in the service; explicit service IDs prevent cross-market edits.
    entries = list(item.get('items', [])) + list(item.get('pending', [])) if isinstance(item, dict) else []
    if isinstance(item, dict):
        for service, key in (('market', 'market'), ('rentals', 'rentals')):
            for x in item.get(key, []):
                entries.append(dict(x, service=service))
    if not entries:
        await say(uid, tr(uid, 'Черга модерації порожня.', 'Очередь модерации пуста.'))
        return
    for entry in entries[:40]:
        service = entry.get('service', 'market')
        lid = entry.get('listing_id', entry.get('id'))
        text = f"<b>{esc(service)} #{esc(lid)} · {esc(entry.get('title'))}</b>\n{esc(str(entry.get('reason', entry.get('descr', '')))[:2000])}"
        await say(uid, text, [[(tr(uid, 'Схвалити', 'Одобрить'), f'bizmod:{service}:{lid}:approve'),
                                    (tr(uid, 'Заблокувати', 'Заблокировать'), f'bizmod:{service}:{lid}:block')]])


FORM_DEFS = {
    'referral': ('/referral', [('code', 'Код друга', 'Код друга', 'text')]),
    'delegate': ('/admin/delegates', [('user_id', 'Telegram ID користувача', 'Telegram ID пользователя', 'id'), ('role', 'Роль: модератор або маркетинг', 'Роль: модератор или маркетинг', 'role')]),
    'campaign': ('/admin/campaigns', [
        ('title', 'Назва кампанії', 'Название кампании', 'text'),
        ('body', 'Текст пропозиції', 'Текст предложения', 'body'),
        ('service', 'Де показувати?', 'Где показывать?', 'service'),
        ('image_url', 'HTTPS-посилання на зображення або - без нього', 'HTTPS-ссылка на изображение или - без него', 'image'),
        ('target_url', 'Посилання: /market, /rentals або HTTPS', 'Ссылка: /market, /rentals или HTTPS', 'url'),
        ('starts_at', 'Початок у UTC, наприклад: 2026-12-01T09:00:00Z', 'Начало в UTC, например: 2026-12-01T09:00:00Z', 'date'),
        ('ends_at', 'Кінець у UTC, наприклад: 2026-12-31T18:00:00Z', 'Конец в UTC, например: 2026-12-31T18:00:00Z', 'date'),
        ('active', 'Увімкнути кампанію?', 'Включить кампанию?', 'bool'),
        ('max_views', 'Загальний ліміт показів: 1–1000000', 'Общий лимит показов: 1–1000000', 'views'),
        ('daily_cap', 'Максимум показів одній людині за день: 1–3', 'Максимум показов одному человеку в день: 1–3', 'cap'),
        ('booked_amount', 'Узгоджена сума в грн (це не підтверджує оплату)', 'Согласованная сумма в грн (это не подтверждает оплату)', 'int')]),
    'offer': ('/admin/offers', [
        ('title', 'Назва тарифу чи бонусу', 'Название тарифа или бонуса', 'text'),
        ('body', 'Що саме отримає користувач?', 'Что именно получит пользователь?', 'body'),
        ('kind', 'Тип переваги', 'Тип преимущества', 'kind'),
        ('point_cost', 'Вартість у балах: 0 — безкоштовно', 'Стоимость в баллах: 0 — бесплатно', 'int'),
        ('price_uah', 'Ціна в грн лише для інформаційного тарифу; для цифрових бонусів — 0', 'Цена в грн только для информационного тарифа; для цифровых бонусов — 0', 'int'),
        ('price_stars', 'Ціна у Telegram Stars для цифрового бонусу: 0 якщо обмін за бали; для тарифу — 0', 'Цена в Telegram Stars цифрового бонуса: 0 если обмен за баллы; для тарифа — 0', 'stars'),
        ('duration_days', 'Тривалість у днях: 1–365', 'Срок в днях: 1–365', 'days'),
        ('active', 'Показувати пропозицію?', 'Показывать предложение?', 'bool'),
        ('min_level', 'Мінімальний рівень користувача: 0 — всі', 'Минимальный уровень пользователя: 0 — все', 'level'),
        ('vip_only', 'Лише для активних VIP?', 'Только для активных VIP?', 'bool'),
        ('target_user_id', 'Персональний Telegram ID: 0 — для всіх відповідних користувачів', 'Персональный Telegram ID: 0 — для всех подходящих пользователей', 'optional_id')]),
    'receipt': ('/admin/receipts', [
        ('campaign_id', 'Номер кампанії', 'Номер кампании', 'id'),
        ('amount', 'Фактично отримана сума в грн', 'Фактически полученная сумма в грн', 'positive'),
        ('reference', 'Номер банківської операції чи документа (без даних картки)', 'Номер банковской операции или документа (без данных карты)', 'text')]),
    'alert': ('/alerts', [
        ('service', 'Для якого сервісу зберегти пошук?', 'Для какого сервиса сохранить поиск?', 'alertservice'),
        ('q', 'Що шукаєте? Або - для всіх', 'Что ищете? Или - для всех', 'optional'),
        ('city', 'Місто або - для всіх', 'Город или - для всех', 'optional'),
        ('max_price', 'Максимальна ціна у грн: 0 без обмеження', 'Максимальная цена в грн: 0 без ограничения', 'int')])
}


async def form_allowed(uid, kind):
    r = await role(uid)
    if kind in ('delegate', 'receipt', 'qualify', 'refund'):
        return r == 'owner'
    if kind in ('campaign', 'offer'):
        return r in ('owner', 'admin', 'marketing')
    return True


async def begin_business_form(uid, kind, initial=None):
    if kind not in FORM_DEFS or not await form_allowed(uid, kind):
        return await say(uid, tr(uid, 'Недостатньо прав для цієї дії.', 'Недостаточно прав для этого действия.'))
    put(uid, flow='business_form', kind=kind, step=0, data=dict(initial or {}))
    await prompt_business_form(uid, state(uid))


async def prompt_business_form(uid, s):
    _, fields = FORM_DEFS[s['kind']]
    if s['step'] >= len(fields):
        return await preview_business_form(uid, s)
    key, uk, ru, typ = fields[s['step']]
    value = s['data'].get(key)
    msg = '<b>' + tr(uid, uk, ru) + '</b>\n/cancel — ' + tr(uid, 'скасувати', 'отменить')
    if value is not None:
        msg += '\n' + esc(value) + '\n/keep'
    choices = {
        'service': [('market', 'Маркет', 'Маркет'), ('rentals', 'Нерухомість', 'Недвижимость'), ('all', 'Обидва', 'Оба')],
        'alertservice': [('market', 'Маркет', 'Маркет'), ('rentals', 'Нерухомість', 'Недвижимость')],
        'kind': [('alerts_extra', 'Більше збережених пошуків', 'Больше сохранённых поисков'), ('profile_accent', 'Акцент профілю', 'Акцент профиля'), ('ad_free', 'Без рекламних банерів', 'Без рекламных баннеров'), ('vip', 'VIP-профіль', 'VIP-профиль'), ('plan', 'Інформаційний тариф', 'Информационный тариф')],
        'role': [('moderator', 'Модератор', 'Модератор'), ('marketing', 'Маркетинг', 'Маркетинг')],
        'bool': [('yes', 'Так', 'Да'), ('no', 'Ні', 'Нет')]
    }.get(typ)
    rows = [[(tr(uid, a, b), 'formvalue:' + value)] for value, a, b in choices] if choices else None
    await say(uid, msg, rows)


async def preview_business_form(uid, s):
    s['step'] = len(FORM_DEFS[s['kind']][1])
    labels = {k: tr(uid, uk, ru) for k, uk, ru, _ in FORM_DEFS[s['kind']][1]}
    lines = [tr(uid, '<b>Перевірте перед збереженням</b>', '<b>Проверьте перед сохранением</b>')]
    for key, value in s['data'].items():
        if key != 'id':
            lines.append(esc(labels.get(key, key)) + ': ' + esc(str(value)[:800]))
    if s['kind'] == 'receipt':
        lines.append(tr(uid, '\nПідтверджуйте лише після перевірки реального надходження. Це створить фінансовий запис.', '\nПодтверждайте только после проверки реального поступления. Это создаст финансовую запись.'))
    if s['kind'] == 'qualify':
        lines.append(tr(uid, '\nПідтвердіть лише реальну завершену угоду. Це може активувати обмежений реферальний бонус, а не дохід платформи.', '\nПодтвердите только реальную завершённую сделку. Это может активировать ограниченный реферальный бонус, а не доход платформы.'))
    if s['kind'] == 'refund':
        lines.append(tr(uid, '\nПідтвердження виконає реальне повернення Stars покупцеві. Перевірте ID рахунку.', '\nПодтверждение выполнит реальный возврат Stars покупателю. Проверьте ID счёта.'))
    await say(uid, '\n'.join(lines), [[(tr(uid, 'Підтверджую та зберігаю', 'Подтверждаю и сохраняю'), 'formsave')], [(tr(uid, 'Змінити поля', 'Изменить поля'), 'formedit'), (tr(uid, 'Скасувати', 'Отменить'), 'cancel')]])


async def business_form_step(uid, text, s):
    if not await form_allowed(uid, s['kind']):
        STATE.pop(uid, None)
        return await say(uid, tr(uid, 'Доступ змінено. Дію скасовано.', 'Доступ изменён. Действие отменено.'))
    fields = FORM_DEFS[s['kind']][1]
    if s['step'] >= len(fields):
        return await say(uid, tr(uid, 'Перевірте форму та натисніть підтвердження.', 'Проверьте форму и нажмите подтверждение.'))
    key, _, _, typ = fields[s['step']]
    if text == '/keep' and key in s['data']:
        value = s['data'][key]
    else:
        value = text.strip()
        try:
            if typ in ('id', 'int', 'positive', 'cap', 'days', 'views', 'stars', 'optional_id', 'level'):
                if not re.fullmatch(r'\d{1,18}', value):
                    raise ValueError()
                value = int(value)
                bounds = {'id': (1, 2**63-1), 'int': (0, 100000000), 'positive': (1, 100000000), 'cap': (1, 3), 'days': (1, 365), 'views': (1, 1000000), 'stars': (0, 100000), 'optional_id': (0, 2**63-1), 'level': (0, 10000)}
                low, high = bounds[typ]
                if not low <= value <= high:
                    raise ValueError()
            elif typ == 'invoice':
                from uuid import UUID
                value = str(UUID(value))
            elif typ == 'bool':
                if value not in ('yes', 'no'):
                    raise ValueError()
                value = value == 'yes'
            elif typ in ('service', 'alertservice', 'kind', 'role'):
                allowed = {'service': ('market','rentals','all'), 'alertservice': ('market','rentals'), 'kind': ('alerts_extra','profile_accent','ad_free','vip','plan'), 'role': ('moderator','marketing')}
                if value not in allowed[typ]:
                    raise ValueError()
            elif typ == 'date':
                from datetime import datetime
                stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
                if stamp.tzinfo is None:
                    raise ValueError()
                value = stamp.isoformat()
            elif typ in ('image', 'url'):
                from urllib.parse import urlsplit
                if value == '-' and typ == 'image':
                    value = ''
                elif typ == 'url' and value in ('/market', '/rentals'):
                    pass
                else:
                    parsed = urlsplit(value)
                    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
                        raise ValueError()
            elif typ == 'optional':
                value = '' if value == '-' else value[:100]
            elif not 1 <= len(value) <= (2000 if typ == 'body' else 200):
                raise ValueError()
        except (ValueError, OverflowError):
            return await say(uid, tr(uid, 'Перевірте формат. Введіть значення ще раз.', 'Проверьте формат. Введите значение ещё раз.'))
    if key == 'title' and not 3 <= len(str(value)) <= 100:
        return await say(uid, tr(uid, 'Назва: 3–100 символів.', 'Название: 3–100 символов.'))
    if s['kind'] == 'campaign' and key == 'body' and len(str(value)) > 500:
        return await say(uid, tr(uid, 'Текст кампанії: до 500 символів.', 'Текст кампании: до 500 символов.'))
    if key == 'reference' and not 5 <= len(str(value)) <= 150:
        return await say(uid, tr(uid, 'Посилання на підтвердження: 5–150 символів.', 'Ссылка на подтверждение: 5–150 символов.'))
    s['data'][key] = value
    s['step'] += 1
    await prompt_business_form(uid, s)


async def admin_collection(uid, kind):
    if await role(uid) not in ('owner', 'admin', 'marketing'):
        return await say(uid, tr(uid, 'Немає прав керування маркетингом.', 'Нет прав управления маркетингом.'))
    path = '/admin/campaigns' if kind == 'campaign' else '/admin/offers'
    items = (await business_call(uid, path)).get('items', [])
    await say(uid, tr(uid, '<b>Кампанії</b>' if kind == 'campaign' else '<b>Тарифи й бонуси</b>', '<b>Кампании</b>' if kind == 'campaign' else '<b>Тарифы и бонусы</b>'),
              [[(tr(uid, 'Створити', 'Создать'), 'newform:' + kind)]])
    for item in items[:30]:
        title = f"#{item['id']} · {item.get('title', '')}"
        summary = esc(item.get('body', ''))
        summary += '\n' + tr(uid, 'Увімкнено' if item.get('active') else 'Вимкнено', 'Включено' if item.get('active') else 'Выключено')
        await say(uid, '<b>' + esc(title) + '</b>\n' + summary,
                  [[(tr(uid, 'Редагувати', 'Редактировать'), f"editform:{kind}:{item['id']}")]])


async def delegates(uid):
    if await role(uid) != 'owner':
        return await say(uid, tr(uid, 'Ролями керує лише власник сервісу.', 'Ролями управляет только владелец сервиса.'))
    items = (await business_call(uid, '/admin/delegates')).get('items', [])
    await say(uid, tr(uid, '<b>Доступ до керування</b>\nМодератор перевіряє оголошення. Маркетинг керує кампаніями та пропозиціями. Власник призначається в налаштуваннях сервера.',
                     '<b>Доступ к управлению</b>\nМодератор проверяет объявления. Маркетинг управляет кампаниями и предложениями. Владелец назначается в настройках сервера.'),
              [[(tr(uid, 'Додати адміністратора', 'Добавить администратора'), 'newform:delegate')]])
    for item in items[:40]:
        rows = None if item.get('immutable') else [[(tr(uid, 'Відкликати доступ', 'Отозвать доступ'), f"revoke:{item['user_id']}")]]
        await say(uid, f"<b>{esc(item.get('name'))}</b>\nID {esc(item['user_id'])} · {esc(item.get('role'))}", rows)


async def business_callback(uid, action, parts):
    if action in ('club', 'friends', 'account', 'admin', 'cart'):
        await {'club': club, 'friends': friends, 'account': account, 'admin': admin_home, 'cart': cart}[action](uid)
    elif action == 'daily':
        result = await business_call(uid, '/daily', {}, 'POST')
        item = result.get('item', result)
        await say(uid, tr(uid, 'Щоденну дію опрацьовано. Бонус за день нараховується один раз.', 'Ежедневное действие обработано. Бонус за день начисляется один раз.'))
        await club(uid)
    elif action == 'feed':
        data = await business_call(uid, '/feed')
        items = data.get('items', [])
        await say(uid, tr(uid, '<b>Ваші підбірки</b>' if items else 'Поки немає нових збігів. Збережіть пошук — ми покажемо відповідні оголошення.',
                         '<b>Ваши подборки</b>' if items else 'Пока нет новых совпадений. Сохраните поиск — мы покажем подходящие объявления.'),
                  [[(tr(uid, 'Зберегти пошук', 'Сохранить поиск'), 'newform:alert')]])
        for item in items[:20]:
            service = item.get('service', 'market')
            lid = item.get('listing_id', item.get('id'))
            callback = f'item:{lid}' if service == 'market' else f'rentitem:{lid}'
            await say(uid, f"<b>{esc(item.get('title'))}</b>\n{esc(item.get('city'))} · {esc(item.get('price', item.get('price_night', '')))} грн",
                      [[(tr(uid, 'Переглянути', 'Посмотреть'), callback)]])
    elif action == 'rentitem':
        await B.card(uid, uid, int(parts[1]))
    elif action == 'buyperk':
        import billing
        try:
            invoice = await billing.invoice(uid, int(parts[1]))
        except billing.BillingError as e:
            raise market.MarketError(str(e), e.status) from None
        text = tr(uid, '<b>Разова покупка цифрової переваги</b>', '<b>Разовая покупка цифрового преимущества</b>')
        text += f"\n{esc(invoice['amount'])} Stars · {esc(invoice.get('duration_days', 0))} {tr(uid, 'днів', 'дней')}"
        text += tr(uid, '\nБез автоматичного продовження. Перевага активується лише після підтвердження платежу Telegram.\nПідтримка: /paysupport', '\nБез автоматического продления. Преимущество активируется только после подтверждения платежа Telegram.\nПоддержка: /paysupport')
        text += '\nID: <code>' + esc(invoice['invoice_id']) + '</code>'
        await B.bot.send_message(uid, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=tr(uid, 'Відкрити рахунок Telegram', 'Открыть счёт Telegram'), url=invoice['url'])]]))
    elif action == 'redeem':
        await business_call(uid, '/redeem', {'offer_id': int(parts[1]), 'request_id': parts[2]}, 'POST')
        await say(uid, tr(uid, 'Перевагу активовано. Деталі — у клубі.', 'Преимущество активировано. Подробности — в клубе.'))
        await club(uid)
    elif action == 'referralcode':
        await begin_business_form(uid, 'referral')
    elif action == 'bizmoderate':
        await dual_moderation(uid)
    elif action == 'bizmod':
        if await role(uid) not in ('owner', 'admin', 'moderator'):
            await say(uid, tr(uid, 'Недостатньо прав.', 'Недостаточно прав.'))
        elif parts[1] in ('market', 'rentals') and parts[3] in ('approve', 'block'):
            await business_call(uid, '/admin/moderate', {'service': parts[1], 'listing_id': int(parts[2]), 'action': parts[3], 'reason': tr(uid, 'Рішення модератора через Telegram', 'Решение модератора через Telegram')}, 'POST')
            await say(uid, tr(uid, 'Рішення збережено.', 'Решение сохранено.'))
    elif action in ('campaigns', 'offers'):
        await admin_collection(uid, 'campaign' if action == 'campaigns' else 'offer')
    elif action == 'delegates':
        await delegates(uid)
    elif action == 'receipt':
        await begin_business_form(uid, 'receipt')
    elif action == 'newform':
        await begin_business_form(uid, parts[1])
    elif action == 'editform':
        kind = parts[1]
        if kind not in ('campaign', 'offer') or not await form_allowed(uid, kind):
            return True
        path = '/admin/campaigns' if kind == 'campaign' else '/admin/offers'
        items = (await business_call(uid, path)).get('items', [])
        item = next((x for x in items if x['id'] == int(parts[2])), None)
        if item:
            allowed = {x[0] for x in FORM_DEFS[kind][1]} | {'id'}
            await begin_business_form(uid, kind, {k: v for k, v in item.items() if k in allowed})
    elif action == 'formvalue':
        s = state(uid)
        if s.get('flow') == 'business_form':
            await business_form_step(uid, parts[1], s)
    elif action == 'formedit':
        s = state(uid)
        if s.get('flow') == 'business_form':
            s['step'] = 0
            await prompt_business_form(uid, s)
    elif action == 'formsave':
        s = state(uid)
        if s.get('flow') != 'business_form' or s['step'] != len(FORM_DEFS[s['kind']][1]):
            return True
        if not await form_allowed(uid, s['kind']):
            STATE.pop(uid, None)
            return True
        if s.get('saving'):
            return True
        s['saving'] = True
        data = dict(s['data'])
        if s['kind'] in ('receipt', 'qualify'):
            data['confirmed'] = True
        try:
            if s['kind'] == 'refund':
                await billing_refund(uid, data['invoice_id'])
            else:
                await business_call(uid, FORM_DEFS[s['kind']][0], data, 'POST')
        finally:
            s['saving'] = False
        STATE.pop(uid, None)
        await say(uid, tr(uid, 'Збережено.', 'Сохранено.'))
    elif action == 'revoke':
        if await role(uid) == 'owner':
            await business_call(uid, '/admin/delegates', {'user_id': int(parts[1]), 'role': 'none'}, 'POST')
            await delegates(uid)
        else:
            await say(uid, tr(uid, 'Лише власник може відкликати доступ.', 'Только владелец может отозвать доступ.'))
    elif action == 'cartadd':
        await cart_call(uid, '/items', {'listing_id': int(parts[1]), 'quantity': 1}, 'POST')
        await say(uid, tr(uid, 'Додано до кошика.', 'Добавлено в корзину.'), [[(tr(uid, 'Відкрити кошик', 'Открыть корзину'), 'cart')]])
    elif action == 'cartremove':
        await cart_call(uid, '/items/' + parts[1], {}, 'DELETE')
        await cart(uid)
    elif action == 'cartbuy':
        await say(uid, tr(uid, 'Оберіть доставку для товарів цього продавця.', 'Выберите доставку для товаров этого продавца.'),
                  [[('Нова пошта', f'cartdelivery:{parts[1]}:nova_poshta')], [('Укрпошта', f'cartdelivery:{parts[1]}:ukrposhta')],
                   [(tr(uid, 'Самовивіз', 'Самовывоз'), f'cartdelivery:{parts[1]}:pickup')]])
    elif action == 'cartdelivery':
        seller_id = int(parts[1])
        groups = (await cart_call(uid)).get('groups', [])
        group = next((x for x in groups if x['seller_id'] == seller_id), None)
        if not group or not group.get('items'):
            return True
        first = group['items'][0]
        put(uid, flow='order', step=0, data={'listing_id': first.get('listing_id', first.get('id')), 'checkout_seller_id': seller_id,
                                           'seller_id': seller_id, 'delivery': parts[2], 'expected_total': group['subtotal'], 'idempotency_key': secrets.token_hex(16)})
        await say(uid, tr(uid, 'Прізвище та ім’я одержувача. /cancel — скасувати.', 'Фамилия и имя получателя. /cancel — отменить.'))
    elif action == 'track':
        order = (await call(uid, '/orders/' + parts[1]))['item']
        if order.get('carrier') == 'nova':
            item = (await market.delivery(uid, '/delivery/track', {'order_id': int(parts[1])})).get('item', {})
            await say(uid, tr(uid, '<b>Статус перевізника</b>', '<b>Статус перевозчика</b>') + '\n' + esc(item.get('status'))
                      + '\n' + esc(item.get('scheduled_delivery_date', ''))
                      + tr(uid, '\nСтатус доставки не є підтвердженням оплати.', '\nСтатус доставки не является подтверждением оплаты.'))
        elif re.fullmatch(r'\d{13}', str(order.get('ttn', ''))):
            await say(uid, 'https://track.ukrposhta.ua/tracking_UA.html?barcode=' + str(order['ttn']))
    elif action == 'sender':
        put(uid, flow='seller_delivery', step=0, data={})
        await say(uid, tr(uid, '<b>Дані відправника</b>\nВкажіть прізвище та ім’я. API-ключ не потрібен. /cancel — скасувати.', '<b>Данные отправителя</b>\nУкажите фамилию и имя. API-ключ не нужен. /cancel — отменить.'))
    elif action == 'senderchoice':
        await sender_choice(uid, int(parts[1]))
    elif action == 'sendersave':
        s = state(uid)
        if s.get('flow') == 'seller_delivery' and s.get('step') == 4:
            await market.delivery(uid, '/delivery/settings', {'sender': s['data']}, 'POST')
            STATE.pop(uid, None)
            await say(uid, tr(uid, 'Дані відправника збережено. Право на створення накладних перевіряється сервісом окремо.', 'Данные отправителя сохранены. Право на создание накладных проверяется сервисом отдельно.'))
    else:
        return False
    return True


FORM_DEFS['qualify'] = ('/admin/qualify-order', [
    ('service', 'Сервіс завершеної угоди', 'Сервис завершённой сделки', 'alertservice'),
    ('order_id', 'Номер реально завершеного замовлення або проживання', 'Номер реально завершённого заказа или проживания', 'id'),
    ('reference', 'Документ чи посилання підтвердження (без платіжних секретів)', 'Документ или ссылка подтверждения (без платёжных секретов)', 'text')])


async def sender_step(uid, text, s):
    if s['step'] == 0:
        if not 3 <= len(text) <= 120 or len(text.split()) < 2:
            return await say(uid, tr(uid, 'Вкажіть прізвище та ім’я, наприклад: Шевченко Олена.', 'Укажите фамилию и имя, например: Шевченко Елена.'))
        s['data']['name'] = text
        s['step'] = 1
        return await say(uid, tr(uid, 'Телефон відправника: +380XXXXXXXXX.', 'Телефон отправителя: +380XXXXXXXXX.'))
    if s['step'] == 1:
        if not re.fullmatch(r'\+380\d{9}', text):
            return await say(uid, tr(uid, 'Формат телефону: +380XXXXXXXXX.', 'Формат телефона: +380XXXXXXXXX.'))
        s['data']['phone'] = text
        s['step'] = 2
        return await say(uid, tr(uid, 'Місто відправлення.', 'Город отправления.'))
    if s['step'] in (2, 3):
        path = '/delivery/cities' if s['step'] == 2 else '/delivery/warehouses'
        data = {'q': text[:80], 'seller_id': uid}
        if s['step'] == 3:
            data['city_ref'] = s['data']['city_ref']
        choices = (await market.delivery(uid, path, data)).get('items', [])[:20]
        if not choices:
            return await say(uid, tr(uid, 'Не знайдено. Уточніть назву міста чи номер відділення.', 'Не найдено. Уточните город или номер отделения.'))
        s['choices'] = choices
        await say(uid, tr(uid, 'Оберіть із довідника Нової пошти:', 'Выберите из справочника Новой почты:'),
                  [[(str(x.get('name', ''))[:55], f'senderchoice:{index}')] for index, x in enumerate(choices)])
    else:
        await say(uid, tr(uid, 'Перевірте дані та підтвердіть кнопкою.', 'Проверьте данные и подтвердите кнопкой.'))


async def sender_choice(uid, index):
    s = state(uid)
    if s.get('flow') != 'seller_delivery' or s.get('step') not in (2, 3):
        return
    choices = s.get('choices', [])
    if not 0 <= index < len(choices):
        return
    choice = choices[index]
    if s['step'] == 2:
        s['data']['city_ref'] = choice['ref']
        s['city_name'] = choice['name']
        s['step'] = 3
        s.pop('choices', None)
        await say(uid, tr(uid, 'Введіть номер чи назву відділення відправлення.', 'Введите номер или название отделения отправления.'))
    else:
        s['data']['warehouse_ref'] = choice['ref']
        s['step'] = 4
        await say(uid, tr(uid, '<b>Перевірте відправника</b>', '<b>Проверьте отправителя</b>')
                  + '\n' + esc(s['data']['name']) + '\n' + esc(s['data']['phone']) + '\n' + esc(s.get('city_name')) + '\n' + esc(choice['name']),
                  [[(tr(uid, 'Зберегти дані', 'Сохранить данные'), 'sendersave'), (tr(uid, 'Скасувати', 'Отменить'), 'cancel')]])


FORM_DEFS['refund'] = ('/billing/refund', [
    ('invoice_id', 'ID рахунку Stars з повідомлення про покупку (UUID)', 'ID счёта Stars из сообщения о покупке (UUID)', 'invoice')])


async def billing_refund(uid, invoice_id):
    if await role(uid) != 'owner':
        raise market.MarketError(tr(uid, 'Повернення оформлює лише власник.', 'Возврат оформляет только владелец.'), 403)
    import billing
    try:
        return await billing.refund(uid, invoice_id)
    except billing.BillingError as e:
        raise market.MarketError(str(e), e.status) from None
