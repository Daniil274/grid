"""
Utilities for working with images in multimodal messages.
Handles image conversion, validation, and format detection.
"""

import base64
import mimetypes
import os
import re
from pathlib import Path
from typing import Optional, Tuple, Union, TYPE_CHECKING
from urllib.parse import urlparse
import logging

if TYPE_CHECKING:
    from schemas.schemas import ImageProcessingConfig

logger = logging.getLogger(__name__)


class ImageUtils:
    """Utilities for image processing and conversion."""

    SUPPORTED_FORMATS = {
        'image/jpeg', 'image/jpg', 'image/png', 'image/gif',
        'image/webp', 'image/bmp', 'image/tiff'
    }

    # Default settings (can be overridden by config)
    _config: Optional['ImageProcessingConfig'] = None

    @classmethod
    def set_config(cls, config: 'ImageProcessingConfig'):
        """Set image processing configuration."""
        cls._config = config

    @classmethod
    def get_max_size_bytes(cls) -> int:
        """Get maximum file size in bytes."""
        if cls._config:
            return cls._config.max_file_size_mb * 1024 * 1024
        return 10 * 1024 * 1024  # Default 10MB

    @classmethod
    def get_max_dimensions(cls) -> Tuple[int, int]:
        """Get maximum image dimensions (width, height)."""
        if cls._config:
            return cls._config.max_width, cls._config.max_height
        return 512, 512  # Default

    @classmethod
    def get_jpeg_quality(cls) -> int:
        """Get JPEG quality setting."""
        if cls._config:
            return cls._config.jpeg_quality
        return 70  # Default

    @classmethod
    def is_auto_resize_enabled(cls) -> bool:
        """Check if auto-resize is enabled."""
        if cls._config:
            return cls._config.auto_resize
        return True  # Default

    @staticmethod
    def is_base64_image(data: str) -> bool:
        """Check if string is a base64-encoded image."""
        return data.startswith('data:image/') and ';base64,' in data

    @staticmethod
    def is_image_url(url: str) -> bool:
        """Check if URL points to an image."""
        try:
            parsed = urlparse(url)
            if not parsed.scheme in ('http', 'https', 'file'):
                return False

            # Check file extension
            path_lower = parsed.path.lower()
            image_extensions = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tiff', '.tif')
            if any(path_lower.endswith(ext) for ext in image_extensions):
                return True

            # Check if it's a base64 data URL
            if ImageUtils.is_base64_image(url):
                return True

            return False
        except Exception:
            return False

    @staticmethod
    def is_local_path(path: str) -> bool:
        """Check if string is a local file path."""
        try:
            p = Path(path)
            return p.exists() and p.is_file()
        except Exception:
            return False

    @staticmethod
    def get_mime_type(file_path: Union[str, Path]) -> Optional[str]:
        """Get MIME type of an image file."""
        try:
            mime_type, _ = mimetypes.guess_type(str(file_path))
            if mime_type and mime_type in ImageUtils.SUPPORTED_FORMATS:
                return mime_type

            # Fallback: try to detect from extension
            ext = Path(file_path).suffix.lower()
            mime_map = {
                '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg',
                '.png': 'image/png',
                '.gif': 'image/gif',
                '.webp': 'image/webp',
                '.bmp': 'image/bmp',
                '.tiff': 'image/tiff',
                '.tif': 'image/tiff',
            }
            return mime_map.get(ext)
        except Exception as e:
            logger.warning(f"Failed to get MIME type for {file_path}: {e}")
            return None

    @classmethod
    def resize_image_if_needed(cls, file_path: Union[str, Path], max_width: Optional[int] = None, max_height: Optional[int] = None) -> Optional[bytes]:
        """
        Resize image if it exceeds max dimensions to reduce token usage.

        Args:
            file_path: Path to image file
            max_width: Maximum width in pixels (uses config if not provided)
            max_height: Maximum height in pixels (uses config if not provided)

        Returns:
            Image bytes (resized if needed) or None if failed
        """
        # Get dimensions from config if not provided
        if max_width is None or max_height is None:
            config_width, config_height = cls.get_max_dimensions()
            max_width = max_width or config_width
            max_height = max_height or config_height

        quality = cls.get_jpeg_quality()

        try:
            # Try to import PIL for resizing
            try:
                from PIL import Image
                import io
            except ImportError:
                # PIL not available, just read file as-is
                logger.warning("PIL not available, cannot resize images. Install: pip install Pillow")
                with open(file_path, 'rb') as f:
                    return f.read()

            # Open image
            with Image.open(file_path) as img:
                original_size = img.size

                # Always resize and compress to JPEG for consistent token usage (~8k tokens)
                # Calculate new size maintaining aspect ratio
                if img.width > max_width or img.height > max_height:
                    # Resize if exceeds limits
                    ratio = min(max_width / img.width, max_height / img.height)
                    new_size = (int(img.width * ratio), int(img.height * ratio))
                    img_resized = img.resize(new_size, Image.Resampling.LANCZOS)
                else:
                    # Keep original size but still compress to JPEG
                    new_size = original_size
                    img_resized = img

                # Convert to bytes with JPEG compression (always apply compression)
                output = io.BytesIO()
                # Save as JPEG for compression (quality from config)
                if img_resized.mode in ('RGBA', 'LA', 'P'):
                    # Convert RGBA/LA/P to RGB
                    rgb_img = Image.new('RGB', img_resized.size, (255, 255, 255))
                    if img_resized.mode == 'P':
                        img_resized = img_resized.convert('RGBA')
                    rgb_img.paste(img_resized, mask=img_resized.split()[-1] if img_resized.mode == 'RGBA' else None)
                    rgb_img.save(output, format='JPEG', quality=quality, optimize=True)
                else:
                    img_resized.save(output, format='JPEG', quality=quality, optimize=True)

                image_bytes = output.getvalue()

                if new_size != original_size:
                    logger.info(f"Resized image: {original_size} -> {new_size}, {len(image_bytes)} bytes")
                else:
                    logger.info(f"Compressed image: {original_size} (no resize), {len(image_bytes)} bytes")
                return image_bytes

        except Exception as e:
            logger.error(f"Failed to resize image {file_path}: {e}")
            # Fallback: read original
            try:
                with open(file_path, 'rb') as f:
                    return f.read()
            except Exception:
                return None

    @classmethod
    def file_to_base64(cls, file_path: Union[str, Path], mime_type: Optional[str] = None, resize: Optional[bool] = None) -> Optional[str]:
        """
        Convert image file to base64 data URL.

        Args:
            file_path: Path to image file
            mime_type: Optional MIME type (auto-detected if not provided)
            resize: Whether to resize large images (uses config if None)

        Returns:
            Base64 data URL or None if conversion fails
        """
        # Get resize setting from config if not specified
        if resize is None:
            resize = cls.is_auto_resize_enabled()

        try:
            path = Path(file_path)

            # Validate file exists
            if not path.exists():
                logger.error(f"Image file not found: {file_path}")
                return None

            file_size = path.stat().st_size
            max_size = cls.get_max_size_bytes()
            if file_size > max_size:
                logger.error(f"Image file too large ({file_size} bytes, max {max_size}): {file_path}")
                return None

            # Get MIME type
            if not mime_type:
                mime_type = cls.get_mime_type(path)

            if not mime_type:
                logger.error(f"Could not determine MIME type for: {file_path}")
                return None

            # Resize if needed to reduce token usage
            if resize:
                image_data = cls.resize_image_if_needed(path)
                if not image_data:
                    logger.error(f"Failed to process image: {file_path}")
                    return None
                # After resize, use JPEG mime type
                if mime_type != 'image/gif':  # Keep GIF as-is
                    mime_type = 'image/jpeg'
            else:
                # Read file as-is
                with open(path, 'rb') as f:
                    image_data = f.read()

            base64_data = base64.b64encode(image_data).decode('utf-8')
            data_url = f"data:{mime_type};base64,{base64_data}"

            logger.debug(f"Converted image to base64: {file_path} ({len(image_data)} bytes -> {len(base64_data)} base64 chars)")
            return data_url

        except Exception as e:
            logger.error(f"Failed to convert image to base64: {file_path} - {e}")
            return None

    @staticmethod
    def extract_base64_data(data_url: str) -> Tuple[Optional[str], Optional[bytes]]:
        """
        Extract MIME type and binary data from base64 data URL.

        Args:
            data_url: Base64 data URL (data:image/...;base64,...)

        Returns:
            Tuple of (mime_type, binary_data) or (None, None) if invalid
        """
        try:
            if not ImageUtils.is_base64_image(data_url):
                return None, None

            # Parse data URL
            match = re.match(r'data:(image/[^;]+);base64,(.+)', data_url)
            if not match:
                return None, None

            mime_type = match.group(1)
            base64_data = match.group(2)

            # Decode base64
            binary_data = base64.b64decode(base64_data)

            return mime_type, binary_data

        except Exception as e:
            logger.error(f"Failed to extract base64 data: {e}")
            return None, None

    @staticmethod
    def validate_image_source(source: str) -> Tuple[str, Optional[str]]:
        """
        Validate and classify image source.

        Args:
            source: Image source (URL, base64, or file path)

        Returns:
            Tuple of (source_type, error_message)
            source_type: 'url', 'base64', 'file', or 'invalid'
        """
        if not source or not isinstance(source, str):
            return 'invalid', 'Empty or invalid image source'

        # Check if it's a base64 data URL
        if ImageUtils.is_base64_image(source):
            mime_type, data = ImageUtils.extract_base64_data(source)
            if mime_type and data:
                return 'base64', None
            else:
                return 'invalid', 'Invalid base64 image data'

        # Check if it's a URL
        if ImageUtils.is_image_url(source):
            parsed = urlparse(source)
            if parsed.scheme in ('http', 'https'):
                return 'url', None
            elif parsed.scheme == 'file':
                # Convert file:// URL to path
                file_path = parsed.path
                if ImageUtils.is_local_path(file_path):
                    return 'file', None
                else:
                    return 'invalid', f'File not found: {file_path}'

        # Check if it's a local file path
        if ImageUtils.is_local_path(source):
            return 'file', None

        return 'invalid', f'Unknown image source type: {source[:100]}'

    @staticmethod
    def normalize_image_source(source: str, convert_files_to_base64: bool = True) -> Optional[str]:
        """
        Normalize image source to a standard format.

        Args:
            source: Image source (URL, base64, or file path)
            convert_files_to_base64: Convert local files to base64 data URLs

        Returns:
            Normalized image source (URL or base64) or None if invalid
        """
        source_type, error = ImageUtils.validate_image_source(source)

        if source_type == 'invalid':
            logger.error(f"Invalid image source: {error}")
            return None

        if source_type in ('url', 'base64'):
            return source

        if source_type == 'file':
            if convert_files_to_base64:
                # Convert local file to base64
                return ImageUtils.file_to_base64(source)
            else:
                # Return file path as-is
                return source

        return None

    @staticmethod
    def get_image_info(source: str) -> dict:
        """
        Get information about an image source.

        Returns:
            Dictionary with image info (type, size, mime_type, etc.)
        """
        info = {
            'source': source,
            'type': 'unknown',
            'size_bytes': None,
            'mime_type': None,
            'valid': False,
            'error': None
        }

        source_type, error = ImageUtils.validate_image_source(source)
        info['type'] = source_type
        info['valid'] = source_type != 'invalid'
        info['error'] = error

        if source_type == 'base64':
            mime_type, data = ImageUtils.extract_base64_data(source)
            info['mime_type'] = mime_type
            info['size_bytes'] = len(data) if data else None

        elif source_type == 'file':
            try:
                path = Path(source)
                info['size_bytes'] = path.stat().st_size
                info['mime_type'] = ImageUtils.get_mime_type(path)
            except Exception as e:
                info['error'] = str(e)

        elif source_type == 'url':
            # For URLs, we can't determine size without fetching
            # Try to guess MIME type from URL
            parsed = urlparse(source)
            ext = Path(parsed.path).suffix.lower()
            mime_map = {
                '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                '.png': 'image/png', '.gif': 'image/gif',
                '.webp': 'image/webp', '.bmp': 'image/bmp',
            }
            info['mime_type'] = mime_map.get(ext)

        return info
