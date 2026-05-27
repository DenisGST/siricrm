# Контейнер для парсера kad.arbitr.ru на Selenium + selenium-stealth.
# Запускается как celery worker очереди `arbitr` (см. CELERY_TASK_ROUTES).
#
# Почему не headless: kad детектит headless-Chromium (CDP-флаги, WebGL,
# поведенческие признаки). Headed-Chromium через Xvfb проходит проверки.

FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    # Дисплей Xvfb — общий для celery worker и для docker exec (kad_probe).
    DISPLAY=:99

# Системные зависимости:
#  - postgresql-client — стандартные нужны для Django/check
#  - xvfb              — виртуальный X-сервер (headed Chrome без графики)
#  - google-chrome     — устанавливается ниже отдельным шагом из репо Google
#  - fonts-*           — без них kad может выдавать капчу по рендер-признакам
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg unzip wget \
        postgresql-client \
        xvfb \
        fonts-liberation fonts-dejavu fonts-noto-color-emoji \
        libnss3 libxss1 libasound2 libxtst6 libatk1.0-0 \
        libatk-bridge2.0-0 libcups2 libdrm2 libgbm1 libgtk-3-0 \
        libpangocairo-1.0-0 libx11-xcb1 libxcomposite1 libxdamage1 \
        libxrandr2 \
    && rm -rf /var/lib/apt/lists/*

# Google Chrome stable из официального репо.
RUN wget -qO- https://dl.google.com/linux/linux_signing_key.pub \
        | gpg --dearmor -o /usr/share/keyrings/google-linux.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-linux.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends google-chrome-stable && \
    rm -rf /var/lib/apt/lists/*

# chromedriver под установленную мажор-версию Chrome (chrome-for-testing).
# Версия chromedriver ДОЛЖНА совпадать с major-номером Chrome,
# иначе selenium падает на старте.
RUN CHROME_MAJOR=$(google-chrome --version | grep -oE '[0-9]+' | head -n1) && \
    DRIVER_VERSION=$(curl -s "https://googlechromelabs.github.io/chrome-for-testing/LATEST_RELEASE_${CHROME_MAJOR}") && \
    echo "Chrome major=${CHROME_MAJOR} → chromedriver ${DRIVER_VERSION}" && \
    curl -fsSL "https://storage.googleapis.com/chrome-for-testing-public/${DRIVER_VERSION}/linux64/chromedriver-linux64.zip" \
        -o /tmp/chromedriver.zip && \
    unzip -q /tmp/chromedriver.zip -d /tmp/ && \
    mv /tmp/chromedriver-linux64/chromedriver /usr/local/bin/chromedriver && \
    chmod +x /usr/local/bin/chromedriver && \
    rm -rf /tmp/chromedriver*

# Python-зависимости проекта + selenium + selenium-stealth.
# selenium-stealth подделывает navigator/webgl-fingerprint глубже, чем
# playwright-stealth, — критично для kad.
COPY requirements.txt .
RUN pip install --upgrade pip setuptools wheel && \
    pip install -r requirements.txt && \
    pip install selenium==4.27.1 selenium-stealth==1.0.6

COPY . .

RUN mkdir -p /app/logs /app/staticfiles /app/media \
    && cp docker/arbitr/entrypoint.sh /usr/local/bin/arbitr-entrypoint.sh \
    && chmod +x /usr/local/bin/arbitr-entrypoint.sh

# entrypoint поднимает Xvfb :99 и exec'ит CMD (целевой процесс).
# Дисплей фиксированный — чтобы worker и docker exec ходили на один Xvfb.
ENTRYPOINT ["/usr/local/bin/arbitr-entrypoint.sh"]
CMD ["celery", "-A", "config", "worker", "-Q", "arbitr", "-l", "info", \
     "--concurrency=1", "--max-tasks-per-child=20"]
