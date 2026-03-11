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
      el.className = "fixed inset-0 z-[9999] flex items-center justify-center bg-black/30";
      el.innerHTML = `
        <div class="bg-white rounded-xl shadow-xl px-8 py-6 flex flex-col items-center gap-3">
          <div class="import-spinner__wheel" style="width:48px;height:48px;">
            <span></span><span></span><span></span><span></span>
            <span></span><span></span><span></span><span></span>
            <span></span><span></span><span></span><span></span>
          </div>
          <div class="text-sm text-gray-600 font-medium">Загрузка истории...</div>
          <div id="import-overlay-counter" class="text-xs text-gray-400"></div>
        </div>`;
      document.body.appendChild(el);
    }
    el.classList.remove("hidden");
  }

  function hideImportOverlay() {
    const el = document.getElementById("import-loading-toast");
    if (el) el.classList.add("hidden");
  }

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