#!/usr/bin/env python3
"""
rhodawk_core/image_gen.py — FAL.ai Image Generation (Layer H)

Provides image generation via FAL.ai (fast, affordable, high-quality).
Falls back to placeholder message when FAL_API_KEY is not set.

Models supported:
  - fal-ai/flux/schnell (default, fastest, free tier)
  - fal-ai/flux/dev (higher quality, slower)
  - fal-ai/stable-diffusion-v3-medium
  - fal-ai/imagen4/preview (Google Imagen 4)

Usage from gateway:
  result = generate_image("A futuristic code terminal at midnight")
  # Returns: path to downloaded PNG or error message

Copyright (c) 2024-2025 Rhodawk AI. All rights reserved.
"""

import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# ── Config ────────────────────────────────────────────────────────────────────

FAL_API_KEY  = os.environ.get("FAL_API_KEY", "")
FAL_BASE_URL = "https://fal.run"
IMAGE_DIR    = Path(os.environ.get("HERMES_HOME", "/data/.hermes")) / "images"

# Default model: schnell is fastest and free tier has generous limits
DEFAULT_MODEL = os.environ.get("FAL_IMAGE_MODEL", "fal-ai/flux/schnell")


# ── Data Models ───────────────────────────────────────────────────────────────


@dataclass
class ImageResult:
    """Result from an image generation request."""
    success: bool
    image_url: str = ""
    local_path: str = ""
    prompt: str = ""
    model: str = ""
    seed: Optional[int] = None
    error: str = ""
    duration_ms: float = 0.0


# ── FAL.ai Client ─────────────────────────────────────────────────────────────


def _fal_request(model: str, payload: dict) -> dict:
    """Make a synchronous request to the FAL.ai API."""
    url = f"{FAL_BASE_URL}/{model}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Key {FAL_API_KEY}",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def _download_image(image_url: str, output_path: Path) -> bool:
    """Download an image from URL to local path."""
    try:
        req = urllib.request.Request(image_url)
        req.add_header("User-Agent", "RhodawkAI/1.0")
        with urllib.request.urlopen(req, timeout=30) as resp:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(resp.read())
        return True
    except Exception:
        return False


# ── Main Interface ────────────────────────────────────────────────────────────


def generate_image(
    prompt: str,
    model: str = DEFAULT_MODEL,
    width: int = 1024,
    height: int = 1024,
    num_inference_steps: int = 4,   # schnell default
    seed: Optional[int] = None,
    download: bool = True,
) -> ImageResult:
    """
    Generate an image via FAL.ai.

    Args:
        prompt: Text description of the image
        model: FAL model ID (default: fal-ai/flux/schnell)
        width: Image width in pixels (default: 1024)
        height: Image height in pixels (default: 1024)
        num_inference_steps: Diffusion steps (4=fast/schnell, 20-50=quality)
        seed: Optional seed for reproducibility
        download: If True, download image to local file

    Returns:
        ImageResult with image_url and local_path (if download=True)
    """
    if not FAL_API_KEY:
        return ImageResult(
            success=False,
            prompt=prompt,
            model=model,
            error=(
                "FAL_API_KEY not set. Get a free key at https://fal.ai and "
                "add it as a secret: FAL_API_KEY=fal-xxx"
            ),
        )

    start_time = time.time()

    # Build model-appropriate payload
    payload: dict = {
        "prompt": prompt,
        "image_size": {"width": width, "height": height},
        "num_inference_steps": num_inference_steps,
        "enable_safety_checker": False,
    }
    if seed is not None:
        payload["seed"] = seed

    # Adjust steps for quality models
    if "dev" in model or "stable-diffusion" in model or "imagen" in model:
        payload["num_inference_steps"] = max(num_inference_steps, 20)

    try:
        response = _fal_request(model, payload)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300] if exc.fp else ""
        return ImageResult(
            success=False,
            prompt=prompt,
            model=model,
            error=f"FAL API error HTTP {exc.code}: {body}",
            duration_ms=(time.time() - start_time) * 1000,
        )
    except Exception as exc:
        return ImageResult(
            success=False,
            prompt=prompt,
            model=model,
            error=f"FAL request failed: {exc}",
            duration_ms=(time.time() - start_time) * 1000,
        )

    # Parse response — FAL returns {"images": [{"url": "..."}]}
    images = response.get("images", [])
    if not images:
        return ImageResult(
            success=False,
            prompt=prompt,
            model=model,
            error=f"No images in FAL response: {json.dumps(response)[:300]}",
            duration_ms=(time.time() - start_time) * 1000,
        )

    image_url = images[0].get("url", "")
    result_seed = response.get("seed")

    result = ImageResult(
        success=True,
        image_url=image_url,
        prompt=prompt,
        model=model,
        seed=result_seed,
        duration_ms=(time.time() - start_time) * 1000,
    )

    if download and image_url:
        # Save to hermes images directory
        safe_prompt = "".join(c if c.isalnum() else "_" for c in prompt)[:40]
        ts = int(time.time())
        filename = f"{ts}_{safe_prompt}.png"
        local_path = IMAGE_DIR / filename
        if _download_image(image_url, local_path):
            result.local_path = str(local_path)
        else:
            result.local_path = ""

    return result


def format_result(result: ImageResult) -> str:
    """Format an ImageResult as a human-readable message for Telegram delivery."""
    if not result.success:
        return f"Image generation failed: {result.error}"

    lines = [
        f"Image generated ({result.model.split('/')[-1]})",
        f"Prompt: {result.prompt[:100]}",
        f"Duration: {result.duration_ms/1000:.1f}s",
    ]
    if result.seed:
        lines.append(f"Seed: {result.seed}")
    if result.local_path:
        lines.append(f"Saved: {result.local_path}")
    if result.image_url and not result.local_path:
        lines.append(f"URL: {result.image_url}")
    return "\n".join(lines)


# ── Available Models ──────────────────────────────────────────────────────────

AVAILABLE_MODELS = {
    "schnell":          "fal-ai/flux/schnell",          # Fastest, free tier
    "flux-dev":         "fal-ai/flux/dev",              # High quality
    "stable-diffusion": "fal-ai/stable-diffusion-v3-medium",
    "imagen4":          "fal-ai/imagen4/preview",       # Google Imagen 4
}


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="FAL.ai Image Generator")
    parser.add_argument("prompt", help="Image prompt")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="FAL model ID")
    parser.add_argument("--width", type=int, default=1024)
    parser.add_argument("--height", type=int, default=1024)
    parser.add_argument("--steps", type=int, default=4)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--no-download", action="store_true")
    args = parser.parse_args()

    result = generate_image(
        prompt=args.prompt,
        model=args.model,
        width=args.width,
        height=args.height,
        num_inference_steps=args.steps,
        seed=args.seed,
        download=not args.no_download,
    )
    print(format_result(result))
    if result.success and result.image_url:
        print(f"\nImage URL: {result.image_url}")
