#!/usr/bin/env python3
"""
Example of using multimodal capabilities with MultimodalConverter.

Demonstrates creating a multimodal ContextMessage with text and images,
converting to OpenAI format and Agents SDK format.
"""

import logging
from datetime import datetime
from pathlib import Path

# Assuming the project structure
from utils.multimodal_converter import MultimodalConverter
try:
    from schemas.schemas import ContextMessage
except ImportError:
    print("Note: schemas.schemas not available. Install dependencies or adjust imports.")
    ContextMessage = None

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def main():
    converter = MultimodalConverter()
    
    # Example image sources: local file path, URL, or base64
    image_sources = [
        # Replace with a real image path or URL
        "https://example.com/sample-image.jpg",  # URL example
        # "path/to/local/image.jpg",  # Local file example
        # "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD..."  # Base64 example
    ]
    
    # Create multimodal message
    timestamp = datetime.now().isoformat()
    multimodal_msg = converter.create_multimodal_message(
        role="user",
        text="Please describe this image in detail.",
        image_sources=image_sources,
        timestamp=timestamp
    )
    
    print("Created ContextMessage:")
    print(f"Role: {multimodal_msg.role}")
    print(f"Timestamp: {multimodal_msg.timestamp}")
    print(f"Content type: {type(multimodal_msg.content)}")
    if hasattr(multimodal_msg.content, '__len__'):
        print(f"Content parts: {len(multimodal_msg.content)}")
    
    # Convert to OpenAI format
    openai_msg = converter.context_message_to_openai(multimodal_msg)
    print("\nOpenAI format:")
    import json
    print(json.dumps(openai_msg, indent=2))
    
    # Convert to Agents SDK format
    sdk_content = converter.context_message_to_agents_sdk(multimodal_msg)
    print("\nAgents SDK format:")
    print(json.dumps(sdk_content, indent=2))
    
    # Check if has images
    has_images = converter.has_images(multimodal_msg.content)
    print(f"\nHas images: {has_images}")
    
    # Extract text
    text_only = converter.extract_text_from_multimodal(multimodal_msg.content)
    print(f"Extracted text: {text_only}")

if __name__ == "__main__":
    main()