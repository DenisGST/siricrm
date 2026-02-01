// CSRF для всех HTMX-запросов
(function () {
  function getCookie(name) {
    const value = `; ${document.cookie}`;
    const parts = value.split(`; ${name}=`);
    if (parts.length === 2) return parts.pop().split(";").shift();
  }

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

    // Ошибка ответа сервера (4xx/5xx)
    body.addEventListener("htmx:responseError", function () {
      showToast("Ошибка при запросе к серверу. Попробуйте позже.", "error");
    });

    // Ошибка сети / timeout
    body.addEventListener("htmx:sendError", function () {
      showToast("Проблема с сетью. Проверьте подключение.", "warning");
    });
  });
})();
