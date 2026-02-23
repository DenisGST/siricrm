# apps/files/ffmpeg_utils.py
import subprocess
import tempfile
import os
from pathlib import Path

def convert_to_ogg(input_file) -> str:
    """
    Принимает Django UploadedFile или файловый объект,
    сохраняет во временный файл и конвертирует в OGG/Opus.
    Возвращает путь к .ogg файлу.
    """
    # сохраняем исходный во временный файл
    tmp_dir = tempfile.gettempdir()
    orig_name = getattr(input_file, "name", "audio_input")
    orig_path = os.path.join(tmp_dir, orig_name)

    if hasattr(input_file, "chunks"):
        with open(orig_path, "wb") as f:
            for chunk in input_file.chunks():
                f.write(chunk)
    else:
        # file‑like
        with open(orig_path, "wb") as f:
            f.write(input_file.read())

    out_path = os.path.join(
        tmp_dir,
        Path(orig_name).stem + "_conv.ogg"
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-i", orig_path,
        "-c:a", "libopus",
        "-b:a", "64k",
        out_path,
    ]
    #subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.returncode != 0:
        # выведи в лог, чтобы увидеть, что ffmpeg не устраивает
        logger.error("ffmpeg error: %s", result.stderr)
        raise RuntimeError("ffmpeg convert_to_ogg failed")

    # исходник можно удалить
    try:
        os.remove(orig_path)
    except FileNotFoundError:
        pass

    return out_path
