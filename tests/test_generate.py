import json
import runpy
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import generate as generator
from generate import (
    LEGAL_BASE_OPCODES,
    Rom,
    alu_result,
    build_core_rom,
    inc_dec_flags,
    set_condition,
)


def test_generator_rejects_invalid_emitter_inputs() -> None:
    rom = Rom("ERRORS")
    rom.label("duplicate")
    with pytest.raises(ValueError, match="duplicate label"):
        rom.label("duplicate")

    too_small = Rom("SMALL", size=generator.CODE_START)
    with pytest.raises(ValueError, match="exceeds"):
        too_small.raw(0)

    with pytest.raises(ValueError, match="progress message is too long"):
        rom.announce("x" * 21)

    unknown = Rom("UNKNOWN")
    unknown.jp("missing")
    with pytest.raises(ValueError, match="unknown label"):
        unknown.patch()

    distant = Rom("DISTANT")
    distant.rel_branch(0x18, "far")
    distant.raw(*([0] * 128))
    distant.label("far")
    with pytest.raises(ValueError, match="out of range"):
        distant.patch()

    with pytest.raises(ValueError, match="invalid"):
        alu_result("invalid", 0, 0, 0)
    with pytest.raises(ValueError, match="invalid"):
        set_condition(rom, "invalid", True)

    assert inc_dec_flags(0x0F, decrement=False, carry=False) == (0x10, 0x20)


def test_generator_rejects_incomplete_opcode_ledgers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        generator,
        "LEGAL_BASE_OPCODES",
        LEGAL_BASE_OPCODES | {0xD3},
    )
    with pytest.raises(ValueError, match="missing legal base opcodes: D3"):
        build_core_rom()


def test_generator_rejects_incomplete_cb_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def emit_cb_without_recording(self: Rom, opcode: int) -> None:
        self.base_coverage.add(0xCB)
        self.raw(0xCB, opcode)

    monkeypatch.setattr(Rom, "cb", emit_cb_without_recording)
    with pytest.raises(ValueError, match="missing CB opcodes"):
        build_core_rom()


def test_generator_cli_writes_and_rejects_stale_artifacts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "generated" / "roms"
    arguments = ("--output", str(output))

    assert generator.main(arguments) == 0
    assert "wrote otr.gb" in capsys.readouterr().out
    assert generator.main((*arguments, "--check")) == 0

    image = output / "otr.gb"
    cgb_image = output / "otr-cgb.gb"
    manifest = output / "manifest.json"
    image.write_bytes(b"stale")
    assert generator.main((*arguments, "--check")) == 1
    assert "is stale" in capsys.readouterr().out

    assert generator.main(arguments) == 0
    capsys.readouterr()
    cgb_image.write_bytes(b"stale")
    assert generator.main((*arguments, "--check")) == 1
    assert "otr-cgb.gb" in capsys.readouterr().out

    assert generator.main(arguments) == 0
    capsys.readouterr()
    manifest.write_text("{}")
    assert generator.main((*arguments, "--check")) == 1
    assert "manifest.json is stale" in capsys.readouterr().out

    assert generator.main(arguments) == 0
    capsys.readouterr()
    (output / "controllers" / "mbc1.gb").unlink()
    assert generator.main((*arguments, "--check")) == 1
    assert "missing or stale" in capsys.readouterr().out

    assert generator.main(arguments) == 0
    capsys.readouterr()
    (output / "probes" / "10.gb").write_bytes(b"stale")
    assert generator.main((*arguments, "--check")) == 1
    assert "missing or stale" in capsys.readouterr().out

    missing = tmp_path / "missing"
    assert generator.main(("--output", str(missing), "--check")) == 1
    assert "artifacts are missing" in capsys.readouterr().out
    missing.mkdir()
    (missing / "otr.gb").write_bytes(b"")
    assert generator.main(("--output", str(missing), "--check")) == 1
    assert "artifacts are missing" in capsys.readouterr().out


def test_generator_module_entrypoint(tmp_path: Path) -> None:
    output = tmp_path / "entrypoint"
    with (
        patch.object(sys, "argv", ["generate.py", "--output", str(output)]),
        pytest.raises(SystemExit) as exit_info,
    ):
        runpy.run_path(generator.__file__, run_name="__main__")

    assert exit_info.value.code == 0
    assert (output / "otr.gb").is_file()
    assert (output / "otr-cgb.gb").is_file()


def test_manifest_accounts_for_every_opcode_and_subsystem() -> None:
    dmg_image, manifest = build_core_rom()
    cgb_image, cgb_manifest = build_core_rom(cgb=True)

    assert manifest["protocol"] == "OTR/1"
    assert set(manifest["base_opcodes"]) == {
        f"{opcode:02X}" for opcode in LEGAL_BASE_OPCODES
    }
    assert set(manifest["cb_opcodes"]) == {
        f"CB {opcode:02X}" for opcode in range(0x100)
    }
    assert "ppu registers" in manifest["subsystems"]
    assert "apu registers" in manifest["subsystems"]
    assert dmg_image[0x147] == 0x1A
    assert set(cgb_manifest["core_base_opcodes"]) == {
        f"{opcode:02X}" for opcode in LEGAL_BASE_OPCODES
    }
    assert cgb_image[0x143] == 0xC0
    assert cgb_image[0x147] == 0x1A
    assert "cgb double-speed mode" in cgb_manifest["subsystems"]
    assert "cgb hblank vram dma" in cgb_manifest["subsystems"]
    assert "cgb pcm outputs" in cgb_manifest["subsystems"]


def test_written_manifest_is_valid_json(tmp_path: Path) -> None:
    output = tmp_path / "dist"
    generator.write_outputs(output)

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["targets"] == ["dmg", "cgb"]
    assert manifest["cgb_rom"]["rom"] == "otr-cgb.gb"
    assert set(manifest["controller_roms"]) == {
        "mbc1.gb",
        "mbc2.gb",
        "mbc3.gb",
        "mbc5.gb",
    }
