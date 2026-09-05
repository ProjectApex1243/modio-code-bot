"""OCR for the in-game SUPPORT screen: pulls a Player ID out of a screenshot
so staff don't have to transcribe one by eye.

Needs the tesseract-ocr system binary on the host - pytesseract is only a
Python wrapper around it, not an OCR engine by itself. A host missing either
the pip package or the binary raises OcrUnavailableError so the caller can
say so plainly instead of surfacing a raw traceback.
"""

import io
import re

from PIL import Image, ImageOps

try:
    import pytesseract
except ImportError:  # pytesseract is in requirements.txt but may not be
    pytesseract = None  # installed yet on an older deploy.

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)

# Only characters that are definitely NOT a valid hex digit get corrected -
# 'b' is never touched even though it looks like it could be a misread '8' or
# '6', because 'b' is itself a legal hex digit and guessing wrong would
# silently corrupt an otherwise-correct id.
_CONFUSABLE = str.maketrans({
    "o": "0", "i": "1", "l": "1", "s": "5", "z": "2", "g": "9", "|": "1",
})

# Tesseract only proposes characters from this set, narrowed to hex digits
# plus the specific look-alikes _CONFUSABLE knows how to fix - anything else
# it might otherwise guess (punctuation, other letters) can't be a hex digit
# and would just be noise.
_TESSERACT_WHITELIST = "0123456789abcdefOoIiLlSsZzGg|-"


class OcrUnavailableError(RuntimeError):
    """tesseract-ocr (the binary, or the pytesseract wrapper) isn't available
    on this host."""


def extract_player_id(image_bytes: bytes) -> tuple[str | None, str]:
    """Returns (uuid_or_None, raw_ocr_text).

    The raw text is always returned alongside the match (or lack of one) so a
    failed read can still be shown to staff instead of a bare "nothing
    found" with no way to tell whether the image was fine and the id just
    didn't parse, or the image was unreadable to begin with.
    """
    if pytesseract is None:
        raise OcrUnavailableError(
            "The `pytesseract` package isn't installed on this host — check "
            "that the last deploy picked up requirements.txt."
        )

    image = Image.open(io.BytesIO(image_bytes)).convert("L")
    # Upscale small images and force pure black/white. The terminal font is
    # high-contrast to begin with, so this mostly helps phone photos taken at
    # an angle rather than clean digital screenshots.
    if image.width < 1200:
        scale = 1200 / image.width
        image = image.resize((1200, max(1, int(image.height * scale))))
    image = ImageOps.autocontrast(image)
    image = image.point(lambda p: 255 if p > 140 else 0)

    try:
        raw = pytesseract.image_to_string(
            image,
            config=f"--psm 6 -c tessedit_char_whitelist={_TESSERACT_WHITELIST}",
        )
    except pytesseract.TesseractNotFoundError as error:
        raise OcrUnavailableError(
            "The `tesseract-ocr` system package isn't installed on this "
            "host — pytesseract only wraps it, it doesn't include it."
        ) from error

    # A wrapped id has a newline (or a run of spaces, on some phone photos)
    # where the screen just ran out of width, not an extra character - so
    # whitespace is deleted outright rather than replaced with anything.
    flattened = re.sub(r"[‐-―−]", "-", raw)
    flattened = re.sub(r"\s+", "", flattened).lower()
    corrected = flattened.translate(_CONFUSABLE)

    match = _UUID_RE.search(corrected)
    return (match.group(0) if match else None), raw.strip()
