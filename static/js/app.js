// CSRF-утилита — глобальная функция
function getCookie(name) {
  const value = `; ${document.cookie}`;
  const parts = value.split(`; ${name}=`);
  if (parts.length === 2) return parts.pop().split(";").shift();
}

// CSRF для всех HTMX-запросов
(function () {
  const csrftoken = getCookie("csrftoken");

  document.addEventListener("DOMContentLoaded", function () {
    const body = document.body;
    if (!body) return;

    body.addEventListener("htmx:configRequest", function (event) {
      if (csrftoken) {
        event.detail.headers["X-CSRFToken"] = csrftoken;
      }
    });
  });
})();

// Простой обработчик ошибок HTMX
(function () {
  function showToast(message, type) {
    const container =
      document.getElementById("toast-container") ||
      (function () {
        const el = document.createElement("div");
        el.id = "toast-container";
        el.className = "fixed bottom-4 right-4 z-50 space-y-2";
        document.body.appendChild(el);
        return el;
      })();

    const toast = document.createElement("div");
    toast.className = "alert alert-" + (type || "error") + " shadow-lg w-80";
    toast.innerHTML = `<span>${message}</span>`;
    container.appendChild(toast);
    setTimeout(() => toast.remove(), 4000);
  }

  document.addEventListener("DOMContentLoaded", function () {
    const body = document.body;
    if (!body) return;

    body.addEventListener("htmx:responseError", function () {
      showToast("Ошибка при запросе к серверу. Попробуйте позже.", "error");
    });

    body.addEventListener("htmx:sendError", function () {
      showToast("Проблема с сетью. Проверьте подключение.", "warning");
    });
  });
})();

// ── Импорт истории Telegram ──
(function () {

  function showImportOverlay() {
    let el = document.getElementById("import-loading-toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "import-loading-toast";
      el.style.cssText = "position:fixed;inset:0;z-index:100000;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,0.4);";
      el.innerHTML = `
        <div style="background:white;border-radius:16px;padding:32px 40px;display:flex;flex-direction:column;align-items:center;gap:12px;box-shadow:0 20px 60px rgba(0,0,0,0.3);">
          <div class="import-spinner__wheel" style="width:48px;height:48px;">
            <span></span><span></span><span></span><span></span>
            <span></span><span></span><span></span><span></span>
            <span></span><span></span><span></span><span></span>
          </div>
          <div style="font-size:14px;color:#4b5563;font-weight:500;">Загрузка истории...</div>
          <div id="import-overlay-counter" style="font-size:12px;color:#9ca3af;"></div>
        </div>`;
      document.body.appendChild(el);
    }
    el.style.display = "flex";
  }

  function hideImportOverlay() {
    const el = document.getElementById("import-loading-toast");
    if (el) el.style.display = "none";
  }

  window.startImportHistory = function (clientId) {
    const btn = document.getElementById("btn-import-history");
    if (btn) {
      btn.disabled = true;
      btn.innerHTML = "⏳ Загрузка...";
    }

    showImportOverlay();

    fetch(`/telegram/chat/${clientId}/import-history/`, {
      method: "POST",
      headers: { "X-CSRFToken": getCookie("csrftoken") },
    })
      .then((r) => r.json())
      .then((data) => {
        if (data.task_id) {
          pollImportStatus(data.task_id, clientId, 0);
        } else {
          hideImportOverlay();
          resetImportBtn();
        }
      })
      .catch(() => { hideImportOverlay(); resetImportBtn(); });
  };

  function pollImportStatus(taskId, clientId, attempt) {
    fetch(`/task-status/${taskId}/`)
      .then((r) => r.json())
      .then((data) => {
        const counter = document.getElementById("import-overlay-counter");
        if (counter) {
          counter.textContent = data.total > 0
            ? `${data.current} / ${data.total}`
            : "Загрузка...";
        }

        if (data.ready) {
          hideImportOverlay();
          resetImportBtn();
          const btn = document.getElementById("btn-import-history");
          if (btn) btn.innerHTML = `✅ Загружено ${data.current || ""}`;
          setTimeout(() => {
            resetImportBtn();
            htmx.ajax("GET", `/telegram/chat/${clientId}/`, {
              target: "#telegram-chat-panel-root",
              swap: "outerHTML",
            });
          }, 2000);
        } else {
          setTimeout(() => pollImportStatus(taskId, clientId, attempt + 1), 2000);
        }
      })
      .catch(() => { hideImportOverlay(); resetImportBtn(); });
  }

  function resetImportBtn() {
    const btn = document.getElementById("btn-import-history");
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = "🕓 История";
    }
  }
})();
