"""
Document Tools - tools for converting and exporting documents.
The agent uses these tools to transform formats and generate reports.
"""
import logging
import json
from pathlib import Path
from typing import Any, Optional
from datetime import datetime

from agents import function_tool, RunContextWrapper
from agents.tool import ToolOutputText
from utils.path_utils import display_agent_path_from_ctx, resolve_agent_path_from_ctx

logger = logging.getLogger("tools.document")


def get_user_workspace(ctx: RunContextWrapper[Any]) -> Path:
    """Gets the user workspace directory."""
    import os

    user_id = "default"
    if hasattr(ctx, 'user_id'):
        user_id = str(ctx.user_id)
    elif hasattr(ctx, 'context'):
        # Check GridRunContext
        if hasattr(ctx.context, 'user_id') and ctx.context.user_id:
            user_id = str(ctx.context.user_id)
        # Check metadata
        elif hasattr(ctx.context, 'metadata'):
            metadata = ctx.context.metadata
            if isinstance(metadata, dict) and 'user_id' in metadata:
                user_id = str(metadata['user_id'])

    workspace_env = os.environ.get("WORKSPACE_ROOT", "./workspace")
    workspace_root = Path(workspace_env).resolve()
    user_workspace = workspace_root / f"user_{user_id}"
    user_workspace.mkdir(parents=True, exist_ok=True)

    return user_workspace


