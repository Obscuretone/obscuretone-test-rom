import hashlib
import json
import runpy
import sys
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

import generate
import verify


def _build(tmp_path: Path) -> Path:
    output = tmp_path / "dist"
    generate.write_outputs(output)
    return output


def _manifest(output: Path) -> dict[str, object]:
    decoded: object = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    return cast("dict[str, object]", decoded)


def _write_manifest(output: Path, manifest: dict[str, object]) -> None:
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )


def _repair_global_checksum(image: bytearray) -> None:
    checksum = (sum(image[:0x14E]) + sum(image[0x150:])) & 0xFFFF
    image[0x14E:0x150] = checksum.to_bytes(2, "big")


def test_verifier_accepts_generated_bundle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = _build(tmp_path)

    verify.verify_bundle(output)
    assert verify.main((str(output),)) == 0
    assert "verified 18 ROMs" in capsys.readouterr().out


def test_verifier_module_entrypoint(tmp_path: Path) -> None:
    output = _build(tmp_path)
    with (
        patch.object(sys, "argv", ["verify.py", str(output)]),
        pytest.raises(SystemExit) as exit_info,
    ):
        runpy.run_path(verify.__file__, run_name="__main__")
    assert exit_info.value.code == 0


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", 3, "schema"),
        ("rom", "wrong.gb", "core ROM"),
        ("protocol", "OTHER/1", "protocol"),
        ("targets", ["dmg"], "target ledger"),
        ("base_opcodes", [], "base opcode"),
        ("cb_opcodes", [], "CB opcode"),
        ("core_base_opcodes", [], "execution ledger"),
        ("dedicated_probes", {}, "probe ledger"),
        ("subsystems", [], "subsystem ledger"),
        ("code_end", "nope", "must be an integer"),
        ("code_end", 0x150, "out of range"),
    ],
)
def test_verifier_rejects_invalid_manifest_contract(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    output = _build(tmp_path)
    manifest = _manifest(output)
    manifest[field] = value
    _write_manifest(output, manifest)

    with pytest.raises(verify.VerificationError, match=message):
        verify.verify_bundle(output)


@pytest.mark.parametrize(
    ("relative_path", "offset", "value", "message"),
    [
        ("otr.gb", 0x100, 0x00, "entry point"),
        ("otr.gb", 0x104, 0xCE, "logo data"),
        ("otr.gb", 0x134, 0x00, "title"),
        ("otr-cgb.gb", 0x143, 0x00, "CGB flag"),
        ("otr.gb", 0x147, 0x00, "cartridge type"),
        ("otr-cgb.gb", 0x147, 0x08, "cartridge type"),
        ("otr.gb", 0x148, 0x01, "ROM size code"),
        ("otr.gb", 0x149, 0x00, "RAM size code"),
        ("otr.gb", 0x14D, 0x00, "header checksum"),
        ("otr.gb", 0x14E, 0x00, "global checksum"),
        ("probes/10.gb", 0x150, 0x00, "probe opcode"),
        ("probes/10.gb", 0x152, 0x00, "safety loop"),
    ],
)
def test_verifier_rejects_invalid_rom_structure(
    tmp_path: Path,
    relative_path: str,
    offset: int,
    value: int,
    message: str,
) -> None:
    output = _build(tmp_path)
    artifact = output / relative_path
    image = bytearray(artifact.read_bytes())
    image[offset] = value
    if offset >= verify.CODE_START:
        _repair_global_checksum(image)
    artifact.write_bytes(image)

    with pytest.raises(verify.VerificationError, match=message):
        verify.verify_bundle(output)


def test_verifier_rejects_bad_file_sets_and_unreadable_manifest(
    tmp_path: Path,
) -> None:
    output = _build(tmp_path)
    (output / "probes" / "10.gb").unlink()
    with pytest.raises(verify.VerificationError, match="file set"):
        verify.verify_bundle(output)

    generate.write_outputs(output)
    (output / "extra.gb").write_bytes(b"")
    with pytest.raises(verify.VerificationError, match="file set"):
        verify.verify_bundle(output)

    (output / "extra.gb").unlink()
    (output / "manifest.json").write_text("not JSON", encoding="utf-8")
    with pytest.raises(verify.VerificationError, match="cannot read"):
        verify.verify_bundle(output)

    (output / "manifest.json").write_text("[]", encoding="utf-8")
    with pytest.raises(verify.VerificationError, match="must be an object"):
        verify.verify_bundle(output)


def test_verifier_rejects_incomplete_artifact_metadata(tmp_path: Path) -> None:
    output = _build(tmp_path)
    manifest = _manifest(output)
    manifest["controller_roms"] = {}
    _write_manifest(output, manifest)
    with pytest.raises(verify.VerificationError, match="controller manifest"):
        verify.verify_bundle(output)

    manifest = _manifest(_build(tmp_path))
    manifest["probe_roms"] = {}
    _write_manifest(output, manifest)
    with pytest.raises(verify.VerificationError, match="probe manifest"):
        verify.verify_bundle(output)


def test_verifier_rejects_stale_size_and_hash(tmp_path: Path) -> None:
    output = _build(tmp_path)
    manifest = _manifest(output)
    manifest["size"] = 1
    _write_manifest(output, manifest)
    with pytest.raises(verify.VerificationError, match="size is stale"):
        verify.verify_bundle(output)

    generate.write_outputs(output)
    core_path = output / "otr.gb"
    image = bytearray(core_path.read_bytes())
    image[-1] ^= 0xFF
    _repair_global_checksum(image)
    core_path.write_bytes(image)
    with pytest.raises(verify.VerificationError, match="hash is stale"):
        verify.verify_bundle(output)


def test_verifier_requires_protocol_marker(tmp_path: Path) -> None:
    output = _build(tmp_path)
    core_path = output / "otr.gb"
    image = bytearray(core_path.read_bytes())
    marker = image.index(b"OTR/1")
    image[marker : marker + 5] = b"BAD/1"
    _repair_global_checksum(image)
    core_path.write_bytes(image)

    manifest = _manifest(output)
    manifest["sha256"] = hashlib.sha256(image).hexdigest()
    _write_manifest(output, manifest)

    with pytest.raises(verify.VerificationError, match="protocol marker"):
        verify.verify_bundle(output)


def test_verifier_cli_reports_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert verify.main((str(tmp_path / "missing"),)) == 1
    assert "OTR verification failed" in capsys.readouterr().err
