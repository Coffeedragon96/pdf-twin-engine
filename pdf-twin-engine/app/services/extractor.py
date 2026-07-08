import fitz
import base64
from typing import Dict, Any, List


def extract_pdf(pdf_path: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extracts pages from a PDF document and converts them to base64 encoded images.
    Also attempts to extract vector geometry from CAD-exported PDFs.

    Args:
        pdf_path:  Absolute file path to the temporary PDF document.
        metadata:  Building metadata associated with the conversion job.

    Returns:
        Dictionary containing extracted base64 images, vector geometry, and page count.
    """
    try:
        document = fitz.open(pdf_path)
    except Exception as exc:
        raise RuntimeError(f"Failed to open PDF: {exc}")

    extracted_pages  = []
    vector_geometry  = []

    for page_index in range(len(document)):
        page = document.load_page(page_index)

        # High-resolution render (2x zoom for Claude Vision clarity)
        zoom_matrix = fitz.Matrix(2.0, 2.0)
        pixmap      = page.get_pixmap(matrix=zoom_matrix, alpha=False)

        image_bytes = pixmap.tobytes("jpeg")
        image_b64   = base64.b64encode(image_bytes).decode("utf-8")

        extracted_pages.append({
            "page_index": page_index,
            "image_b64":  image_b64,
            "width":      pixmap.width,
            "height":     pixmap.height,
        })

        # Attempt vector geometry extraction (CAD/vector PDFs only)
        page_vectors = _extract_vector_geometry(page, page_index)
        if page_vectors:
            vector_geometry.extend(page_vectors)

    document.close()

    return {
        "total_pages":     len(extracted_pages),
        "pages":           extracted_pages,
        "vector_geometry": vector_geometry if vector_geometry else None,
        "metadata":        metadata,
    }


def _extract_vector_geometry(page: fitz.Page, page_index: int) -> List[Dict]:
    """
    Extracts line/rect primitives from a vector PDF page.
    Returns empty list for scanned/raster pages.
    """
    try:
        paths   = page.get_drawings()
        vectors = []

        for path in paths:
            for item in path.get("items", []):
                # "l" = line segment, "re" = rectangle
                if item[0] == "l":
                    vectors.append({
                        "page":  page_index,
                        "type":  "line",
                        "start": list(item[1]),
                        "end":   list(item[2]),
                    })
                elif item[0] == "re":
                    rect = item[1]
                    vectors.append({
                        "page":   page_index,
                        "type":   "rect",
                        "x":      rect.x0,
                        "y":      rect.y0,
                        "width":  rect.width,
                        "height": rect.height,
                    })
        return vectors

    except Exception:
        # Scanned PDFs have no vector primitives - silently skip
        return []