@function_tool
async def markdown_to_html(
    ctx: RunContextWrapper[Any],
    markdown_content: str,
    output_filename: str,
    title: str = "Document"
) -> ToolOutputText:
    """
    Converts Markdown to an HTML file.

    Args:
        markdown_content: Markdown content to convert
        output_filename: Output file name (e.g. "report.html")
        title: HTML document title

    Returns:
        Path to the created HTML file
    """
    try:
        import markdown

        # Generate HTML
        html_body = markdown.markdown(
            markdown_content,
            extensions=['tables', 'fenced_code', 'codehilite']
        )

        # HTML wrapper
        html_template = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            line-height: 1.6;
            max-width: 900px;
            margin: 0 auto;
            padding: 20px;
            color: #333;
        }}
        h1, h2, h3 {{ color: #2c3e50; }}
        code {{
            background: #f4f4f4;
            padding: 2px 6px;
            border-radius: 3px;
            font-family: 'Courier New', monospace;
        }}
        pre {{
            background: #f4f4f4;
            padding: 15px;
            border-radius: 5px;
            overflow-x: auto;
        }}
        table {{
            border-collapse: collapse;
            width: 100%;
            margin: 20px 0;
        }}
        th, td {{
            border: 1px solid #ddd;
            padding: 12px;
            text-align: left;
        }}
        th {{
            background-color: #f2f2f2;
            font-weight: bold;
        }}
        img {{
            max-width: 100%;
            height: auto;
        }}
    </style>
</head>
<body>
{html_body}
</body>
</html>
"""

        # Save file
        user_workspace = get_user_workspace(ctx)
        output_path = user_workspace / output_filename

        output_path.write_text(html_template, encoding='utf-8')
        visible_output = output_path.name
        logger.info(f"✅ Created HTML: {visible_output}")

        return ToolOutputText(text=f"✅ HTML file created: {visible_output}")

    except ImportError:
        return ToolOutputText(text="❌ Need to install 'markdown' library: pip install markdown")
    except Exception as e:
        logger.error(f"HTML conversion failed: {e}", exc_info=True)
        return ToolOutputText(text=f"❌ Conversion error: {str(e)}")


@function_tool
async def markdown_to_pdf(
    ctx: RunContextWrapper[Any],
    markdown_content: str,
    output_filename: str,
    title: str = "Document"
) -> ToolOutputText:
    """
    Converts Markdown to PDF via HTML.

    Args:
        markdown_content: Markdown content to convert
        output_filename: Output file name (e.g. "report.pdf")
        title: Document title

    Returns:
        Path to the created PDF file
    """
    try:
        import markdown
        from weasyprint import HTML
        from io import BytesIO

        # Generate HTML
        html_body = markdown.markdown(
            markdown_content,
            extensions=['tables', 'fenced_code']
        )

        html_template = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>{title}</title>
    <style>
        @page {{ size: A4; margin: 2cm; }}
        body {{
            font-family: 'DejaVu Sans', Arial, sans-serif;
            line-height: 1.6;
            color: #333;
        }}
        h1 {{ color: #2c3e50; font-size: 24pt; }}
        h2 {{ color: #34495e; font-size: 18pt; }}
        h3 {{ color: #34495e; font-size: 14pt; }}
        code {{
            background: #f4f4f4;
            padding: 2px 6px;
            font-family: 'Courier New', monospace;
        }}
        pre {{
            background: #f4f4f4;
            padding: 15px;
            border-radius: 5px;
            font-size: 10pt;
        }}
        table {{
            border-collapse: collapse;
            width: 100%;
            margin: 10px 0;
        }}
        th, td {{
            border: 1px solid #ddd;
            padding: 8px;
            text-align: left;
        }}
        th {{ background-color: #f2f2f2; }}
    </style>
</head>
<body>
{html_body}
</body>
</html>
"""

        # Generate PDF
        user_workspace = get_user_workspace(ctx)
        output_path = user_workspace / output_filename

        html_doc = HTML(string=html_template)
        html_doc.write_pdf(output_path)

        visible_output = output_path.name
        logger.info(f"✅ Created PDF: {visible_output}")
        return ToolOutputText(text=f"✅ PDF file created: {visible_output}")

    except ImportError as e:
        return ToolOutputText(
            text=f"❌ Required libraries: pip install markdown weasyprint\n"
            f"Also need GTK3 runtime for WeasyPrint (see documentation)"
        )
    except Exception as e:
        logger.error(f"PDF conversion failed: {e}", exc_info=True)
        return ToolOutputText(text=f"❌ PDF conversion error: {str(e)}")


@function_tool
async def save_report(
    ctx: RunContextWrapper[Any],
    content: str,
    filename: str,
    format: str = "md"
) -> ToolOutputText:
    """
    Saves a report in the specified format.

    Args:
        content: Report content
        filename: File name without extension
        format: File format (md, txt, html, json)

    Returns:
        Path to the saved file
    """
    try:
        user_workspace = get_user_workspace(ctx)

        # Add timestamp to filename
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        full_filename = f"{filename}_{timestamp}.{format}"
        output_path = user_workspace / full_filename

        if format == "json":
            # For JSON, try to parse the content
            try:
                data = json.loads(content)
                output_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            except json.JSONDecodeError:
                # If not JSON, save as-is in a wrapper
                data = {"content": content, "timestamp": timestamp}
                output_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        else:
            output_path.write_text(content, encoding='utf-8')

        visible_output = output_path.name
        logger.info(f"✅ Saved report: {visible_output}")
        return ToolOutputText(text=f"✅ Report saved: {visible_output}")

    except Exception as e:
        logger.error(f"Save report failed: {e}", exc_info=True)
        return ToolOutputText(text=f"❌ Error saving report: {str(e)}")


@function_tool
async def merge_reports(
    ctx: RunContextWrapper[Any],
    report_paths: list[str],
    output_filename: str,
    section_headers: Optional[list[str]] = None
) -> ToolOutputText:
    """
    Merges multiple reports into a single document.

    Args:
        report_paths: List of paths to report files
        output_filename: Output file name
        section_headers: Optional headers for each section

    Returns:
        Path to the merged report
    """
    try:
        combined_content = []
        combined_content.append(f"# Merged Report\n")
        combined_content.append(f"Date created: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

        for idx, report_path in enumerate(report_paths):
            visible_input = display_agent_path_from_ctx(report_path, ctx)
            path = Path(resolve_agent_path_from_ctx(report_path, ctx))
            if not path.exists():
                logger.warning(f"Report not found: {visible_input}")
                continue

            # Add section header
            if section_headers and idx < len(section_headers):
                header = section_headers[idx]
            else:
                header = f"Report {idx + 1}: {path.name}"

            combined_content.append(f"\n---\n\n## {header}\n\n")

            # Read and add content
            content = path.read_text(encoding='utf-8')
            combined_content.append(content)

        # Save the merged report
        user_workspace = get_user_workspace(ctx)
        output_path = user_workspace / output_filename

        output_path.write_text("".join(combined_content), encoding='utf-8')

        visible_output = output_path.name
        logger.info(f"✅ Merged {len(report_paths)} reports into: {visible_output}")
        return ToolOutputText(text=f"✅ Merged report created: {visible_output}\nFiles processed: {len(report_paths)}")

    except Exception as e:
        logger.error(f"Merge reports failed: {e}", exc_info=True)
        return ToolOutputText(text=f"❌ Error merging reports: {str(e)}")


# Exported tools
DOCUMENT_TOOLS = {
    "markdown_to_html": markdown_to_html,
    "markdown_to_pdf": markdown_to_pdf,
    "save_report": save_report,
    "merge_reports": merge_reports
}
