"""Independently verify generated Obscuretone Test ROM artifacts.

This module intentionally does not import the generator. It duplicates the
public cartridge and manifest contract so a generator bug cannot redefine the
rules used to accept its own output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

HEADER_ENTRY: Final = bytes((0xC3, 0x50, 0x01, 0x00))
HEADER_LOGO: Final = slice(0x104, 0x134)
HEADER_TITLE: Final = slice(0x134, 0x143)
HEADER_CHECKSUM: Final = 0x14D
GLOBAL_CHECKSUM: Final = 0x14E
CODE_START: Final = 0x150

ILLEGAL_BASE_OPCODES: Final = frozenset(
    {
        0xD3,
        0xDB,
        0xDD,
        0xE3,
        0xE4,
        0xEB,
        0xEC,
        0xED,
        0xF4,
        0xFC,
        0xFD,
    }
)
LEGAL_BASE_OPCODES: Final = frozenset(range(0x100)) - ILLEGAL_BASE_OPCODES
PROBE_OPCODES: Final = (0x10, *sorted(ILLEGAL_BASE_OPCODES))
EXPECTED_SUBSYSTEMS: Final = (
    "cpu",
    "interrupts",
    "timer",
    "memory bus",
    "wram/echo",
    "hram",
    "cartridge ram",
    "joypad",
    "serial",
    "oam dma",
    "ppu registers",
    "apu registers",
)
EXPECTED_CGB_SUBSYSTEMS: Final = (
    *EXPECTED_SUBSYSTEMS,
    "cgb boot mode",
    "cgb wram banking",
    "cgb vram banking",
    "cgb bg map attributes",
    "cgb palette ram",
    "cgb general-purpose vram dma",
    "cgb hblank vram dma",
    "cgb double-speed mode",
    "cgb fast serial",
    "cgb infrared port",
    "cgb object priority",
    "cgb pcm outputs",
)


class VerificationError(ValueError):
    """A generated artifact violates OTR's public build contract."""


@dataclass(frozen=True)
class RomSpec:
    """Expected cartridge-header fields for one generated ROM."""

    relative_path: str
    title: str
    size: int
    cartridge_type: int
    rom_size_code: int
    ram_size_code: int
    probe_opcode: int | None = None
    cgb_flag: int = 0x00


