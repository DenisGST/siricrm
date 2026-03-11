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
  const SPINNER_HTML = `
    <span class="import-spinner">
      <span class="import-spinner__wheel">
        <span></span><span></span><span></span><span></span>
        <span></span><span></span><span></span><span></span>
        <span></span><span></span><span></span><span></span>
      </span>
      <span class="import-spinner__text" id="import-status">Запуск...</span>
    </span>`;

  window.startImportHistory = function (clientId) {
    const btn = document.getElementById("btn-import-history");
    btn.disabled = true;
    btn.innerHTML = SPINNER_HTML;

    fetch(`/telegram/chat/${clientId}/import-history/`, {
      method: "POST",
      headers: { "X-CSRFToken": getCookie("csrftoken") },
    })
      .then((r) => r.json())
      .then((data) => {
        if (data.task_id) {
          pollImportStatus(data.task_id, clientId, 0);
        } else {
          resetImportBtn();
        }
      })
      .catch(resetImportBtn);
  };

  function pollImportStatus(taskId, clientId, attempt) {
    fetch(`/task-status/${taskId}/`)
      .then((r) => r.json())
      .then((data) => {
        const statusEl = document.getElementById("import-status");
        if (statusEl) {
          statusEl.textContent =
            data.total > 0
              ? `${data.current} / ${data.total}`
              : `Загрузка...`;
        }

        if (data.ready) {
          const btn = document.getElementById("btn-import-history");
          if (btn) {
            btn.innerHTML = `✅ Загружено ${data.current || ""}`;
            setTimeout(() => {
              resetImportBtn();
              htmx.ajax("GET", `/telegram/chat/${clientId}/`, {
                target: "#telegram-chat-panel-root",
                swap: "outerHTML",
              });
            }, 2000);
          }
        } else {
          setTimeout(() => pollImportStatus(taskId, clientId, attempt + 1), 2000);
        }
      })
      .catch(resetImportBtn);
  }

  function resetImportBtn() {
    const btn = document.getElementById("btn-import-history");
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = "🕓 История";
    }
  }
})();
