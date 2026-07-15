"""Аватар сотрудника: обработка загруженной картинки и заливка в S3.

Хранение — S3 (media-бакет, префикс `avatars/`), на модели держим только ключ
(`Employee.avatar_key`). Обычный ImageField не подходит: MEDIA_ROOT живёт внутри
контейнера и не смонтирован volume'ом — файл пропал бы при первом рестарте web.

Картинку нормализуем на входе: любой JPEG/PNG/WebP с телефона превращаем в
квадратный JPEG 256×256. Это разом решает вопросы веса (аватар грузится в
сайдбаре на каждой странице), EXIF-поворота и прозрачности PNG.
"""
import io
import logging

from PIL import Image, ImageOps, UnidentifiedImageError

from apps.files.s3_utils import delete_file_from_s3, upload_file_to_s3

logger = logging.getLogger(__name__)

# Больше — почти наверняка не аватар, а случайно выбранный файл.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
AVATAR_SIZE = 256
S3_PREFIX = "avatars"


class AvatarError(ValueError):
    """Понятная пользователю ошибка загрузки (показываем текст в форме)."""


def process_avatar(raw: bytes) -> bytes:
    """Байты загруженного файла → квадратный JPEG 256×256.

    Кидает AvatarError с текстом для пользователя, если это не картинка
    или она слишком большая.
    """
    if not raw:
        raise AvatarError("Файл пустой.")
    if len(raw) > MAX_UPLOAD_BYTES:
        mb = MAX_UPLOAD_BYTES // (1024 * 1024)
        raise AvatarError(f"Файл больше {mb} МБ — выберите изображение поменьше.")

    try:
        img = Image.open(io.BytesIO(raw))
        img.verify()                      # структурная проверка, портит объект
        img = Image.open(io.BytesIO(raw))  # поэтому открываем заново
    except (UnidentifiedImageError, OSError, ValueError):
        raise AvatarError("Не удалось прочитать изображение. Нужен JPG, PNG или WebP.")

    # Фото с телефона несут поворот в EXIF — без этого аватар ляжет боком.
    img = ImageOps.exif_transpose(img)
    # Прозрачность PNG/WebP при переводе в JPEG стала бы чёрной — кладём на белое.
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img)
    img = img.convert("RGB")
    # Обрезаем по центру в квадрат и ужимаем.
    img = ImageOps.fit(img, (AVATAR_SIZE, AVATAR_SIZE), method=Image.LANCZOS,
                       centering=(0.5, 0.5))

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85, optimize=True)
    return out.getvalue()


def set_avatar(employee, raw: bytes) -> str:
    """Обработать, залить в S3, проставить employee.avatar_key. Возвращает ключ.

    Старый аватар удаляем из S3 — иначе в бакете копятся мёртвые файлы.
    Сбой удаления не валит загрузку: новый аватар важнее подчистки старого.
    """
    from django.conf import settings

    data = process_avatar(raw)
    old_key = employee.avatar_key

    _bucket, key = upload_file_to_s3(
        data, prefix=S3_PREFIX,
        filename=f"avatar-{employee.pk}.jpg",
        content_type="image/jpeg",
    )
    employee.avatar_key = key
    employee.save(update_fields=["avatar_key"])

    if old_key and old_key != key:
        try:
            delete_file_from_s3(settings.AWS_STORAGE_BUCKET_NAME, old_key)
        except Exception:
            logger.warning("Не удалось удалить старый аватар %s", old_key, exc_info=True)
    return key


def clear_avatar(employee) -> None:
    """Убрать аватар (в сайдбаре снова буква имени) и удалить файл из S3."""
    from django.conf import settings

    old_key = employee.avatar_key
    if not old_key:
        return
    employee.avatar_key = ""
    employee.save(update_fields=["avatar_key"])
    try:
        delete_file_from_s3(settings.AWS_STORAGE_BUCKET_NAME, old_key)
    except Exception:
        logger.warning("Не удалось удалить аватар %s", old_key, exc_info=True)
