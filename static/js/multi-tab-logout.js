// Multi-tab logout: при закрытии ПОСЛЕДНЕЙ вкладки приложения шлём
// POST /accounts/logout/ через sendBeacon. Если осталась хоть одна вкладка
// с тем же доменом — никаких действий.
//
// Этот скрипт ОБЯЗАН быть подключён на каждой странице приложения
// (dashboard.html, arbitr/_layout.html, devops/_layout.html и т.п.) —
// иначе:
//   1) страница без heartbeat'а не появится в sirius_tabs → при закрытии
//      её вкладки счётчик «живых» = 0 → лишний logout;
//   2) или наоборот — открытая страница не отправит logout при
//      реальном закрытии последней вкладки.
//
// Также: при переходе по <a href> внутри своего origin sendBeacon НЕ
// шлём — это просто навигация, не закрытие. См. sessionStorage флаг
// sirius_internal_nav (срабатывает на 5 сек после клика по внутренней
// ссылке + на любой form submit).

(function () {
    const KEY = "sirius_tabs";
    const NAV_KEY = "sirius_internal_nav";
    const HEARTBEAT_MS = 3000;
    const STALE_MS = 10000;
    const tabId = (crypto.randomUUID && crypto.randomUUID()) || String(Math.random());

    function readTabs() {
        try { return JSON.parse(localStorage.getItem(KEY) || "{}"); }
        catch (e) { return {}; }
    }
    function writeTabs(o) {
        try { localStorage.setItem(KEY, JSON.stringify(o)); } catch (e) {}
    }
    function heartbeat() {
        const tabs = readTabs();
        tabs[tabId] = Date.now();
        const cutoff = Date.now() - STALE_MS;
        for (const k of Object.keys(tabs)) if (tabs[k] < cutoff) delete tabs[k];
        writeTabs(tabs);
    }
    heartbeat();
    setInterval(heartbeat, HEARTBEAT_MS);

    // Маркер «внутренней навигации»: если кликнули по <a href> внутри
    // нашего origin, ИЛИ submit любой <form>, — выставляем флаг.
    // beforeunload это увидит и пропустит beacon.
    function markInternalNav() {
        try { sessionStorage.setItem(NAV_KEY, String(Date.now())); } catch (e) {}
    }

    document.addEventListener("click", function (e) {
        const a = e.target.closest && e.target.closest("a[href]");
        if (!a) return;
        const href = a.getAttribute("href") || "";
        // якоря и javascript: — не навигация
        if (!href || href.startsWith("#") || href.toLowerCase().startsWith("javascript:")) return;
        // явный target=_blank — новая вкладка, текущая остаётся → тоже не закрытие
        if (a.target === "_blank") return;
        // проверяем что переход в пределах своего origin
        try {
            const url = new URL(a.href, window.location.href);
            if (url.origin === window.location.origin) markInternalNav();
        } catch (e) { /* invalid URL — игнорируем */ }
    }, true);

    document.addEventListener("submit", function () { markInternalNav(); }, true);

    // F5 / Ctrl+R / Cmd+R — это reload, а не закрытие вкладки. beforeunload
    // не отличает одно от другого, поэтому ловим хоткей сами и ставим
    // маркер — иначе sendBeacon logout успевает дропнуть сессию между
    // unload и re-GET, и Django выдаёт 400 SessionInterrupted на reload.
    document.addEventListener("keydown", function (e) {
        if (e.key === "F5" || ((e.ctrlKey || e.metaKey) && (e.key === "r" || e.key === "R"))) {
            markInternalNav();
        }
    }, true);

    window.addEventListener("beforeunload", function () {
        const tabs = readTabs();
        delete tabs[tabId];
        const cutoff = Date.now() - STALE_MS;
        const alive = Object.values(tabs).filter(ts => ts > cutoff).length;
        writeTabs(tabs);
        if (alive > 0) return;

        // Внутренняя навигация в пределах 5 сек — не закрытие, пропускаем
        const navAt = parseInt(sessionStorage.getItem(NAV_KEY) || "0", 10);
        if (Date.now() - navAt < 5000) {
            try { sessionStorage.removeItem(NAV_KEY); } catch (e) {}
            return;
        }

        const csrf = (document.cookie.split('; ').find(r => r.startsWith('csrftoken=')) || '').split('=')[1] || '';
        const fd = new FormData();
        fd.append("csrfmiddlewaretoken", csrf);
        navigator.sendBeacon("/accounts/logout/", fd);
    });
})();
