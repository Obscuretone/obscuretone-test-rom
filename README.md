# PyGameBoy Test ROM

PyGameBoy Test ROM is a first-party, self-checking diagnostic for the DMG Game
Boy. It is built from a dependency-free Python generator, shows its current test
on the emulated display, streams structured progress over the serial port, and
halts at the first failure.

This is its own test system, not a wrapper around Blargg, Mooneye, or another
compatibility suite. Its public reporting contract is the versioned `PYGB/1`
protocol.

## What it tests

The generated suite contains:

- a 32 KiB core ROM that executes every runnable legal base opcode and all 256
  CB-prefixed opcodes;
- a dedicated STOP probe and one deterministic probe for each of the 11
  undefined opcodes;
- CPU-visible checks for flags, loads, ALU operations, stack and control flow,
  interrupts, timer, WRAM/echo/HRAM, cartridge RAM, joypad selection, serial,
  OAM DMA, PPU state/rendering, and all four APU channels;
- MBC1, MBC2, MBC3/RTC, and MBC5 controller ROMs, including MBC5's ninth ROM
  bank bit and banked cartridge RAM;
- a machine-readable manifest with SHA-256 hashes and exact opcode/subsystem
  coverage.

“Every opcode” means every legal encoding is executed and checked by the suite.
It does not mean all possible register and memory states for every encoding;
that state space is effectively unbounded once timing and peripherals are
included. The generator uses independent flag/result oracles and deliberately
chosen boundary vectors.

Some hardware behavior cannot be observed by software running inside a
cartridge. The PyGameBoy host harness therefore also verifies the rendered
framebuffer, produced audio buffer, STOP state, undefined-opcode behavior, and
the exact set of dispatched opcodes.

## Build

Python 3.10 or newer is the only build dependency:

```bash
python generate.py --output dist
python generate.py --output dist --check
```

The output contains the core ROM, controller ROMs, opcode probes, and
`manifest.json`. The large MBC5 image is mostly zero-filled and compresses well
in release archives.

Generated images intentionally leave the Nintendo logo area blank. They run in
emulators that skip the original boot ROM, including PyGameBoy. A physical DMG
or an emulator configured to run Nintendo's boot ROM will reject them during
the logo check; the project does not redistribute Nintendo-owned logo data.

## Live display and failure behavior

The first background row shows the current group, for example:

```text
20 CB OPCODES
```

Success displays `PASS`. A failure displays `ERROR`, reports the exact
`group:case` code over serial, changes the mailbox to failure, displays `FAIL`,
disables interrupts, and executes HALT. Tests never continue after a failed
assertion.

## `PYGB/1` protocol

Serial output is line-oriented ASCII:

```text
PYGB/1 01 CPU LOAD MATRIX
PYGB/1 20 CB OPCODES
PYGB/1 PASS
```

On failure:

```text
PYGB/1 ERROR
AT 20:7C
PYGB/1 FAIL
```

The core ROM also exposes a memory mailbox in cartridge RAM:

| Address | Meaning |
|---|---|
| `$A000-$A003` | ASCII `PYGB` |
| `$A004` | `$7E` running, `$00` pass, `$E0` fail |
| `$A005` | Current or failing group |
| `$A006` | Current or failing case/opcode |
| `$A010...` | NUL-terminated result text |

The controller ROMs report over serial only so their tests can freely exercise
every external-RAM bank.

## License

The generator and original ROM content are MIT licensed. Nintendo, Game Boy,
and related marks belong to their respective owners.
