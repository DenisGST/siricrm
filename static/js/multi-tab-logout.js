// Multi-tab logout (grace-версия).
//
// Задача: завершать сессию, когда пользователь ЗАКРЫЛ приложение (последнюю
// вкладку), но НЕ выкидывать его при reload / внутренней навигации / закрытии
// одной из нескольких вкладок.
//
// Почему так, а не sendBeacon прямо в beforeunload (как было раньше):
//   beforeunload НЕ отличает закрытие вкладки от reload/навигации. Старый код
//   слал sendBeacon('/accounts/logout/') на любом unload последней вкладки, если
//   не успевала выставиться метка «внутренняя навигация» (а на JS-reload,
//   HX-Redirect, back/forward, www↔без-www она не выставлялась). Итог — десятки
//   ложных авто-логаутов в день: юзера выкидывало на ровном месте, страница
//   уходила в цикл logout→login→тяжёлая перезагрузка.
//
// Grace-схема (чисто клиентская):
//   1. heartbeat: каждая вкладка раз в 3с пишет своё «я жива» в localStorage.
//   2. beforeunload: вкладка УДАЛЯЕТ свой heartbeat и СТАВИТ метку «намерение
//      закрытия» (sirius_pending_logout = ts). Логаут НЕ шлём.
//   3. Загрузка ЛЮБОЙ страницы приложения сразу СНИМАЕТ метку — значит предыдущий
//      unload был reload/навигацией, а не закрытием → логаут не нужен.
//   4. Живая вкладка раз в 2с: если метка висит дольше grace и есть живые
//      вкладки (приложение открыто — закрыли одну из нескольких) — снимает метку.
//   5. Реальное закрытие ПОСЛЕДНЕЙ вкладки: метка остаётся, но JS уже не работает
//      и активно логаут никто не шлёт — сессию подчистит idle-таймаут (10 мин).
//      Это осознанный компромисс ради нуля ложных логаутов.
//
// Скрипт ОБЯЗАН быть подключён на каждой странице приложения (dashboard.html,
// arbitr/_layout.html, devops/_layout.html) — иначе загрузка такой страницы не
// снимет метку (п.3) и heartbeat не будет учитываться (п.4).

(function () {
    const KEY = "sirius_tabs";
    const PENDING = "sirius_pending_logout";
    const HEARTBEAT_MS = 3000;
    const STALE_MS = 10000;   // heartbeat старше — вкладка считается мёртвой
    const GRACE_MS = 5000;    // окно «это был reload/навигация, не закрытие»
    const tabId = (crypto.randomUUID && crypto.randomUUID()) || String(Math.random());

    function readTabs() {
        try { return JSON.parse(localStorage.getItem(KEY) || "{}"); }
        catch (e) { return {}; }
    }
    function writeTabs(o) {
        try { localStorage.setItem(KEY, JSON.stringify(o)); } catch (e) {}
    }
    function aliveCount() {
        const tabs = readTabs();
        const cutoff = Date.now() - STALE_MS;
        return Object.values(tabs).filter(ts => ts > cutoff).length;
    }
    function clearPending() {
        try { localStorage.removeItem(PENDING); } catch (e) {}
    }

    // (3) Эта страница загрузилась → предыдущий unload этой вкладки был
    // reload/навигацией, а не закрытием. Снимаем метку «намерение закрытия».
    clearPending();

    function heartbeat() {
        const tabs = readTabs();
        tabs[tabId] = Date.now();
        const cutoff = Date.now() - STALE_MS;
        for (const k of Object.keys(tabs)) if (tabs[k] < cutoff) delete tabs[k];
        writeTabs(tabs);
    }
    heartbeat();
    setInterval(heartbeat, HEARTBEAT_MS);

    // (4) Живая вкладка: если метка висит дольше grace и приложение всё ещё
    // открыто (есть живые вкладки — закрыли одну из нескольких ИЛИ это была наша
    // вкладка после reload, которую п.3 не успел снять) — снимаем метку.
    setInterval(function () {
        const pending = parseInt(localStorage.getItem(PENDING) || "0", 10);
        if (!pending) return;
        if (Date.now() - pending < GRACE_MS) return;
        if (aliveCount() > 0) clearPending();
    }, 2000);

    // (2) При уходе со страницы только помечаем намерение закрытия. Логаут
    // активно не шлём — это и убирало ложные разлогины на reload/навигации.
    window.addEventListener("beforeunload", function () {
        const tabs = readTabs();
        delete tabs[tabId];
        writeTabs(tabs);
        try { localStorage.setItem(PENDING, String(Date.now())); } catch (e) {}
    });
})();
