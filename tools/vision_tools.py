"""
Vision tools for Grid agents: cropping a screenshot to zoom in.
"""
import io
import logging
from pathlib import Path
from typing import Any, List, Optional, Union
import base64

from agents import function_tool, RunContextWrapper
from agents.tool import ToolOutputImage, ToolOutputText
from utils.path_utils import display_agent_path_from_ctx, resolve_agent_path_from_ctx
from utils.tool_requirements import Requires

logger = logging.getLogger("tools.vision")

# Where screenshot tools keep the latest capture, relative to the working directory.
LAST_SCREENSHOT = "screenshots/last_screenshot.png"


def _image_path_to_data_url(image_path: str) -> str:
    """
    Convert image path to data URL (base64).
    Accepts absolute host path.
    """
    img_file = Path(image_path).resolve()

    if not img_file.exists():
        raise FileNotFoundError(f"Image file not found: {image_path} (resolved to: {img_file})")

    ext = img_file.suffix.lower()
    mime_types = {
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.gif': 'image/gif',
        '.webp': 'image/webp',
        '.bmp': 'image/bmp'
    }
    mime_type = mime_types.get(ext, 'image/jpeg')

    with open(img_file, 'rb') as f:
        img_data = f.read()

    b64_data = base64.b64encode(img_data).decode('utf-8')
    return f"data:{mime_type};base64,{b64_data}"


@function_tool
async def crop_image(
    ctx: RunContextWrapper[Any],
    left: int,
    top: int,
    right: int,
    bottom: int,
    image_path: Optional[str] = None,
) -> List[Union[ToolOutputText, ToolOutputImage]]:
    """
    Cuts an image fragment by pixel coordinates for detailed zoom-in.

    Coordinates in pixels of the screenshot.

    Args:
        left: X of left edge (pixels)
        top: Y of top edge (pixels)
        right: X of right edge (pixels)
        bottom: Y of bottom edge (pixels)
        image_path: Path to the image (None = last screenshot)
    """
    try:
        from PIL import Image
    except ImportError:
        return [ToolOutputText(text="❌ Pillow is not installed. Install with: pip install Pillow")]

    image_path = image_path or LAST_SCREENSHOT
    try:
        img_file = Path(resolve_agent_path_from_ctx(image_path, ctx)).resolve()
    except Exception as e:
        return [ToolOutputText(text=f"❌ Error loading image: {str(e)}")]
    if not img_file.exists():
        visible_path = display_agent_path_from_ctx(image_path, ctx)
        return [ToolOutputText(text=f"❌ File not found: {visible_path}. Take a screenshot first.")]

    try:
        with Image.open(img_file) as img:
            w, h = img.size

            if left >= right or top >= bottom:
                return [ToolOutputText(
                    text=f"❌ Invalid coordinates: left={left}, top={top}, right={right}, bottom={bottom}."
                )]

            cropped = img.crop((left, top, right, bottom))
            crop_w, crop_h = cropped.size

            output = io.BytesIO()
            rgb = cropped.convert("RGB") if cropped.mode not in ("RGB", "L") else cropped
            rgb.save(output, format="JPEG", quality=90)
            img_bytes = output.getvalue()

        b64 = base64.b64encode(img_bytes).decode("utf-8")
        data_url = f"data:image/jpeg;base64,{b64}"

        logger.info(f"✂️ Crop {left},{top}→{right},{bottom} ({crop_w}x{crop_h}px) from {img_file.name}")
        return [
            ToolOutputText(text=f"✂️ Crop {left},{top}→{right},{bottom} ({crop_w}x{crop_h}px)"),
            ToolOutputImage(image_url=data_url, detail="high"),
        ]

    except Exception as e:
        logger.error(f"Error in crop_image: {e}", exc_info=True)
        return [ToolOutputText(text=f"❌ Error during crop: {e}")]


# Export tools
TOOL_REQUIREMENTS = {
    "crop_image": Requires(modules=("PIL",), hint="pip install Pillow"),
}

VISION_TOOLS = {
    "crop_image": crop_image,
}
