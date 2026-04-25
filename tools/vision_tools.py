"""
Vision Tools for Grid agents - viewing and analyzing images.
"""
import io
import logging
from pathlib import Path
from typing import Any, List, Optional, Union
import base64

from agents import function_tool, RunContextWrapper
from agents.tool import ToolOutputImage, ToolOutputText
from utils.path_utils import display_agent_path_from_ctx, resolve_agent_path_from_ctx

logger = logging.getLogger("tools.vision")


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


async def _inject_image_for_analysis(
    ctx: RunContextWrapper[Any],
    image_path: str,
    question: str,
) -> List[Union[ToolOutputText, ToolOutputImage]]:
    """
    Load image and return as ToolOutputImage for agent analysis.
    """
    visible_path = display_agent_path_from_ctx(image_path, ctx)
    logger.info(f"Processing image: {visible_path}")

    file_ext = Path(image_path).suffix.lower()
    if file_ext == '.pdf':
        return [ToolOutputText(
            text=f"❌ ERROR: view_image does not support PDF files!\n\n"
                 f"To analyze PDF use:\n"
                 f'- pdf("{visible_path}", pages="1:1") to get pages as images\n'
                 f'- pdf-ocr("{visible_path}", pages="1:1") for OCR'
        )]

    try:
        resolved = resolve_agent_path_from_ctx(image_path, ctx)
        data_url = _image_path_to_data_url(resolved)
    except Exception as e:
        return [ToolOutputText(text=f"❌ Error loading image: {str(e)}")]

    logger.info(f"✅ Returning ToolOutputImage for {visible_path}")
    return [
        ToolOutputText(text=f"📷 Image: {visible_path}\n\nQuestion: {question}"),
        ToolOutputImage(image_url=data_url, detail="high"),
    ]


@function_tool
async def view_image(
    ctx: RunContextWrapper[Any],
    image_path: str,
    question: str = "Describe in detail what is shown in this picture"
) -> List[Union[ToolOutputText, ToolOutputImage]]:
    """
    Shows an image to the agent for analysis. Only for jpg, png, gif, webp, bmp — not for PDF.

    Args:
        image_path: Path to the image file
        question: What to find or describe in the image
    """
    try:
        return await _inject_image_for_analysis(ctx, image_path, question)
    except Exception as e:
        logger.error(f"Error in view_image tool: {e}", exc_info=True)
        return [ToolOutputText(text=f"❌ Error processing image: {str(e)}")]


@function_tool
async def analyze_screenshot(
    ctx: RunContextWrapper[Any],
    screenshot_path: str
) -> List[Union[ToolOutputText, ToolOutputImage]]:
    """
    Analyze a UI/screenshot. Specialized version of view_image for screenshots.

    Args:
        screenshot_path: Path to the screenshot

    Returns:
        List with screenshot analysis results

    Example:
        analyze_screenshot(ctx, "/path/to/screenshot.png")
    """
    return await _inject_image_for_analysis(
        ctx,
        screenshot_path,
        "Analyze this screenshot: determine the interface type, find all UI elements (buttons, fields, menus), describe what is displayed on the screen."
    )


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

    Coordinates in pixels (taken from the screenshot size returned by take_screenshot).

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

    if image_path is None:
        wd = Path(ctx.context.factory.config.get_working_directory())
        img_file = wd / "screenshots" / "last_screenshot.png"
        if not img_file.exists():
            return [ToolOutputText(
                text="❌ No last screenshot. Call take_screenshot() first."
            )]
    else:
        try:
            img_file = Path(resolve_agent_path_from_ctx(image_path, ctx)).resolve()
        except Exception as e:
            return [ToolOutputText(text=f"❌ Error loading image: {str(e)}")]
        if not img_file.exists():
            visible_path = display_agent_path_from_ctx(image_path, ctx)
            return [ToolOutputText(text=f"❌ File not found: {visible_path}")]

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
VISION_TOOLS = {
    "view_image": view_image,
    "analyze_screenshot": analyze_screenshot,
    "crop_image": crop_image,
}
