"""Telegram-only marketplace flows backed by the same service as the Mini App.

Integration: setup(bot_module), then call on_message/on_callback before the
legacy catch-all handlers. Returning False leaves rental handling untouched.
"""
import html
import logging
import re
import secrets
import time

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

import db
import market

B = None
STATE = {}
LANG = {}
COMMANDS = {'market', 'rent', 'sell', 'import', 'orders', 'services', 'settings', 'moderate', 'cancel'}


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
    STATE.pop(uid, None)
    await say(uid, tr(uid, '<b>Domivka — оберіть сервіс</b>\nЖитло та речі від людей поруч.',
                     '<b>Domivka — выберите сервис</b>\nЖильё и вещи от людей рядом.'), [
        [(tr(uid, '🏡 Нерухомість', '🏡 Недвижимость'), 'rent')],
        [(tr(uid, '🛍 Маркет', '🛍 Маркет'), 'home')],
        [(tr(uid, '⚙️ Налаштування', '⚙️ Настройки'), 'settings')]])


async def home(uid, query='', offset=0):
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
    if status not in ('cancelled', 'completed', 'delivered'):
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
            return await say(uid, f"<b>{esc(data['title'])}</b>\n{esc(data['city'])} · {data['price']} грн\n{esc(data['description'])}\n"
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
            if command == 'market':
                await home(uid, text.partition(' ')[2].strip())
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
                await moderation(uid)
            else:
                await services(uid)
            return True
        if s.get('flow') == 'sell':
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
                      + '\n'.join(f"{esc(r.get('rating'))}/5 · {esc(r.get('text'))}" for r in seller.get('reviews', [])[:5]),
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
                result = await call(uid, '/orders', payload, 'POST')
                STATE.pop(uid, None)
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
            if not cfg['item']['connected']:
                await say(uid, tr(uid, 'Спочатку підключіть особистий кабінет Нової пошти в налаштуваннях мініаппа. API-ключі не надсилайте повідомленням у бот. Або створіть ТТН у кабінеті перевізника та вкажіть її вручну.', 'Сначала подключите личный кабинет Новой почты в настройках миниаппа. API-ключи не отправляйте сообщением в бот. Или создайте ТТН в кабинете перевозчика и укажите её вручную.'))
            else:
                put(uid, flow='parcel', order_id=int(parts[1]))
                await say(uid, tr(uid, 'Введіть вагу у кг та довжину, ширину, висоту у см через пробіл. Наприклад: 1 30 20 10. Наступне повідомлення створить реальну ТТН у вашому кабінеті. /cancel — скасувати.', 'Введите вес в кг и длину, ширину, высоту в см через пробел. Например: 1 30 20 10. Следующее сообщение создаст реальную ТТН в вашем кабинете. /cancel — отменить.'))
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
