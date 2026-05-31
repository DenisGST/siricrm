"""Middleware: для прокси-пути /wa/file/ вычищаем все «лишние» заголовки.

WhatsApp Cloud (через 1msg.io) отвергает media upload, если в HTTP-ответе
есть Vary/Cookie/X-Frame-Options/HSTS/Content-Disposition/Cache-Control.
Тесты подтвердили: те же файлы по «голым» публичным URL (Adobe, Picsum)
доходят, наш прокси — нет, пока не убрать security-headers Django.

Стрипаем только на /wa/file/ — для остального сайта security-заголовки
по-прежнему ставятся стандартными мидлварями.
"""


class WAFileProxyHeaderStripMiddleware:
    _STRIP = (
        "Vary", "Set-Cookie",
        "X-Frame-Options", "X-Content-Type-Options",
        "Strict-Transport-Security",
        "Referrer-Policy",
        "Cross-Origin-Opener-Policy", "Cross-Origin-Embedder-Policy",
        "Cross-Origin-Resource-Policy",
        "Content-Disposition",
        "Cache-Control", "Expires", "Pragma",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.path.startswith("/wa/file/"):
            for h in self._STRIP:
                if h in response:
                    del response[h]
        return response
