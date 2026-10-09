"""Bounded, local OCR for cashier ticket photos. OCR text is never model context."""
from __future__ import annotations

import csv
import io
import os
import shutil
import subprocess
import tempfile
import warnings
from dataclasses import dataclass
from pathlib import Path

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
MAX_IMAGE_SIDE = 8_000
MAX_OCR_OUTPUT_BYTES = 1_000_000


class OcrInputError(ValueError):
    """The uploaded image is not a supported, bounded image."""


class OcrUnavailable(RuntimeError):
    """The isolated local OCR process could not produce a bounded result."""


@dataclass(frozen=True, slots=True)
class OcrLine:
    text: str
    confidence: float


@dataclass(frozen=True, slots=True)
class OcrDocument:
    lines: tuple[OcrLine, ...]
    language: str = "spa"


def validate_image(image_bytes: bytes, declared_mime: str | None = None) -> str:
    """Validate real image type and fully decode a bounded JPEG/PNG in memory."""
    if not isinstance(image_bytes, bytes) or not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
        raise OcrInputError("La imagen está vacía o supera el límite de 5 MB.")
    if image_bytes.startswith(b"\xff\xd8\xff"):
        expected, mime = "JPEG", "image/jpeg"
    elif image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        expected, mime = "PNG", "image/png"
    else:
        raise OcrInputError("Solo se aceptan imágenes JPEG o PNG.")
    if declared_mime is not None and declared_mime.lower().split(";", 1)[0].strip() != mime:
        raise OcrInputError("El formato real de la imagen no coincide con el declarado.")
    try:
        from PIL import Image
    except ImportError as exc:
        raise OcrUnavailable("Falta la dependencia segura de lectura de imágenes.") from exc
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(image_bytes)) as image:
                if image.format != expected or image.width <= 0 or image.height <= 0:
                    raise OcrInputError("El archivo no es una imagen válida.")
                if image.width > MAX_IMAGE_SIDE or image.height > MAX_IMAGE_SIDE or image.width * image.height > MAX_IMAGE_PIXELS:
                    raise OcrInputError("La imagen excede las dimensiones permitidas.")
                image.verify()
            with Image.open(io.BytesIO(image_bytes)) as image:
                image.load()
    except OcrInputError:
        raise
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise OcrInputError("No se pudo validar o abrir la imagen.") from exc
    return mime


class TesseractEngine:
    """Run Spanish Tesseract through stdin in a private, automatically removed cwd."""

    def __init__(self, executable: str | Path | None = None, *, timeout_seconds: float = 10.0):
        if type(timeout_seconds) not in {int, float} or not 1 <= timeout_seconds <= 30:
            raise ValueError("El límite del OCR debe ser de 1 a 30 segundos.")
        configured = str(executable or os.getenv("TESSERACT_CMD", "tesseract"))
        resolved = shutil.which(configured) if not Path(configured).is_absolute() else configured
        if not resolved or not Path(resolved).is_file():
            raise OcrUnavailable("No está disponible el motor OCR local.")
        self.executable = resolved
        self.timeout_seconds = float(timeout_seconds)

    def recognize(self, image_bytes: bytes, *, declared_mime: str | None = None) -> OcrDocument:
        validate_image(image_bytes, declared_mime)
        try:
            from PIL import Image, ImageOps
        except ImportError as exc:
            raise OcrUnavailable("Falta la dependencia segura de lectura de imágenes.") from exc
        env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "TESSDATA_PREFIX", "LANG", "LC_ALL") if key in os.environ}
        with Image.open(io.BytesIO(image_bytes)) as opened:
            base = ImageOps.exif_transpose(opened).convert("RGB")
            candidates = [base]
            initial_bytes = _png_bytes(base)
            initial = self._recognize_once(initial_bytes, env)
            if _document_quality(initial) >= 0.88:
                return initial
            candidates.extend(base.rotate(angle, expand=True, fillcolor="white") for angle in (180, 90, 270))
        best = initial
        best_score = _document_quality(initial)
        for candidate in candidates[1:]:
            document = self._recognize_once(_png_bytes(candidate), env)
            score = _document_quality(document)
            if score > best_score:
                best, best_score = document, score
        return best

    def _recognize_once(self, image_bytes: bytes, env: dict[str, str]) -> OcrDocument:
        try:
            with tempfile.TemporaryDirectory(prefix="coffee-house-h2-ocr-") as private_cwd:
                result = subprocess.run(
                    [self.executable, "stdin", "stdout", "-l", "spa", "--psm", "6", "tsv"],
                    input=image_bytes,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    cwd=private_cwd,
                    env=env,
                    timeout=self.timeout_seconds,
                    check=False,
                    close_fds=True,
                )
        except subprocess.TimeoutExpired as exc:
            raise OcrUnavailable("La lectura del ticket excedió el tiempo permitido.") from exc
        except OSError as exc:
            raise OcrUnavailable("No se pudo iniciar el motor OCR local.") from exc
        if result.returncode != 0 or len(result.stdout) > MAX_OCR_OUTPUT_BYTES:
            raise OcrUnavailable("El motor OCR no devolvió una lectura segura.")
        return _parse_tsv(result.stdout)


def _parse_tsv(payload: bytes) -> OcrDocument:
    try:
        reader = csv.DictReader(io.StringIO(payload.decode("utf-8", errors="strict")), delimiter="\t")
        grouped: dict[tuple[str, str, str, str, str], list[tuple[str, float]]] = {}
        for row in reader:
            if row.get("level") != "5":
                continue
            word = row.get("text", "").strip()
            try:
                confidence = float(row.get("conf", "-1")) / 100
            except ValueError:
                continue
            if not word or not 0 <= confidence <= 1:
                continue
            key = tuple(row.get(column, "") for column in ("page_num", "block_num", "par_num", "line_num", "word_num"))
            grouped.setdefault(key[:4], []).append((word, confidence))
    except (csv.Error, UnicodeError) as exc:
        raise OcrUnavailable("La respuesta del OCR no tiene formato válido.") from exc
    lines = []
    for words in grouped.values():
        text = " ".join(word for word, _ in words).strip()
        if text:
            lines.append(OcrLine(text=text[:512], confidence=sum(score for _, score in words) / len(words)))
    return OcrDocument(tuple(lines))


def _png_bytes(image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False)
    return output.getvalue()


def _document_quality(document: OcrDocument) -> float:
    weighted = count = 0
    for line in document.lines:
        text_length = sum(character.isalnum() for character in line.text)
        weighted += line.confidence * text_length
        count += text_length
    return weighted / count if count else 0.0
