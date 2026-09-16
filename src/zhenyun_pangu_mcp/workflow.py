"""Portable workflow guidance; no network, credentials or agent-specific APIs."""
from importlib.resources import files


GUIDE_TOPICS = ("overview", "requirement", "triage", "repair", "knowledge", "handoff")


def guide_document() -> str:
    return files("zhenyun_pangu_mcp").joinpath("workflow_guide.md").read_text(encoding="utf-8")


def get_guide(topic: str) -> str:
    if topic not in GUIDE_TOPICS:
        raise ValueError(f"Unknown workflow topic: {topic}")
    sections = guide_document().split("\n## ")[1:]
    for section in sections:
        title, _, body = section.partition("\n")
        if title == topic:
            return body.strip()
    raise RuntimeError(f"Missing packaged workflow topic: {topic}")
