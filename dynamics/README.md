# T50 · Дәлел в Dynamics 365

Рабочий локальный iframe-host и инструкция для model-driven app. Это пример интеграции протокола, **не выполненное размещение в Dynamics 365**. Доступ к tenant, публикация solution, Entra ID и права на CRM-записи в этой работе не проверялись.

## Запуск без изменения приложения

Из корня checkout (Python 3, зависимости не нужны):

```sh
python3 dynamics/build_demo.py --output /tmp/dalel-t50-demo
python3 -m http.server 8050 --bind 127.0.0.1 --directory /tmp/dalel-t50-demo
```

Открыть [локальный пример](http://127.0.0.1:8050/dynamics/embed-demo.html). По умолчанию используются встроенные синтетические диалоги; модель и API не вызываются. Пример открывает `demo-ru-001`, позволяет выбрать роль и другой ID, показывает полученные события. Очистка ID при уже открытом диалоге не закрывает его: в текущем протоколе нет команды «закрыть диалог».

Сборщик создаёт **новую** папку и отказывается перезаписывать существующую. Он копирует только `web/index.html`, `web/app.css`, непосредственные `.js`/`.css` из `web/js` и `web/css`, а также файлы этого примера. Не копирует `web/mocks`, реальные данные, отчёты, `.env`, Git и Python backend. В копии HTML сборщик меняет только два пути импортов bootstrap с `./dynamics/` на `../dynamics/`; единственный conditional bootstrap остаётся из основного index. Исходники `web/index.html` и `core.js` не меняются. Серверу отдаётся только каталог сборки, не корень репозитория.

Для проверки разных origin:

```sh
python3 dynamics/build_demo.py --output /tmp/dalel-t50-cross \
  --frame-url http://127.0.0.1:8051/web/index.html
python3 -m http.server 8050 --bind 127.0.0.1 --directory /tmp/dalel-t50-cross
# В другом терминале:
python3 -m http.server 8051 --bind 127.0.0.1 --directory /tmp/dalel-t50-cross
```

Host остаётся на `8050`, приложение загружается с `8051`. `localhost` и `127.0.0.1`, а также разные порты — разные origin. Allowlist должен совпадать точно.

Для локального API используйте новую папку и `--mode live --api-base http://127.0.0.1:8047/api --conv-id ID_ИЗ_API`. Сам API запускается отдельно. Режим `live` означает обращение к серверу; если сервер использует mock provider, это не реальная модель.

## Точная интеграция для main

1. Смонтировать `state.web_dir.parent / "dynamics"` в `/dynamics` через `ProductStaticFiles` **перед** корневым mount `/`. Тест этого mount принадлежит main.
2. Только когда в URL есть `data`, выполнить bootstrap **до** вызова `core.boot()`. Обычный URL приложения не должен требовать конфигурацию встраивания:

```js
// В существующей async-цепочке запуска index.html, после базового CQA_CONFIG:
if (new URLSearchParams(location.search).has('data')) {
  const [{ applyBootstrap }, { deployment }] = await Promise.all([
    import('./dynamics/bootstrap.js'),
    import('./dynamics/deployment.js'),
  ]);
  applyBootstrap(window, deployment);
}
const { boot } = await import('./js/core.js');
await boot();
```

При ошибке bootstrap показать ошибку и **не вызывать `boot()`**. Не продолжать с default API или mock fallback. В сборщике это уже реализовано.

3. Исходный `deployment.js`: `framePath: '../'` для приложения в `/`; локальные parent origins `http://127.0.0.1:8000`, `http://localhost:8000`, `http://127.0.0.1:8047` и `http://127.0.0.1:8050`. На 8000/8047 host выбирает API своего origin из allowlist. Для другого origin изменить этот доверенный файл или собрать пакет с `--parent-origin`. Параметр допускается повторять. `--api-base` тоже допускается повторять; host показывает только разрешённые адреса. Перед production замените локальные значения точными адресами окружения.
4. **Правки `core.js` и поле initial-conversation не нужны.** После `ready` host отправляет существующие `setRole`, затем `open`. На повторный `ready` (например, после повторного создания iframe) контекст отправляется снова. Main не должен добавлять ещё один `conv:open`, иначе будет двойная загрузка.

Эта проводка уже добавлена main в исходный `index.html`. Сборщик ожидает её наличие и останавливается при изменении контракта, вместо добавления второго bootstrap. После запуска основного API можно открыть [host на 8000](http://127.0.0.1:8000/dynamics/embed-demo.html); это тот же интерфейс со встроенными демо-данными. Автономный пакет сохраняет структуру `/web/` и потому использует другие относительные пути.

## Bootstrap и граница доверия

Файл `deployment.js` — доверенная конфигурация окружения, без секретов. `mock` задаётся в нём, а не произвольным query-параметром. `apiBase` принимается только при полном совпадении с разрешённым адресом, включая путь. HTTPS обязателен вне loopback; URL с credentials/query/fragment отклоняются. `parentOrigin` — точный origin без пути, wildcard и завершающего `/`.

В URL iframe передаётся один пользовательский параметр `data` с тремя полями:

```js
const url = new URL('https://cqa.example.test/web/index.html');
url.searchParams.set('data', new URLSearchParams({
  parentOrigin: 'https://org.crm4.dynamics.com',
  apiBase: 'https://cqa.example.test/api',
  role: 'operator',
}).toString());
```

Используйте `buildFrameUrl()` вместо ручной конкатенации. Каждый уровень URL-кодирования разбирается ровно один раз. Дубликаты, неизвестные вложенные поля и адреса вне allowlist отклоняются. `convId` хранится в host-контексте и передаётся после `ready`, чтобы не помещать ID записи в URL. Не передавайте в query access token, текст разговора, email или комментарий апелляции.

`role` принимает `operator` или `reviewer` и меняет только интерфейс. **Это не аутентификация и не авторизация.** Полномочия проверяются сервером независимо от роли в UI, ID в сообщении и выбранной CRM-записи.

## Контракт сообщений

Все вызовы `postMessage` используют точный `targetOrigin`. Получатель проверяет одновременно `event.source`, `event.origin`, namespace, тип, структуру и значения. Даже соседний iframe с тем же origin не считается доверенным приложением.

| Направление | source | type | payload |
| --- | --- | --- | --- |
| Host → приложение | `dalel-host` | `setRole` | `{role: 'operator' \| 'reviewer'}` |
| Host → приложение | `dalel-host` | `open` | `{id: 'conversation-id'}` |
| Приложение → host | `dalel-cqa` | `ready` | `{role, mock}` |
| Приложение → host | `dalel-cqa` | `resize` | `{height: number}` |
| Приложение → host | `dalel-cqa` | `score.updated` | `{conv_id, total: number \| null}` |
| Приложение → host | `dalel-cqa` | `appeal.created` | `{id, conv_id}` |
| Приложение → host | `dalel-cqa` | `decision.saved` | `{id, appeal_id}` |

`bridge.js` принимает только известные поля. Невалидные сообщения молча игнорируются; сырые payload не попадают в журнал. Высота должна быть конечным неотрицательным числом; host округляет вверх и ограничивает её диапазоном **360–1400 px**, без анимации размеров. Журнал хранит последние 12 событий в DOM, не использует localStorage. ID ограничен 200 символами; поддержаны буквы, цифры, `_ . : @ / -`. Числовой ID надо явно преобразовать в строку у отправителя.

`ready` подтверждает загрузку UI, а не доступность API или качество модели. У `open` нет ответа-подтверждения; host пишет «команда передана», а наличие диалога проверяется в приложении. `score.updated` и другие события — уведомления, не доверенный источник для записи баллов в CRM: сервер должен повторно проверить данные и полномочия.

## Размещение в model-driven app

Есть два варианта: HTTPS-сайт в IFRAME-контроле либо HTML/JS/CSS web resources внутри solution. Microsoft описывает обмен cross-origin через `postMessage`, строгие проверки origin/source и ограничения iframe. Для долгосрочного интерактивного компонента Microsoft рекомендует рассмотреть PCF; здесь выполнен требуемый iframe-прототип. [Microsoft Learn: IFRAME controls](https://learn.microsoft.com/en-us/power-apps/developer/model-driven-apps/use-iframe-and-web-resource-controls-on-a-form).

Для web resources сохраните общую виртуальную структуру, например:

```text
new_/web/index.html
new_/web/app.css
new_/web/js/*.js
new_/web/css/*.css
new_/dynamics/bootstrap.js
new_/dynamics/bridge.js
new_/dynamics/deployment.js
# Для демонстрационного host также:
new_/dynamics/embed-demo.html
new_/dynamics/embed-demo.js
new_/dynamics/embed-demo.css
```

Загрузите HTML как webpage, JS как script, CSS как stylesheet; включите все файлы в solution и опубликуйте. Relative imports/links сохраняют virtual-folder структуру под publisher prefix; ссылки между файлами автоматически не создают solution dependencies. Не переносите Python, SQLite или данные в web resources: это статические клиентские файлы. Используйте копию index из сборщика либо интеграцию main. Для web resources host нужен `framePath: '../web/index.html'`. [Microsoft Learn: HTML web resources](https://learn.microsoft.com/en-us/power-apps/developer/model-driven-apps/webpage-html-web-resources).

У HTML web resources есть ограниченный набор стандартных query-параметров. Пользовательские `apiBase` и `role` здесь упакованы в `data`; не добавляйте их отдельными параметрами к `/WebResources/...`. [Microsoft Learn: data parameter](https://learn.microsoft.com/en-us/power-apps/developer/model-driven-apps/sample-pass-multiple-values-web-resource-through-data-parameter).

В форме используйте `executionContext.getFormContext()`, настройте IFRAME/Web Resource control и получите его окно через `control.getContentWindow()`. Установите слушатель host **до** навигации `control.setSrc(url.href)`, чтобы не пропустить `ready`. Код host может использовать `createHostBridge({eventTarget: window, getChildWindow: () => contentWindow, childOrigin, role, convId, onEvent})`. При повторном Form OnLoad/TabStateChange вызовите `dispose()` старого bridge и подключите новый: control может быть уничтожен и создан повторно. `convId` берите из отдельного поля с сопоставлением CQA conversation ID; GUID CRM-записи автоматически не является ID разговора. Высоту реального form control задавайте через дизайнер формы, не через неподдерживаемую правку DOM Dynamics. [Microsoft Learn: getContentWindow](https://learn.microsoft.com/en-us/power-apps/developer/model-driven-apps/clientapi/reference/controls/getcontentwindow).

Для внешнего сайта настройте `Content-Security-Policy: frame-ancestors` на все ожидаемые origins в цепочке предков Dynamics. Конфликтующий `X-Frame-Options: DENY`/`SAMEORIGIN` заблокирует cross-origin embed. Настройка form «Restrict cross-frame scripting» применяет sandbox-ограничения и может блокировать скрипты и сообщения; задавайте её осознанно для доверенного приложения. Не передавайте `Xrm`/form context через сообщения. [Microsoft Learn: iframe security and messaging](https://learn.microsoft.com/en-us/power-apps/developer/model-driven-apps/use-iframe-and-web-resource-controls-on-a-form).

## API, CORS и вход

Текущий FastAPI разрешает CORS для localhost, а клиент использует `credentials: 'same-origin'` и не добавляет bearer token. Для production потребуется отдельная реализация входа и серверной проверки прав. Сам этот пакет её не добавляет.

Предпочтительный для текущего клиента вариант — приложение и защищённый backend-for-frontend под одним HTTPS-origin; `apiBase` указывает на его `/api`. Если API имеет другой origin, разрешите **origin iframe**, а не только верхней CRM-формы, разрешённые методы и Content-Type/Authorization с обработкой OPTIONS. Для bearer auth понадобится изменение API-клиента main: токен нужной audience/scopes, безопасное получение и обновление. При cookie auth придётся отдельно решить credentials, CSRF и ограничения сторонних cookies. Не заменяйте это `allow_origins=['*']`. CORS управляет браузерным доступом, а не правами пользователя. [Microsoft Learn: CORS](https://learn.microsoft.com/en-us/aspnet/core/security/cors?view=aspnetcore-8.0).

Встроенная аутентификация web resources относится к **Dataverse Web API**. Она не авторизует наш отдельный FastAPI. При прямой работе внешней SPA с Dataverse Microsoft документирует OAuth/MSAL и регистрацию в Entra ID; секреты серверного приложения не должны попадать в JS. В данном примере нет вызовов Dataverse и переноса токенов. [Dataverse authentication](https://learn.microsoft.com/en-us/power-apps/developer/data-platform/webapi/authenticate-web-api), [OAuth/CORS for SPA](https://learn.microsoft.com/en-us/power-apps/developer/data-platform/oauth-cross-origin-resource-sharing-connect-single-page-application).

## Проверка перед tenant-пилотом

```sh
node --test tests/dynamics*.test.mjs
```

Unit-тесты проверяют forged source/origin, неверные поля, отсутствие текста/секретов в протоколе, ограничение resize, lifecycle, точную API allowlist и URL-кодирование. Build-тест проверяет отдельный статический пакет и отсутствие закрытых файлов. Для реального browser-теста см. комментарий в `tests/dynamics_browser.test.mjs`.

Локально 07.11.2026 подтверждены 10 unit/build-тестов и отдельный Chromium-тест с host `8050` / iframe `8051`; финальная сборка после интеграции main повторно проверена на `8052` / `8053`. Проверены `ready → open`, смена роли и диалога, отклонение поддельного источника и лишнего `transcript`, предел высоты, отсутствие overflow на 375/768/1024/1440 px, targets ≥44 px и reduced motion. Проверенные пары текст/фон host имеют контраст не ниже 6.02:1. Browser-тест намеренно эмулирует `score:updated` на bus для проверки транспорта; это не сохранённая оценка модели. Проверка не является полным аудитом доступности или подтверждением tenant-развёртывания.

В tenant отдельно проверить загрузку всех относительных assets, вход и отказ без прав, нужную CRM-запись, повторное открытие вкладки, `ready → open`, смену роли, resize/layout, события score/appeal/decision и отсутствие transcript/token в сообщениях. Выполнение этих проверок в Dynamics 365 пока **не подтверждено**.

Официальные источники проверены 7 ноября 2026; рекомендации по нашему FastAPI и bootstrap основаны также на текущем коде репозитория.