ROM_SPECS: Final = (
    RomSpec("otr.gb", "OTR-DIAGNOSTIC", 32 * 1024, 0x1A, 0x00, 0x02),
    RomSpec(
        "otr-cgb.gb",
        "OTR-CGB-DIAG",
        32 * 1024,
        0x1A,
        0x00,
        0x02,
        cgb_flag=0xC0,
    ),
    RomSpec("controllers/mbc1.gb", "OTR-MBC1", 64 * 1024, 0x03, 0x01, 0x03),
    RomSpec("controllers/mbc2.gb", "OTR-MBC2", 64 * 1024, 0x06, 0x01, 0x00),
    RomSpec("controllers/mbc3.gb", "OTR-MBC3", 64 * 1024, 0x10, 0x01, 0x03),
    RomSpec(
        "controllers/mbc5.gb",
        "OTR-MBC5",
        8 * 1024 * 1024,
        0x1B,
        0x08,
        0x04,
    ),
    *(
        RomSpec(
            f"probes/{opcode:02x}.gb",
            f"OTR-PROBE-{opcode:02X}",
            32 * 1024,
            0x00,
            0x00,
            0x02,
            opcode,
        )
        for opcode in PROBE_OPCODES
    ),
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def _mapping(value: object, label: str) -> dict[str, object]:
    _require(isinstance(value, Mapping), f"{label} must be an object")
    mapping = cast("Mapping[object, object]", value)
    _require(
        all(isinstance(key, str) for key in mapping),
        f"{label} contains a non-string key",
    )
    return cast("dict[str, object]", dict(mapping))


def _string(value: object, label: str) -> str:
    _require(isinstance(value, str), f"{label} must be a string")
    return cast("str", value)


def _integer(value: object, label: str) -> int:
    _require(
        isinstance(value, int) and not isinstance(value, bool),
        f"{label} must be an integer",
    )
    return cast("int", value)


def _load_manifest(path: Path) -> dict[str, object]:
    try:
        decoded: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise VerificationError(f"cannot read {path}: {error}") from error
    return _mapping(decoded, "manifest")


def _sha256(image: bytes) -> str:
    return hashlib.sha256(image).hexdigest()


def _header_checksum(image: bytes) -> int:
    checksum = 0
    for value in image[HEADER_TITLE.start : HEADER_CHECKSUM]:
        checksum = (checksum - value - 1) & 0xFF
    return checksum


def _global_checksum(image: bytes) -> int:
    return (sum(image[:GLOBAL_CHECKSUM]) + sum(image[GLOBAL_CHECKSUM + 2 :])) & 0xFFFF


def _verify_header(spec: RomSpec, image: bytes) -> None:
    label = spec.relative_path
    _require(len(image) == spec.size, f"{label} has the wrong size")
    _require(image[0x100:0x104] == HEADER_ENTRY, f"{label} has a bad entry point")
    _require(
        not any(image[HEADER_LOGO]),
        f"{label} unexpectedly contains Nintendo-owned logo data",
    )
    expected_title = spec.title.encode("ascii").ljust(15, b"\0")
    _require(image[HEADER_TITLE] == expected_title, f"{label} has the wrong title")
    _require(image[0x143] == spec.cgb_flag, f"{label} has the wrong CGB flag")
    _require(
        image[0x147] == spec.cartridge_type,
        f"{label} has the wrong cartridge type",
    )
    _require(
        image[0x148] == spec.rom_size_code,
        f"{label} has the wrong ROM size code",
    )
    _require(
        image[0x149] == spec.ram_size_code,
        f"{label} has the wrong RAM size code",
    )
    _require(
        image[HEADER_CHECKSUM] == _header_checksum(image),
        f"{label} has a bad header checksum",
    )
    actual_global = int.from_bytes(
        image[GLOBAL_CHECKSUM : GLOBAL_CHECKSUM + 2],
        "big",
    )
    _require(
        actual_global == _global_checksum(image),
        f"{label} has a bad global checksum",
    )
    if spec.probe_opcode is not None:
        _require(
            image[CODE_START] == spec.probe_opcode,
            f"{label} does not execute its declared probe opcode",
        )
        _require(
            image[CODE_START + 2 : CODE_START + 5] == bytes((0xC3, 0x52, 0x01)),
            f"{label} does not contain its post-probe safety loop",
        )


def _verify_manifest_contract(manifest: dict[str, object]) -> None:
    _require(manifest.get("schema_version") == 2, "unsupported manifest schema")
    _require(manifest.get("rom") == "otr.gb", "manifest names the wrong core ROM")
    _require(manifest.get("protocol") == "OTR/1", "manifest protocol is not OTR/1")
    _require(manifest.get("targets") == ["dmg", "cgb"], "target ledger is incomplete")
    expected_base = [f"{opcode:02X}" for opcode in sorted(LEGAL_BASE_OPCODES)]
    expected_cb = [f"CB {opcode:02X}" for opcode in range(0x100)]
    expected_core = [f"{opcode:02X}" for opcode in sorted(LEGAL_BASE_OPCODES - {0x10})]
    _require(
        manifest.get("base_opcodes") == expected_base,
        "base opcode ledger is incomplete",
    )
    _require(
        manifest.get("cb_opcodes") == expected_cb,
        "CB opcode ledger is incomplete",
    )
    _require(
        manifest.get("core_base_opcodes") == expected_core,
        "core execution ledger is incomplete",
    )
    expected_probes = {
        "10": "STOP probe",
        **{
            f"{opcode:02X}": "illegal-opcode lockup probe"
            for opcode in sorted(ILLEGAL_BASE_OPCODES)
        },
    }
    _require(
        manifest.get("dedicated_probes") == expected_probes,
        "dedicated probe ledger is incomplete",
    )
    _require(
        manifest.get("subsystems") == list(EXPECTED_SUBSYSTEMS),
        "subsystem ledger is incomplete",
    )
    code_end = _integer(manifest.get("code_end"), "manifest code_end")
    _require(CODE_START < code_end <= 0x8000, "manifest code_end is out of range")
    cgb = _mapping(manifest.get("cgb_rom"), "cgb_rom")
    _require(cgb.get("rom") == "otr-cgb.gb", "manifest names the wrong CGB ROM")
    _require(
        cgb.get("base_opcodes") == expected_base,
        "CGB base opcode ledger is incomplete",
    )
    _require(
        cgb.get("cb_opcodes") == expected_cb,
        "CGB CB opcode ledger is incomplete",
    )
    _require(
        cgb.get("subsystems") == list(EXPECTED_CGB_SUBSYSTEMS),
        "CGB subsystem ledger is incomplete",
    )
    cgb_code_end = _integer(cgb.get("code_end"), "cgb_rom code_end")
    _require(CODE_START < cgb_code_end <= 0x8000, "CGB code_end is out of range")


def _declared_artifact(
    manifest: dict[str, object],
    spec: RomSpec,
) -> tuple[int, str]:
    if spec.relative_path == "otr.gb":
        return (
            _integer(manifest.get("size"), "core size"),
            _string(manifest.get("sha256"), "core sha256"),
        )
    if spec.relative_path == "otr-cgb.gb":
        cgb = _mapping(manifest.get("cgb_rom"), "cgb_rom")
        return (
            _integer(cgb.get("size"), "cgb_rom.size"),
            _string(cgb.get("sha256"), "cgb_rom.sha256"),
        )
    if spec.relative_path.startswith("controllers/"):
        controllers = _mapping(manifest.get("controller_roms"), "controller_roms")
        name = Path(spec.relative_path).name
        metadata = _mapping(controllers.get(name), f"controller_roms.{name}")
        return (
            _integer(metadata.get("size"), f"controller_roms.{name}.size"),
            _string(metadata.get("sha256"), f"controller_roms.{name}.sha256"),
        )
    probes = _mapping(manifest.get("probe_roms"), "probe_roms")
    name = Path(spec.relative_path).name
    return spec.size, _string(probes.get(name), f"probe_roms.{name}")


def verify_bundle(output: Path) -> None:
    """Verify every generated file, header, hash, and coverage ledger."""
    manifest = _load_manifest(output / "manifest.json")
    _verify_manifest_contract(manifest)

    expected_paths = {spec.relative_path for spec in ROM_SPECS}
    actual_paths = {
        artifact.relative_to(output).as_posix() for artifact in output.rglob("*.gb")
    }
    _require(actual_paths == expected_paths, "generated ROM file set is incomplete")

    controller_names = {
        Path(spec.relative_path).name
        for spec in ROM_SPECS
        if spec.relative_path.startswith("controllers/")
    }
    probe_names = {
        Path(spec.relative_path).name
        for spec in ROM_SPECS
        if spec.relative_path.startswith("probes/")
    }
    _require(
        set(_mapping(manifest.get("controller_roms"), "controller_roms"))
        == controller_names,
        "controller manifest file set is incomplete",
    )
    _require(
        set(_mapping(manifest.get("probe_roms"), "probe_roms")) == probe_names,
        "probe manifest file set is incomplete",
    )

    for spec in ROM_SPECS:
        artifact = output / spec.relative_path
        image = artifact.read_bytes()
        _verify_header(spec, image)
        declared_size, declared_sha256 = _declared_artifact(manifest, spec)
        _require(declared_size == len(image), f"{spec.relative_path} size is stale")
        _require(
            declared_sha256 == _sha256(image),
            f"{spec.relative_path} hash is stale",
        )

    core = (output / "otr.gb").read_bytes()
    _require(b"OTR/1" in core, "core ROM does not contain the OTR/1 protocol marker")
    cgb_core = (output / "otr-cgb.gb").read_bytes()
    _require(
        b"OTR/1" in cgb_core,
        "CGB core ROM does not contain the OTR/1 protocol marker",
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse verifier command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        default=Path(__file__).parent / "dist",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run verification and return a process exit status."""
    args = parse_args(argv)
    try:
        verify_bundle(args.output)
    except (OSError, VerificationError) as error:
        print(f"OTR verification failed: {error}", file=sys.stderr)
        return 1
    print(f"verified {len(ROM_SPECS)} ROMs in {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
