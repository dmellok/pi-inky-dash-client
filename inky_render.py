#!/usr/bin/env python3
"""
Single-shot renderer for the Inky Impression 13.3".

Reads one JSON job from stdin, renders one image, exits. Designed to be
invoked as a subprocess by inky_display.py so that every render runs in
a fresh process — fully reinitialising the inky driver and releasing all
GPIO/SPI state on exit. This avoids the long-running-process degradation
where inky's set_image()/show() silently stops actually updating the
panel after many consecutive calls.

Stdin payload (JSON object, identical to the MQTT contract):
    {
        "url":        "https://example.com/photo.jpg",   // OR
        "path":       "/home/kayden/images/photo.jpg",   // OR
        "bin":        "/home/kayden/images/photo.bin"        // OR an
                                                              // http(s):// URL
        "rotate":     0 | 90 | 180 | 270,
        "scale":      "fit" | "fill" | "stretch" | "center",
        "bg":         "white" | "black" | "red" | "green" | "blue" | "yellow" | "orange",
        "saturation": 0.0 - 1.0
    }

All fields except url/path/bin are optional. `bin` is a pre-quantized,
panel-ready buffer (see RAW_BUF_BYTES below); when set, the image-
processing fields (rotate/scale/bg/saturation) are ignored. Logs go to
stderr so the parent can forward them to the journal.

Exit codes:
    0  rendered and shown successfully
    1  bad input / network error / panel error
"""

import io
import json
import logging
import sys
from urllib.parse import urlparse

import requests
from PIL import Image, ImageOps


WIDTH, HEIGHT = 1600, 1200

# Size of a pre-quantized panel-ready buffer: 1600*1200 pixels at 4 bits
# each = 960_000 bytes. The first half goes to CS0 (left columns) and the
# second to CS1 (right columns); see render_raw_bin() for the layout the
# upstream tool must produce.
RAW_BUF_BYTES = (WIDTH * HEIGHT) // 2
RAW_HALF_BYTES = RAW_BUF_BYTES // 2

# Hard cap on input pixel count. Anything bigger gets rejected before we
# try to decode it into RGB and OOM the Pi. A 100MP RGB image is roughly
# 300MB just for the source buffer, before working copies.
MAX_INPUT_PIXELS = 100_000_000  # 100 megapixels

# Disable Pillow's built-in "decompression bomb" check; we do our own
# size check above and use JPEG draft mode below to handle large inputs
# gracefully instead of warning about every legitimate large photo.
Image.MAX_IMAGE_PIXELS = None

BG_COLORS = {
    "black":  (0,   0,   0),
    "white":  (255, 255, 255),
    "red":    (255, 0,   0),
    "green":  (0,   255, 0),
    "blue":   (0,   0,   255),
    "yellow": (255, 255, 0),
    "orange": (255, 140, 0),
}

VALID_SCALES = {"fit", "fill", "stretch", "center"}
VALID_ROTATIONS = {0, 90, 180, 270}

DEFAULTS = {
    "rotate":     0,
    "scale":      "fit",
    "bg":         "white",
    "saturation": 0.5,
}

HTTP_TIMEOUT = 30  # seconds

log = logging.getLogger("inky-render")


def fetch_image(source):
    parsed = urlparse(source)
    if parsed.scheme in ("http", "https"):
        log.info("Fetching image from %s", source)
        resp = requests.get(source, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content))
    else:
        log.info("Loading image from %s", source)
        img = Image.open(source)

    log.info("Source image: %s %dx%d (%.1f MP)",
             img.format, img.width, img.height,
             (img.width * img.height) / 1_000_000)

    # For huge JPEGs, draft mode lets Pillow decode at a lower resolution
    # straight from the JPEG's downsample tables instead of materialising
    # the full pixel buffer. No-op for non-JPEG formats. We aim for ~2x
    # the panel size so quality after rotate/resize stays high.
    if img.format == "JPEG":
        original_size = img.size
        img.draft("RGB", (WIDTH * 2, HEIGHT * 2))
        if img.size != original_size:
            log.info("JPEG draft-decoded from %dx%d to %dx%d",
                     *original_size, img.width, img.height)

    # After any draft-mode reduction, refuse to decode anything still too
    # big — saves us from being SIGKILL'd by the kernel mid-render.
    if img.width * img.height > MAX_INPUT_PIXELS:
        raise ValueError(
            f"Image too large: {img.width}x{img.height} = "
            f"{(img.width * img.height) / 1_000_000:.0f} MP "
            f"(limit is {MAX_INPUT_PIXELS // 1_000_000} MP). "
            "Resize the image or raise MAX_INPUT_PIXELS in inky_render.py."
        )

    return img.convert("RGB")


