from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from app.db.storage import get_store
from app.services.export import (
    SUPPORTED_FORMATS,
    export_project,
    filename_for,
    media_type_for,
)

router = APIRouter(prefix="/projects", tags=["export"])


@router.get("/{project_id}/export")
def export_manuscript(
    project_id: str,
    format: str = Query("markdown", alias="format"),
):
    fmt = (format or "markdown").lower().strip()
    if fmt == "md":
        fmt = "markdown"
    if fmt == "text":
        fmt = "txt"
    if fmt not in SUPPORTED_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported format. Choose one of: {', '.join(SUPPORTED_FORMATS)}",
        )

    try:
        project = get_store().get_project(project_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    try:
        data = export_project(project, fmt)
    except RuntimeError as e:
        raise HTTPException(status_code=501, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    filename = filename_for(project, fmt)
    return Response(
        content=data,
        media_type=media_type_for(fmt),
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.get("/{project_id}/export/formats")
def list_export_formats(project_id: str):
    # Validate project exists
    try:
        get_store().get_project(project_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    return {
        "formats": [
            {
                "id": "markdown",
                "label": "Markdown",
                "ext": "md",
                "description": "Portable plain text with structure",
            },
            {
                "id": "txt",
                "label": "Plain text",
                "ext": "txt",
                "description": "Simple manuscript text",
            },
            {
                "id": "html",
                "label": "HTML",
                "ext": "html",
                "description": "Printable / browser-ready",
            },
            {
                "id": "docx",
                "label": "Word (DOCX)",
                "ext": "docx",
                "description": "For editors and collaborators",
            },
            {
                "id": "manuscript-docx",
                "label": "Manuscript (DOCX)",
                "ext": "docx",
                "description": "Story prose only, no titles or front matter",
            },
            {
                "id": "epub",
                "label": "EPUB",
                "ext": "epub",
                "description": "E-reader ebook",
            },
            {
                "id": "manuscript-epub",
                "label": "Manuscript (EPUB)",
                "ext": "epub",
                "description": "Story prose only, no titles or front matter",
            },
            {
                "id": "cover-jpg",
                "label": "Cover (JPG)",
                "ext": "jpg",
                "description": "2:3 print cover image, 300 DPI",
            },
            {
                "id": "cover-tiff",
                "label": "Cover (TIFF)",
                "ext": "tiff",
                "description": "2:3 print cover image, 300 DPI",
            },
            {
                "id": "json",
                "label": "Arthur backup",
                "ext": "json",
                "description": "Full project data (chapters, cast, world)",
            },
        ]
    }
