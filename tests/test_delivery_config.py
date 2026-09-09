from pathlib import Path

from zhenyun_pangu_mcp.config import resolve_marmot_delivery_root


def test_delivery_root_requires_configuration() -> None:
    result = resolve_marmot_delivery_root("")

    assert result["configured"] is False
    assert result["valid"] is False
    assert result["output_root"] is None


def test_delivery_root_rejects_relative_path() -> None:
    result = resolve_marmot_delivery_root("relative/deliveries")

    assert result["configured"] is True
    assert result["valid"] is False
    assert result["output_root"] is None


def test_delivery_root_accepts_existing_directory(tmp_path: Path) -> None:
    result = resolve_marmot_delivery_root(str(tmp_path))

    assert result["configured"] is True
    assert result["valid"] is True
    assert result["output_root"] == str(tmp_path.resolve())
    assert result["exists"] is True
    assert result["is_directory"] is True


def test_delivery_root_accepts_creatable_directory(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deliveries"
    result = resolve_marmot_delivery_root(str(target))

    assert result["configured"] is True
    assert result["valid"] is True
    assert result["output_root"] == str(target.resolve())
    assert result["exists"] is False