def prepare_image(img, target_size, scale, rotation, bg_color):
    if rotation:
        img = img.rotate(rotation, expand=True, resample=Image.BICUBIC)

    tw, th = target_size

    if scale == "stretch":
        return img.resize((tw, th), Image.LANCZOS)

    if scale == "fill":
        return ImageOps.fit(img, (tw, th), method=Image.LANCZOS,
                            centering=(0.5, 0.5))

    if scale == "fit":
        scaled = img.copy()
        scaled.thumbnail((tw, th), Image.LANCZOS)
        out = Image.new("RGB", (tw, th), bg_color)
        out.paste(scaled, ((tw - scaled.width) // 2, (th - scaled.height) // 2))
        return out

    if scale == "center":
        out = Image.new("RGB", (tw, th), bg_color)
        out.paste(img, ((tw - img.width) // 2, (th - img.height) // 2))
        return out

    raise ValueError(f"Unknown scale type: {scale}")


def _load_bin_bytes(source):
    """Read a panel-ready buffer from either a local path or http(s) URL."""
    parsed = urlparse(source)
    if parsed.scheme in ("http", "https"):
        log.info("Fetching bin from %s", source)
        resp = requests.get(source, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        return resp.content

    log.info("Loading bin from %s", source)
    with open(source, "rb") as f:
        return f.read()


def render_raw_bin(source, inky):
    """Push a pre-quantized panel-ready buffer to the display.

    `source` is either a local filesystem path or an http(s):// URL.
    The upstream tool must produce bytes in the exact layout the
    EL133UF1 expects on the wire:

        1. Start with a (height=1200, width=1600) array of palette
           indices (0..6) — the 6-colour Spectra palette, with 4
           reserved/unused.
        2. numpy.rot90(buf, -1) → shape (1600, 1200).
        3. Split column-wise at col 600: left half (1600, 600) and
           right half (1600, 600), flatten each in row-major order.
        4. Pack consecutive index pairs into nibbles:
               byte = ((a << 4) & 0xF0) | (b & 0x0F)
           giving two 480_000-byte buffers.
        5. Concatenate: left half first (CS0), right half second (CS1).

    Total: 960_000 bytes. Any other size is rejected.
    """
    data = _load_bin_bytes(source)
    if len(data) != RAW_BUF_BYTES:
        raise ValueError(
            f"Bin from {source} is {len(data)} bytes; expected "
            f"{RAW_BUF_BYTES} (1600x1200 4-bit packed)."
        )

    buf_a = list(data[:RAW_HALF_BYTES])
    buf_b = list(data[RAW_HALF_BYTES:])

    log.info("Pushing %d bytes (panel-ready) to %dx%d display",
             len(data), inky.width, inky.height)
    inky._update(buf_a, buf_b)


def parse_options(opts):
    rotate = int(opts.get("rotate", DEFAULTS["rotate"]))
    if rotate not in VALID_ROTATIONS:
        raise ValueError(f"rotate must be one of {sorted(VALID_ROTATIONS)}")

    scale = str(opts.get("scale", DEFAULTS["scale"])).lower()
    if scale not in VALID_SCALES:
        raise ValueError(f"scale must be one of {sorted(VALID_SCALES)}")

    bg = str(opts.get("bg", DEFAULTS["bg"])).lower()
    if bg not in BG_COLORS:
        raise ValueError(f"bg must be one of {sorted(BG_COLORS)}")

    saturation = float(opts.get("saturation", DEFAULTS["saturation"]))
    if not 0.0 <= saturation <= 1.0:
        raise ValueError("saturation must be between 0.0 and 1.0")

    return rotate, scale, bg, saturation


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        stream=sys.stderr,
    )

    raw = sys.stdin.read()
    if not raw.strip():
        log.error("No JSON job on stdin")
        sys.exit(1)

    try:
        job = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error("Invalid JSON: %s", e)
        sys.exit(1)

    if not isinstance(job, dict):
        log.error("Job must be a JSON object, got %s", type(job).__name__)
        sys.exit(1)

    bin_path = job.get("bin")
    source = job.get("url") or job.get("path")
    if not bin_path and not source:
        log.error("Job missing 'url', 'path', or 'bin' field")
        sys.exit(1)

    # Import inky lazily — we want to bail out cleanly on bad input
    # before we touch any hardware.
    try:
        from inky.auto import auto
    except ImportError:
        log.error("'inky' library not installed in this Python environment")
        sys.exit(1)

    try:
        inky = auto(ask_user=False, verbose=False)
        log.info("Panel detected: %s %dx%d",
                 type(inky).__name__, inky.width, inky.height)

        if bin_path:
            render_raw_bin(bin_path, inky)
        else:
            try:
                rotate, scale, bg, saturation = parse_options(job)
            except (ValueError, TypeError) as e:
                log.error("Invalid options: %s", e)
                sys.exit(1)

            img = fetch_image(source)
            prepared = prepare_image(img, (inky.width, inky.height),
                                     scale, rotate, BG_COLORS[bg])
            log.info(
                "Rendering (rotate=%d, scale=%s, bg=%s, saturation=%.2f)",
                rotate, scale, bg, saturation,
            )
            inky.set_image(prepared, saturation=saturation)
            inky.show()
        log.info("Render complete")
    except Exception:
        log.exception("Render failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
