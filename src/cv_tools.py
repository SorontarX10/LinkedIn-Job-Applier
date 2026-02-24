from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def extract_cv_text(cv_path: Path) -> str:
    if not cv_path.exists():
        raise FileNotFoundError(f"CV file not found: {cv_path}")

    reader = PdfReader(str(cv_path))
    pages: list[str] = []
    for page in reader.pages:
        text = page.extract_text() or ""
        if text.strip():
            pages.append(text.strip())

    merged = "\n\n".join(pages).strip()
    if not merged:
        raise ValueError("Could not extract text from CV PDF.")
    return merged


def write_tailored_cv_notes(output_dir: Path, job_id: str, notes: list[str]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{job_id}_tailored_cv_notes.md"
    body = ["# Tailored CV notes", ""]
    if notes:
        for note in notes:
            body.append(f"- {note}")
    else:
        body.append("- No tailoring suggestions generated.")
    path.write_text("\n".join(body), encoding="utf-8")
    return path


def write_cover_letter(
    output_dir: Path,
    job_id: str,
    content: str,
    disclosure_text: str = "",
    add_disclosure: bool = True,
) -> Path | None:
    clean = content.strip()
    disclosure = disclosure_text.strip()

    if add_disclosure and disclosure:
        if not clean:
            clean = disclosure
        elif _normalize(disclosure) not in _normalize(clean):
            clean = f"{clean}\n\n{disclosure}"

    if not clean:
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{job_id}_cover_letter.txt"
    path.write_text(clean, encoding="utf-8")
    return path
