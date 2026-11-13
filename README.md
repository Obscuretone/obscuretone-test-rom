# Obscuretone Test ROM (OTR)

[![Build OTR .gb ROMs](https://github.com/Obscuretone/obscuretone-test-rom/actions/workflows/ci.yml/badge.svg)](https://github.com/Obscuretone/obscuretone-test-rom/actions/workflows/ci.yml)

Read the launch story on the
[Obscuretone blog](https://obscuretone.com/posts/en/obscuretone_test_rom).

Obscuretone Test ROM is a self-checking diagnostic for the original Game Boy
and Game Boy Color. It is built from a dependency-free Python generator, shows
its current test on the emulated display, streams structured progress over the
serial port, and halts at the first failure.

This is its own test system, not a wrapper around Blargg, Mooneye, or another
compatibility suite. Its public reporting contract is the versioned `OTR/1`
protocol.

## What it tests

The generated suite contains:

- a 32 KiB DMG core ROM and a native CGB-only core ROM; both execute every
  runnable legal base opcode and all 256 CB-prefixed opcodes;
- a dedicated STOP probe and one deterministic probe for each of the 11
  undefined opcodes;
- CPU-visible checks for flags, loads, ALU operations, stack and control flow,
  interrupts, timer, WRAM/echo/HRAM, cartridge RAM, joypad selection, serial,
  OAM DMA, PPU state/rendering, and all four APU channels;
- CGB-only checks for startup mode, banked WRAM and VRAM, tile-map attributes,
  BG/OBJ palette RAM, general-purpose and HBlank VRAM DMA, double-speed mode,
  fast serial, infrared controls, object-priority selection, and PCM channel
  outputs;
- MBC1, MBC2, MBC3/RTC, and MBC5 controller ROMs, including MBC5's ninth ROM
  bank bit and banked cartridge RAM;
- a machine-readable manifest with SHA-256 hashes and exact opcode/subsystem
  coverage.

“Every opcode” means every legal encoding is executed and checked by the suite.
It does not mean all possible register and memory states for every encoding;
that state space is effectively unbounded once timing and peripherals are
included. The generator uses independent flag/result oracles and deliberately
chosen boundary vectors.

CGB speed checks measure CPU divider ticks across four PPU scanlines. Fast
serial must remain busy initially and then complete within a bounded CPU-clock
window in both normal and double-speed mode. PCM tests activate each channel
separately with a distinct amplitude, check its nibble in PCM12/PCM34, and
require silence after its DAC is disabled. Register readback alone does not
establish timing, rendering, or peripheral accuracy; infrared and object-priority
selection currently have register-level checks only.

Some hardware behavior cannot be observed by software running inside a
cartridge. Emulator host harnesses can additionally verify the rendered
framebuffer, produced audio buffer, STOP state, undefined-opcode behavior, and
the exact set of dispatched opcodes.

## Build

Python 3.10 or newer is the only build dependency:

```bash
python -m pip install .
otr-build --output dist
otr-verify dist
otr-build --output dist --check
```

The output contains `otr.gb` for DMG, `otr-cgb.gb` for native CGB mode,
controller ROMs, opcode probes, and `manifest.json`. The large MBC5 image is
mostly zero-filled and compresses well in release archives.

GitHub Actions builds the same files on every push and pull request. The
**Build OTR .gb ROMs** workflow can also be run manually from the Actions tab;
its **otr-gb-roms** artifact contains `otr.gb`, `otr-cgb.gb`, all controller and
opcode-probe ROMs, and `manifest.json`. Version tags build and verify the ROMs
again before the release archive is published.

## Toolchain integrity

OTR is Python-created by design. The production build does not invoke a C
compiler or assembler: its label-aware LR35902 emitter writes exact instruction
bytes and records every emitted opcode. This prevents a compiler from choosing
different instructions than the ones the suite is meant to exercise.

The verifier is deliberately separate from the generator and does not import
it. It checks the complete artifact set, cartridge headers, entry points,
cartridge types, ROM and RAM size codes, header and global checksums, probe
instructions, protocol marker, opcode ledgers, sizes, and SHA-256 hashes.

Python 3.10 through 3.14 each build the complete suite in CI, and every
resulting file must compare byte-for-byte across all five builds.

CI also executes both core ROMs in pinned SameBoy `v1.0.3` (commit
`208ba4afabffab9edde416f2dbb8ae459e34adb8`). `tools/reference.c` is a small
host runner linked to SameBoy's library, with an original boot stub that skips
the logo check. It is a validation dependency; ROM production remains entirely
Python. This independent runtime check supplements artifact verification and
does not claim physical-hardware validation.

The Python source is formatted and linted with Ruff, checked with strict mypy,
and tested at 100 percent statement and branch coverage. GitHub Actions are
pinned to immutable commits, dependency updates are automated, and tagged
release archives receive GitHub build-provenance attestations.

For development:

```bash
python -m pip install -e ".[dev]"
python -m ruff format --check .
python -m ruff check .
python -m mypy
pytest
```

Generated images intentionally leave the Nintendo logo area blank. They run in
emulators that skip the original boot ROM. A physical DMG/CGB or an emulator
configured to run Nintendo's boot ROM will reject them during the logo check;
the project does not redistribute Nintendo-owned logo data.

## Live display and failure behavior

The first background row shows the current group, for example:

```text
20 CB OPCODES
```

Success displays `PASS`. A failure displays `ERROR`, reports the exact
`group:case` code over serial, changes the mailbox to failure, displays `FAIL`,
disables interrupts, and executes HALT. Tests never continue after a failed
assertion.

## `OTR/1` protocol

Serial output is line-oriented ASCII:

```text
OTR/1 01 CPU LOAD MATRIX
OTR/1 20 CB OPCODES
OTR/1 PASS
```

On failure:

```text
OTR/1 ERROR
AT 20:7C
OTR/1 FAIL
```

The core ROM also exposes a memory mailbox in cartridge RAM:

| Address | Meaning |
|---|---|
| `$A000-$A003` | ASCII `OTR1` |
| `$A004` | `$7E` running, `$00` pass, `$E0` fail |
| `$A005` | Current or failing group |
| `$A006` | Current or failing case/opcode |
| `$A010...` | NUL-terminated result text |

Both core ROMs use a defined MBC5+RAM cartridge layout and explicitly enable
their 8 KiB SRAM before publishing the mailbox.

The controller ROMs report over serial only so their tests can freely exercise
every external-RAM bank.

## License

The generator and original ROM content are MIT licensed. Nintendo, Game Boy,
and related marks belong to their respective owners.
