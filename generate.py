"""Build PyGameBoy's first-party, self-checking DMG diagnostic ROMs.

The generator deliberately has no assembler dependency.  Its small emitter makes
the generated binaries reproducible on every Python version supported by the
project and, more importantly, keeps an exact ledger of the opcodes emitted as
instructions.  Generation fails if the core ROM does not exercise every legal
base opcode (STOP has a dedicated probe ROM) and all 256 CB-prefixed opcodes.

The ROMs use their own versioned ``PYGB/1`` serial event stream and mailbox:

    A000-A003  ASCII "PYGB"
    A004       7E while running, 00 on success, E0 on failure
    A005       current/failing group
    A006       current/failing case or opcode
    A010...    NUL-terminated result text

On failure, FF80 contains the test group and FF81 contains the opcode or case.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Iterable

ROM_SIZE: Final = 32 * 1024
CODE_START: Final = 0x0150
WRAM_VALUE: Final = 0xC100
WRAM_ACTUAL: Final = 0xC200
FAIL_GROUP: Final = 0xFF80
FAIL_CASE: Final = 0xFF81

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

REGISTER_CODES: Final = ("b", "c", "d", "e", "h", "l", "[hl]", "a")
LD_IMMEDIATE: Final = {
    "b": 0x06,
    "c": 0x0E,
    "d": 0x16,
    "e": 0x1E,
    "h": 0x26,
    "l": 0x2E,
    "a": 0x3E,
}
LD_A_REGISTER: Final = {
    "b": 0x78,
    "c": 0x79,
    "d": 0x7A,
    "e": 0x7B,
    "h": 0x7C,
    "l": 0x7D,
    "[hl]": 0x7E,
    "a": 0x7F,
}

# Five-pixel-wide glyphs expanded to native 8x8, two-bit Game Boy tiles.
FONT_5X7: Final = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01111", "10000", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("01110", "00100", "00100", "00100", "00100", "00100", "01110"),
    "J": ("00111", "00010", "00010", "00010", "10010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "10101", "01010"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    ":": ("00000", "00100", "00100", "00000", "00100", "00100", "00000"),
}


@dataclass(frozen=True)
class Fixup:
    offset: int
    label: str
    relative: bool


class Rom:
    """A minimal label-aware LR35902 byte emitter with coverage accounting."""

    def __init__(
        self,
        title: str,
        *,
        cartridge_type: int = 0x08,
        size: int = ROM_SIZE,
        rom_size_code: int = 0x00,
        ram_size_code: int = 0x02,
    ) -> None:
        self.title = title
        self.cartridge_type = cartridge_type
        self.size = size
        self.rom_size_code = rom_size_code
        self.ram_size_code = ram_size_code
        self.image = bytearray(size)
        self.pc = CODE_START
        self.labels: dict[str, int] = {}
        self.fixups: list[Fixup] = []
        self.base_coverage: set[int] = set()
        self.cb_coverage: set[int] = set()
        self.progress_messages: list[tuple[str, bytes]] = []
        self._serial = 0

    def unique(self, stem: str) -> str:
        self._serial += 1
        return f"{stem}_{self._serial}"

    def label(self, name: str) -> None:
        if name in self.labels:
            raise ValueError(f"duplicate label {name}")
        self.labels[name] = self.pc

    def raw(self, *values: int) -> None:
        end = self.pc + len(values)
        if end > self.size:
            raise ValueError(f"{self.title} exceeds {self.size} bytes")
        self.image[self.pc : end] = bytes(value & 0xFF for value in values)
        self.pc = end

    def ins(self, opcode: int, *operands: int, cover: bool = True) -> None:
        if cover:
            self.base_coverage.add(opcode)
        self.raw(opcode, *operands)

    def cb(self, opcode: int) -> None:
        self.base_coverage.add(0xCB)
        self.cb_coverage.add(opcode)
        self.raw(0xCB, opcode)

    def abs_branch(self, opcode: int, label: str) -> None:
        self.base_coverage.add(opcode)
        self.raw(opcode)
        self.fixups.append(Fixup(self.pc, label, False))
        self.raw(0, 0)

    def rel_branch(self, opcode: int, label: str) -> None:
        self.base_coverage.add(opcode)
        self.raw(opcode)
        self.fixups.append(Fixup(self.pc, label, True))
        self.raw(0)

    def ld_a(self, value: int) -> None:
        self.ins(0x3E, value)

    def ld_rr(self, opcode: int, value: int) -> None:
        self.ins(opcode, value & 0xFF, value >> 8)

    def ld_hl_label(self, label: str) -> None:
        self.base_coverage.add(0x21)
        self.raw(0x21)
        self.fixups.append(Fixup(self.pc, label, False))
        self.raw(0, 0)

    def write_a16(self, address: int) -> None:
        self.ins(0xEA, address & 0xFF, address >> 8)

    def read_a16(self, address: int) -> None:
        self.ins(0xFA, address & 0xFF, address >> 8)

    def write_value(self, address: int, value: int) -> None:
        self.ld_a(value)
        self.write_a16(address)

    def ldh_write(self, offset: int) -> None:
        self.ins(0xE0, offset)

    def ldh_read(self, offset: int) -> None:
        self.ins(0xF0, offset)

    def jp(self, label: str) -> None:
        self.abs_branch(0xC3, label)

    def call(self, label: str) -> None:
        self.abs_branch(0xCD, label)

    def fail_if(self, condition_opcode: int = 0xC2) -> None:
        self.abs_branch(condition_opcode, "Fail")

    def set_case(self, group: int, case: int) -> None:
        self.write_value(FAIL_GROUP, group)
        self.write_value(FAIL_CASE, case)
        self.write_value(0xA005, group)
        self.write_value(0xA006, case)

    def set_opcode(self, opcode: int) -> None:
        self.write_value(FAIL_CASE, opcode)
        self.write_value(0xA006, opcode)

    def announce(self, text: str) -> None:
        encoded = text.upper().encode("ascii")
        if len(encoded) > 20:
            raise ValueError(f"progress message is too long: {text}")
        label = self.unique("ProgressText")
        self.progress_messages.append((label, encoded))
        self.ld_hl_label(label)
        self.call("PrintProgress")

    def expect_a(self, expected: int) -> None:
        self.ins(0xFE, expected)
        self.fail_if(0xC2)

    def expect_memory(self, address: int, expected: int) -> None:
        self.read_a16(address)
        self.expect_a(expected)

    def set_af(self, a: int, flags: int) -> None:
        self.ld_rr(0x21, ((a & 0xFF) << 8) | (flags & 0xF0))
        self.ins(0xE5)
        self.ins(0xF1)

    def capture_and_expect_af(self, expected_a: int, expected_f: int) -> None:
        self.ins(0xF5)
        self.ins(0xD1)
        self.ins(0x7A)
        self.expect_a(expected_a)
        self.ins(0x7B)
        self.expect_a(expected_f & 0xF0)

    def patch(self) -> None:
        for fixup in self.fixups:
            try:
                target = self.labels[fixup.label]
            except KeyError as error:
                raise ValueError(f"unknown label {fixup.label}") from error
            if fixup.relative:
                displacement = target - (fixup.offset + 1)
                if not -128 <= displacement <= 127:
                    raise ValueError(
                        f"relative branch to {fixup.label} is out of range"
                    )
                self.image[fixup.offset] = displacement & 0xFF
            else:
                self.image[fixup.offset] = target & 0xFF
                self.image[fixup.offset + 1] = target >> 8

    def install_header(self) -> None:
        # A boot-ROM-free entry point.  The logo area stays zero intentionally:
        # these are emulator diagnostics and do not redistribute Nintendo data.
        main = self.labels["Main"]
        self.image[0x100:0x104] = bytes((0xC3, main & 0xFF, main >> 8, 0x00))
        self.base_coverage.add(0xC3)
        title = self.title.encode("ascii")[:15]
        self.image[0x134:0x143] = title.ljust(15, b"\0")
        self.image[0x143] = 0x00
        self.image[0x146] = 0x00
        self.image[0x147] = self.cartridge_type
        self.image[0x148] = self.rom_size_code
        self.image[0x149] = self.ram_size_code
        checksum = 0
        for value in self.image[0x134:0x14D]:
            checksum = (checksum - value - 1) & 0xFF
        self.image[0x14D] = checksum
        self.image[0x14E:0x150] = b"\0\0"
        global_checksum = sum(self.image) & 0xFFFF
        self.image[0x14E] = global_checksum >> 8
        self.image[0x14F] = global_checksum & 0xFF

    def finish(self) -> bytes:
        self.patch()
        self.install_header()
        return bytes(self.image)


def add_vectors(rom: Rom) -> None:
    """Install RST returns and distinguishable eight-byte interrupt handlers."""
    for address in range(0x00, 0x40, 0x08):
        rom.image[address] = 0xC9
    rom.base_coverage.add(0xC9)

    # LD A,n; LDH (FF80),A; XOR A; LDH (FF0F),A; RETI
    for index, address in enumerate(range(0x40, 0x68, 0x08), start=1):
        rom.image[address : address + 8] = bytes(
            (0x3E, index, 0xE0, 0x80, 0xAF, 0xE0, 0x0F, 0xD9)
        )
    rom.base_coverage.update((0x3E, 0xE0, 0xAF, 0xD9))


def alu_result(operation: str, a: int, value: int, carry: int) -> tuple[int, int]:
    """Independent LR35902 ALU oracle used only while generating constants."""
    if operation in {"add", "adc"}:
        carry_in = carry if operation == "adc" else 0
        total = a + value + carry_in
        result = total & 0xFF
        flags = (0x80 if result == 0 else 0) | (
            0x20 if (a & 0xF) + (value & 0xF) + carry_in > 0xF else 0
        )
        flags |= 0x10 if total > 0xFF else 0
        return result, flags
    if operation in {"sub", "sbc", "cp"}:
        carry_in = carry if operation == "sbc" else 0
        total = a - value - carry_in
        result = total & 0xFF
        flags = 0x40 | (0x80 if result == 0 else 0)
        flags |= (
            0x20
            if (a & 0xF) < ((value & 0xF) + carry_in)
            else 0
        )
        flags |= 0x10 if total < 0 else 0
        return (a if operation == "cp" else result), flags
    if operation == "and":
        result = a & value
        return result, (0x80 if result == 0 else 0) | 0x20
    if operation == "xor":
        result = a ^ value
        return result, 0x80 if result == 0 else 0
    if operation == "or":
        result = a | value
        return result, 0x80 if result == 0 else 0
    raise ValueError(operation)


def cb_result(opcode: int, value: int, flags: int) -> tuple[int, int]:
    """Independent oracle for one CB-prefixed operation."""
    category = opcode >> 6
    operation = (opcode >> 3) & 7
    if category == 0:
        carry_in = 1 if flags & 0x10 else 0
        if operation == 0:  # RLC
            carry = value >> 7
            result = ((value << 1) | carry) & 0xFF
        elif operation == 1:  # RRC
            carry = value & 1
            result = (value >> 1) | (carry << 7)
        elif operation == 2:  # RL
            carry = value >> 7
            result = ((value << 1) | carry_in) & 0xFF
        elif operation == 3:  # RR
            carry = value & 1
            result = (value >> 1) | (carry_in << 7)
        elif operation == 4:  # SLA
            carry = value >> 7
            result = (value << 1) & 0xFF
        elif operation == 5:  # SRA
            carry = value & 1
            result = (value >> 1) | (value & 0x80)
        elif operation == 6:  # SWAP
            carry = 0
            result = ((value & 0xF) << 4) | (value >> 4)
        else:  # SRL
            carry = value & 1
            result = value >> 1
        result_flags = (0x80 if result == 0 else 0) | (carry << 4)
        return result, result_flags
    bit = 1 << operation
    if category == 1:
        result_flags = (flags & 0x10) | 0x20
        result_flags |= 0x80 if not value & bit else 0
        return value, result_flags
    if category == 2:
        return value & ~bit, flags & 0xF0
    return value | bit, flags & 0xF0


def setup_cb_target(rom: Rom, target: str, value: int) -> None:
    if target == "[hl]":
        rom.ld_rr(0x21, WRAM_VALUE)
        rom.ld_a(value)
        rom.ins(0x77)
    else:
        rom.ins(LD_IMMEDIATE[target], value)


def capture_cb_target(rom: Rom, target: str) -> None:
    rom.ins(LD_A_REGISTER[target])
    rom.write_a16(WRAM_ACTUAL)
    rom.ins(0xF5)
    rom.ins(0xD1)


def add_cb_tests(rom: Rom) -> None:
    rom.set_case(0x20, 0)
    rom.announce("20 CB OPCODES")
    for opcode in range(0x100):
        target = REGISTER_CODES[opcode & 7]
        value = (opcode * 73 + 0x35) & 0xFF
        # Force both interesting bit states and both rotate endpoints.
        bit = 1 << ((opcode >> 3) & 7)
        if opcode & 1:
            value |= bit
        else:
            value &= ~bit & 0xFF
        flags = 0xD0 if opcode & 2 else 0x20
        expected_value, expected_flags = cb_result(opcode, value, flags)

        rom.set_opcode(opcode)
        rom.set_af(0xA5, flags)
        setup_cb_target(rom, target, value)
        rom.cb(opcode)
        capture_cb_target(rom, target)
        rom.expect_memory(WRAM_ACTUAL, expected_value)
        rom.ins(0x7B)
        rom.expect_a(expected_flags)


def setup_alu_operand(rom: Rom, target: str, value: int) -> None:
    if target == "[hl]":
        rom.ld_rr(0x21, WRAM_VALUE)
        rom.ld_a(value)
        rom.ins(0x77)
    elif target != "a":
        rom.ins(LD_IMMEDIATE[target], value)


def add_alu_tests(rom: Rom) -> None:
    operations = ("add", "adc", "sub", "sbc", "and", "xor", "or", "cp")
    starts = (0x80, 0x88, 0x90, 0x98, 0xA0, 0xA8, 0xB0, 0xB8)
    rom.set_case(0x10, 0)
    rom.announce("10 CPU ALU")
    for operation, start in zip(operations, starts):
        for index, target in enumerate(REGISTER_CODES):
            opcode = start + index
            a = 0x81 if target == "a" else (0x0F + index * 0x11) & 0xFF
            value = a if target == "a" else (0x91 - index * 13) & 0xFF
            carry = 1 if operation in {"adc", "sbc"} else 0
            expected_a, expected_f = alu_result(operation, a, value, carry)

            rom.set_opcode(opcode)
            rom.set_af(a, 0x10 if carry else 0)
            setup_alu_operand(rom, target, value)
            if target != "a":
                rom.ld_a(a)
            rom.ins(opcode)
            rom.capture_and_expect_af(expected_a, expected_f)

    immediate = {
        0xC6: "add",
        0xCE: "adc",
        0xD6: "sub",
        0xDE: "sbc",
        0xE6: "and",
        0xEE: "xor",
        0xF6: "or",
        0xFE: "cp",
    }
    for opcode, operation in immediate.items():
        a, value = 0x8F, 0x81
        carry = 1 if operation in {"adc", "sbc"} else 0
        expected_a, expected_f = alu_result(operation, a, value, carry)
        rom.set_opcode(opcode)
        rom.set_af(a, 0x10 if carry else 0)
        rom.ins(opcode, value)
        rom.capture_and_expect_af(expected_a, expected_f)


def setup_ld_state(rom: Rom, source: str, value: int) -> int:
    """Prepare one LD r,r case and return the expected destination value."""
    if source == "[hl]":
        rom.ld_rr(0x21, WRAM_VALUE)
        rom.ld_a(value)
        rom.ins(0x77)
        return value
    if source == "h":
        value = 0xC1
    elif source == "l":
        value = 0x00
    rom.ins(LD_IMMEDIATE[source], value)
    return value


def add_load_matrix_tests(rom: Rom) -> None:
    rom.set_case(0x01, 0)
    rom.announce("01 CPU LOAD MATRIX")
    for destination_index, destination in enumerate(REGISTER_CODES):
        for source_index, source in enumerate(REGISTER_CODES):
            opcode = 0x40 + destination_index * 8 + source_index
            if opcode == 0x76:
                continue
            value = (opcode * 29 + 7) & 0xFF
            rom.set_opcode(opcode)

            if destination == "[hl]":
                # H and L are necessarily the pointer bytes for LD (HL),H/L.
                rom.ld_rr(0x21, WRAM_VALUE)
                if source not in {"h", "l", "[hl]"}:
                    rom.ins(LD_IMMEDIATE[source], value)
                    expected = value
                elif source == "h":
                    expected = WRAM_VALUE >> 8
                elif source == "l":
                    expected = WRAM_VALUE & 0xFF
                else:
                    rom.ld_a(value)
                    rom.ins(0x77)
                    expected = value
            else:
                expected = setup_ld_state(rom, source, value)

            rom.ins(opcode)
            if destination == "[hl]":
                rom.ins(0x7E)
            else:
                rom.ins(LD_A_REGISTER[destination])
            rom.expect_a(expected)


def inc_dec_flags(value: int, *, decrement: bool, carry: bool) -> tuple[int, int]:
    if decrement:
        result = (value - 1) & 0xFF
        flags = 0x40 | (0x80 if result == 0 else 0)
        flags |= 0x20 if value & 0xF == 0 else 0
    else:
        result = (value + 1) & 0xFF
        flags = 0x80 if result == 0 else 0
        flags |= 0x20 if value & 0xF == 0xF else 0
    if carry:
        flags |= 0x10
    return result, flags


def add_inc_dec_tests(rom: Rom) -> None:
    rom.set_case(0x02, 0)
    rom.announce("02 CPU INC DEC")
    for decrement, start in ((False, 0x04), (True, 0x05)):
        for index, target in enumerate(REGISTER_CODES):
            opcode = start + index * 8
            value = 0xFF if index & 1 else 0x10
            expected, flags = inc_dec_flags(
                value, decrement=decrement, carry=True
            )
            rom.set_opcode(opcode)
            rom.set_af(0x55, 0x10)
            setup_cb_target(rom, target, value)
            rom.ins(opcode)
            capture_cb_target(rom, target)
            rom.expect_memory(WRAM_ACTUAL, expected)
            rom.ins(0x7B)
            rom.expect_a(flags)


def add_16_bit_tests(rom: Rom) -> None:
    rom.set_case(0x03, 0)
    rom.announce("03 CPU 16 BIT")
    pairs = (
        (0x01, 0x03, 0x0B, (0x78, 0x79), 0xBEEF),
        (0x11, 0x13, 0x1B, (0x7A, 0x7B), 0xCAFE),
        (0x21, 0x23, 0x2B, (0x7C, 0x7D), 0xD123),
    )
    for load, inc, dec, readers, value in pairs:
        rom.set_opcode(load)
        rom.ld_rr(load, value)
        rom.ins(readers[0])
        rom.expect_a(value >> 8)
        rom.ins(readers[1])
        rom.expect_a(value & 0xFF)
        rom.ins(inc)
        rom.ins(dec)
        rom.ins(readers[0])
        rom.expect_a(value >> 8)
        rom.ins(readers[1])
        rom.expect_a(value & 0xFF)

    rom.set_opcode(0x31)
    rom.ld_rr(0x31, 0xDFF0)
    rom.ins(0x33)
    rom.ins(0x3B)

    # ADD HL,rr: check result and Z preservation plus H/C/N.
    add_cases = (
        (0x09, 0x0FFF, 0x0001),
        (0x19, 0xFFFF, 0x0001),
        (0x29, 0x8800, 0x8800),
        (0x39, 0x7FFF, 0x0001),
    )
    for opcode, left, right in add_cases:
        rom.set_opcode(opcode)
        rom.set_af(0, 0x80)
        rom.ld_rr(0x21, left)
        if opcode == 0x09:
            rom.ld_rr(0x01, right)
        elif opcode == 0x19:
            rom.ld_rr(0x11, right)
        elif opcode == 0x39:
            rom.ld_rr(0x31, right)
        rom.ins(opcode)
        rom.ins(0xF5)
        rom.ins(0xD1)
        result = (left + (left if opcode == 0x29 else right)) & 0xFFFF
        flags = 0x80
        operand = left if opcode == 0x29 else right
        flags |= 0x20 if (left & 0xFFF) + (operand & 0xFFF) > 0xFFF else 0
        flags |= 0x10 if left + operand > 0xFFFF else 0
        rom.ins(0x7C)
        rom.expect_a(result >> 8)
        rom.ins(0x7D)
        rom.expect_a(result & 0xFF)
        rom.ins(0x7B)
        rom.expect_a(flags)

    rom.ld_rr(0x31, 0xDFF0)


def add_misc_cpu_tests(rom: Rom) -> None:
    rom.set_case(0x04, 0)
    rom.announce("04 CPU MISC")
    rom.ins(0x00)
    rom.ld_rr(0x21, WRAM_VALUE)
    rom.ins(0x36, 0xA6)
    rom.ins(0x7E)
    rom.expect_a(0xA6)

    # Direct and auto-increment/decrement memory loads.
    for store, load, pointer_after in (
        (0x02, 0x0A, 0xC100),
        (0x12, 0x1A, 0xC100),
        (0x22, 0x2A, 0xC101),
        (0x32, 0x3A, 0xC0FF),
    ):
        rom.set_opcode(store)
        if store in (0x02, 0x0A):
            rom.ld_rr(0x01, WRAM_VALUE)
        elif store in (0x12, 0x1A):
            rom.ld_rr(0x11, WRAM_VALUE)
        else:
            rom.ld_rr(0x21, WRAM_VALUE)
        rom.ld_a(0x5A)
        rom.ins(store)
        rom.ld_a(0)
        if store in (0x22, 0x32):
            rom.ld_rr(0x21, WRAM_VALUE)
        rom.ins(load)
        rom.expect_a(0x5A)
        if load in (0x2A, 0x3A):
            rom.ins(0x7C)
            rom.expect_a(pointer_after >> 8)
            rom.ins(0x7D)
            rom.expect_a(pointer_after & 0xFF)

    # LD (a16),SP, then restore a known stack.
    rom.set_opcode(0x08)
    rom.ld_rr(0x31, 0xC35A)
    rom.ins(0x08, 0x00, 0xC1)
    rom.expect_memory(0xC100, 0x5A)
    rom.expect_memory(0xC101, 0xC3)
    rom.ld_rr(0x31, 0xDFF0)

    rotate_cases = (
        (0x07, 0x81, 0, 0x03, 0x10),
        (0x0F, 0x01, 0, 0x80, 0x10),
        (0x17, 0x80, 0x10, 0x01, 0x10),
        (0x1F, 0x01, 0x10, 0x80, 0x10),
    )
    for opcode, value, flags, expected, expected_flags in rotate_cases:
        rom.set_opcode(opcode)
        rom.set_af(value, flags)
        rom.ins(opcode)
        rom.capture_and_expect_af(expected, expected_flags)

    daa_cases = (
        (0x0A, 0x00, 0x10, 0x00),
        (0x9A, 0x00, 0x00, 0x90),
        (0x0F, 0x20, 0x15, 0x00),
        (0xFF, 0x40, 0xFF, 0x40),
        (0x00, 0xD0, 0xA0, 0x50),
    )
    for value, flags, expected, expected_flags in daa_cases:
        rom.set_opcode(0x27)
        rom.set_af(value, flags)
        rom.ins(0x27)
        rom.capture_and_expect_af(expected, expected_flags)

    rom.set_af(0x55, 0x90)
    rom.ins(0x2F)
    rom.capture_and_expect_af(0xAA, 0xF0)
    rom.set_af(0x55, 0xE0)
    rom.ins(0x37)
    rom.capture_and_expect_af(0x55, 0x90)
    rom.set_af(0x55, 0xF0)
    rom.ins(0x3F)
    rom.capture_and_expect_af(0x55, 0x80)

    # PUSH/POP all pairs. POP AF also verifies the flag low nibble is masked.
    pair_cases = (
        (0x01, 0xC5, 0xC1, 0xBEEF, (0x78, 0x79)),
        (0x11, 0xD5, 0xD1, 0xCAFE, (0x7A, 0x7B)),
        (0x21, 0xE5, 0xE1, 0xD123, (0x7C, 0x7D)),
    )
    for load, push, pop, value, readers in pair_cases:
        rom.set_opcode(push)
        rom.ld_rr(load, value)
        rom.ins(push)
        rom.ld_rr(load, 0)
        rom.ins(pop)
        rom.ins(readers[0])
        rom.expect_a(value >> 8)
        rom.ins(readers[1])
        rom.expect_a(value & 0xFF)
    rom.set_af(0xAB, 0xF0)
    rom.ins(0xF5)
    rom.set_af(0, 0)
    rom.ins(0xF1)
    rom.capture_and_expect_af(0xAB, 0xF0)

    # Absolute and high-memory load forms.
    rom.ld_a(0x66)
    rom.ins(0xEA, 0x20, 0xC1)
    rom.ld_a(0)
    rom.ins(0xFA, 0x20, 0xC1)
    rom.expect_a(0x66)
    rom.ld_a(0x77)
    rom.ins(0xE0, 0x90)
    rom.ld_a(0)
    rom.ins(0xF0, 0x90)
    rom.expect_a(0x77)
    rom.ins(0x0E, 0x91)
    rom.ld_a(0x88)
    rom.ins(0xE2)
    rom.ld_a(0)
    rom.ins(0xF2)
    rom.expect_a(0x88)

    # Signed SP operations at positive and negative boundaries.
    for opcode in (0xE8, 0xF8):
        for sp, offset, result, flags in (
            (0x00FF, 1, 0x0100, 0x30),
            (0x0000, 0xFF, 0xFFFF, 0x00),
        ):
            rom.set_opcode(opcode)
            rom.ld_rr(0x31, sp)
            rom.ins(opcode, offset)
            if opcode == 0xE8:
                rom.ins(0xF5)
                rom.ins(0xD1)
                rom.ins(0xF8, 0)
            else:
                rom.ins(0xF5)
                rom.ins(0xD1)
            rom.ins(0x7C)
            rom.expect_a(result >> 8)
            rom.ins(0x7D)
            rom.expect_a(result & 0xFF)
            rom.ins(0x7B)
            rom.expect_a(flags)

    rom.ld_rr(0x21, 0xDFF0)
    rom.ins(0xF9)
    rom.ins(0xF8, 0)
    rom.ins(0x7C)
    rom.expect_a(0xDF)
    rom.ins(0x7D)
    rom.expect_a(0xF0)


def set_condition(rom: Rom, condition: str, taken: bool) -> None:
    if condition == "nz":
        rom.set_af(0, 0 if taken else 0x80)
    elif condition == "z":
        rom.set_af(0, 0x80 if taken else 0)
    elif condition == "nc":
        rom.set_af(0, 0 if taken else 0x10)
    elif condition == "c":
        rom.set_af(0, 0x10 if taken else 0)
    else:
        raise ValueError(condition)


def add_flow_tests(rom: Rom) -> None:
    rom.set_case(0x05, 0)
    rom.announce("05 CPU FLOW")

    target = rom.unique("jr")
    rom.rel_branch(0x18, target)
    rom.jp("Fail")
    rom.label(target)

    for condition, opcode in (("nz", 0x20), ("z", 0x28), ("nc", 0x30), ("c", 0x38)):
        for taken in (False, True):
            destination = rom.unique(f"jr_{condition}")
            done = rom.unique("jr_done")
            set_condition(rom, condition, taken)
            rom.ld_a(0x11)
            rom.rel_branch(opcode, destination)
            rom.jp(done)
            rom.label(destination)
            rom.ld_a(0x22)
            rom.label(done)
            rom.expect_a(0x22 if taken else 0x11)

    for condition, opcode in (
        ("nz", 0xC2),
        ("z", 0xCA),
        ("nc", 0xD2),
        ("c", 0xDA),
    ):
        for taken in (False, True):
            destination = rom.unique(f"jp_{condition}")
            done = rom.unique("jp_done")
            set_condition(rom, condition, taken)
            rom.ld_a(0x33)
            rom.abs_branch(opcode, destination)
            rom.jp(done)
            rom.label(destination)
            rom.ld_a(0x44)
            rom.label(done)
            rom.expect_a(0x44 if taken else 0x33)

    plain_jump = rom.unique("jp_plain")
    rom.jp(plain_jump)
    rom.jp("Fail")
    rom.label(plain_jump)

    for condition, opcode in (
        ("nz", 0xC4),
        ("z", 0xCC),
        ("nc", 0xD4),
        ("c", 0xDC),
    ):
        for taken in (False, True):
            subroutine = rom.unique(f"call_{condition}")
            done = rom.unique("call_done")
            set_condition(rom, condition, taken)
            rom.ld_a(0x55)
            rom.abs_branch(opcode, subroutine)
            rom.jp(done)
            rom.label(subroutine)
            rom.ld_a(0x66)
            rom.ins(0xC9)
            rom.label(done)
            rom.expect_a(0x66 if taken else 0x55)

    plain_sub = rom.unique("call_plain")
    plain_done = rom.unique("call_plain_done")
    rom.call(plain_sub)
    rom.jp(plain_done)
    rom.label(plain_sub)
    rom.ld_a(0x67)
    rom.ins(0xC9)
    rom.label(plain_done)
    rom.expect_a(0x67)

    # Conditional RET executes in a called subroutine in both directions.
    for condition, opcode in (
        ("nz", 0xC0),
        ("z", 0xC8),
        ("nc", 0xD0),
        ("c", 0xD8),
    ):
        for taken in (False, True):
            subroutine = rom.unique(f"ret_{condition}")
            after = rom.unique("ret_after")
            rom.call(subroutine)
            rom.jp(after)
            rom.label(subroutine)
            set_condition(rom, condition, taken)
            rom.ld_a(0x71)
            rom.ins(opcode)
            rom.ld_a(0x72)
            rom.ins(0xC9)
            rom.label(after)
            rom.expect_a(0x71 if taken else 0x72)

    # JP HL.
    jp_hl_target = rom.unique("jp_hl")
    rom.ld_rr(0x21, 0)
    fixup_offset = rom.pc - 2
    rom.fixups.append(Fixup(fixup_offset, jp_hl_target, False))
    rom.ins(0xE9)
    rom.jp("Fail")
    rom.label(jp_hl_target)

    # All restart vectors contain RET, so successful return verifies stack/PC.
    for opcode in (0xC7, 0xCF, 0xD7, 0xDF, 0xE7, 0xEF, 0xF7, 0xFF):
        rom.set_opcode(opcode)
        rom.ins(opcode)

    # RETI as a callable return, followed immediately by DI to contain IME.
    reti_sub = rom.unique("reti")
    reti_done = rom.unique("reti_done")
    rom.call(reti_sub)
    rom.jp(reti_done)
    rom.label(reti_sub)
    rom.ins(0xD9)
    rom.label(reti_done)
    rom.ins(0xF3)

    # HALT wakes on a pending interrupt even while IME is clear.
    rom.set_opcode(0x76)
    rom.ins(0xF3)
    rom.write_value(0xFFFF, 0x01)
    rom.write_value(0xFF0F, 0x01)
    rom.ins(0x76)
    rom.write_value(0xFF0F, 0)
    rom.write_value(0xFFFF, 0)

    # EI delay and interrupt priority. Vector $40 stores 1 in FF80.
    rom.write_value(FAIL_GROUP, 0)
    rom.write_value(0xFF0F, 0x1F)
    rom.write_value(0xFFFF, 0x1F)
    rom.ins(0xFB)
    rom.ins(0x00)
    rom.ins(0xF3)
    rom.ldh_read(0x80)
    rom.expect_a(1)
    rom.write_value(0xFFFF, 0)


def add_subsystem_tests(rom: Rom) -> None:
    rom.set_case(0x30, 0)
    rom.announce("30 MEMORY BUS")

    # WRAM/echo mirroring, HRAM, unusable OAM and cartridge RAM.
    rom.write_value(0xC123, 0x5A)
    rom.expect_memory(0xE123, 0x5A)
    rom.write_value(0xE456, 0xA5)
    rom.expect_memory(0xC456, 0xA5)
    rom.write_value(0xFF92, 0x77)
    rom.expect_memory(0xFF92, 0x77)
    rom.write_value(0xFEA0, 0x99)
    rom.expect_memory(0xFEA0, 0x00)
    rom.write_value(0xA100, 0x3C)
    rom.expect_memory(0xA100, 0x3C)

    # ROM writes are trapped and cannot modify the instruction image.
    original = 0xC3
    rom.write_value(0x0100, original ^ 0xFF)
    rom.expect_memory(0x0100, original)

    # Joypad with no host input: both selected rows report all buttons released.
    for selector in (0x10, 0x20, 0x00):
        rom.write_value(0xFF00, selector)
        rom.read_a16(0xFF00)
        rom.ins(0xE6, 0x0F)
        rom.expect_a(0x0F)

    # OAM DMA copies a complete page through the connected PPU.
    rom.set_case(0x31, 0)
    rom.announce("31 OAM DMA")
    rom.ld_rr(0x21, 0xC000)
    rom.ins(0x06, 0xA0)
    rom.ins(0x3E, 0x40)
    fill_loop = rom.unique("dma_fill")
    rom.label(fill_loop)
    rom.ins(0x22)
    rom.ins(0x3C)
    rom.ins(0x05)
    rom.rel_branch(0x20, fill_loop)
    rom.write_value(0xFF46, 0xC0)
    rom.expect_memory(0xFE00, 0x40)
    rom.expect_memory(0xFE9F, 0xDF)

    # DIV advances, and any write resets it.
    rom.set_case(0x32, 0)
    rom.announce("32 TIMER IRQ")
    rom.write_value(0xFF04, 0xFF)
    rom.expect_memory(0xFF04, 0)
    rom.ins(0x06, 40)
    div_wait = rom.unique("div_wait")
    rom.label(div_wait)
    rom.ins(0x00)
    rom.ins(0x05)
    rom.rel_branch(0x20, div_wait)
    rom.read_a16(0xFF04)
    rom.ins(0xFE, 0)
    rom.fail_if(0xCA)

    # TIMA increments at TAC frequency 01 and reloads TMA on overflow.
    rom.write_value(0xFF07, 0)
    rom.write_value(0xFF05, 0xFE)
    rom.write_value(0xFF06, 0xA7)
    rom.write_value(0xFF0F, 0)
    rom.write_value(0xFF07, 0x05)
    rom.ins(0x00)
    rom.ins(0x00)
    rom.ins(0x00)
    rom.ins(0x00)
    rom.write_value(0xFF07, 0)
    rom.expect_memory(0xFF05, 0xA7)
    rom.read_a16(0xFF0F)
    rom.ins(0xE6, 0x04)
    rom.expect_a(0x04)
    rom.write_value(0xFF0F, 0)

    # Internal-clock serial transfer completes, reads FF without a peer and IRQs.
    rom.set_case(0x33, 0)
    rom.announce("33 SERIAL")
    rom.write_value(0xFF01, ord("#"))
    rom.write_value(0xFF02, 0x81)
    serial_wait = rom.unique("serial_wait")
    rom.label(serial_wait)
    rom.read_a16(0xFF02)
    rom.ins(0xE6, 0x80)
    rom.rel_branch(0x20, serial_wait)
    rom.expect_memory(0xFF01, 0xFF)
    rom.read_a16(0xFF0F)
    rom.ins(0xE6, 0x08)
    rom.expect_a(0x08)
    rom.ld_a(10)
    rom.call("SendChar")
    rom.write_value(0xFF0F, 0)

    # PPU state changes are CPU-visible through LY and mode bits.
    rom.set_case(0x34, 0)
    rom.announce("34 PPU")
    rom.read_a16(0xFF44)
    rom.write_a16(WRAM_ACTUAL)
    rom.ins(0x06, 80)
    ly_wait = rom.unique("ly_wait")
    rom.label(ly_wait)
    rom.ins(0x00)
    rom.ins(0x05)
    rom.rel_branch(0x20, ly_wait)
    rom.read_a16(0xFF44)
    rom.ins(0x47)
    rom.read_a16(WRAM_ACTUAL)
    rom.ins(0xB8)
    rom.fail_if(0xCA)

    # APU master power and register storage/read masks.
    rom.set_case(0x35, 0)
    rom.announce("35 APU")
    rom.write_value(0xFF26, 0x80)
    rom.read_a16(0xFF26)
    rom.ins(0xE6, 0x80)
    rom.expect_a(0x80)
    rom.write_value(0xFF24, 0x77)
    rom.read_a16(0xFF24)
    rom.expect_a(0x77)
    rom.write_value(0xFF25, 0xFF)

    # Trigger all four channels and verify their CPU-visible status bits.
    for address, value in (
        (0xFF11, 0x80),
        (0xFF12, 0xF0),
        (0xFF13, 0x00),
        (0xFF14, 0x80),
        (0xFF16, 0x80),
        (0xFF17, 0xF0),
        (0xFF18, 0x00),
        (0xFF19, 0x80),
        (0xFF1A, 0x80),
        (0xFF1B, 0x00),
        (0xFF1C, 0x20),
        (0xFF30, 0xA5),
        (0xFF1D, 0x00),
        (0xFF1E, 0x80),
        (0xFF20, 0x00),
        (0xFF21, 0xF0),
        (0xFF22, 0x00),
        (0xFF23, 0x80),
    ):
        rom.write_value(address, value)
    rom.expect_memory(0xFF30, 0xA5)
    rom.read_a16(0xFF26)
    rom.ins(0xE6, 0x0F)
    rom.expect_a(0x0F)
    rom.write_value(0xFF26, 0)
    rom.read_a16(0xFF26)
    rom.ins(0xE6, 0x80)
    rom.expect_a(0)


def emit_result_text(rom: Rom, text: bytes) -> None:
    for index, value in enumerate(text + b"\0"):
        rom.write_value(0xA010 + index, value)


def add_result_handlers(rom: Rom) -> None:
    rom.label("Pass")
    emit_result_text(rom, b"Passed")
    rom.write_value(0xA004, 0)
    rom.announce("PASS")
    pass_loop = rom.unique("pass")
    rom.label(pass_loop)
    rom.ins(0xF3)
    rom.ins(0x76)
    rom.rel_branch(0x18, pass_loop)

    rom.label("Fail")
    rom.ins(0xF3)
    rom.announce("ERROR")
    for value in b"AT ":
        rom.ld_a(value)
        rom.call("SendChar")
    rom.ldh_read(0x80)
    rom.call("SendHex")
    rom.ld_a(ord(":"))
    rom.call("SendChar")
    rom.ldh_read(0x81)
    rom.call("SendHex")
    rom.ld_a(10)
    rom.call("SendChar")
    emit_result_text(rom, b"Failed")
    rom.ldh_read(0x80)
    rom.write_a16(0xA005)
    rom.ldh_read(0x81)
    rom.write_a16(0xA006)
    rom.write_value(0xA004, 0xE0)
    rom.announce("FAIL")
    fail_loop = rom.unique("fail")
    rom.label(fail_loop)
    rom.ins(0x76)
    rom.rel_branch(0x18, fail_loop)


def font_data() -> bytes:
    """Return tiles 0-63 corresponding to printable ASCII 32-95."""
    result = bytearray()
    for codepoint in range(32, 96):
        glyph = FONT_5X7.get(chr(codepoint), ("00000",) * 7)
        rows = ("00000", *glyph)
        for row in rows:
            low_plane = int(row, 2) << 2
            result.extend((low_plane, 0))
    return bytes(result)


def add_display_and_serial_routines(rom: Rom) -> None:
    """Add the tiny on-screen progress console and serial text transport."""
    rom.label("InitDisplay")
    rom.ins(0xAF)
    rom.ldh_write(0x40)
    rom.ld_hl_label("FontData")
    rom.ld_rr(0x11, 0x8000)
    rom.ld_rr(0x01, len(font_data()))
    copy_loop = rom.unique("font_copy")
    rom.label(copy_loop)
    rom.ins(0x2A)
    rom.ins(0x12)
    rom.ins(0x13)
    rom.ins(0x0B)
    rom.ins(0x78)
    rom.ins(0xB1)
    rom.rel_branch(0x20, copy_loop)
    rom.ld_a(0xE4)
    rom.ldh_write(0x47)
    rom.ld_a(0x91)
    rom.ldh_write(0x40)
    rom.ins(0xC9)

    rom.label("PrintProgress")
    rom.ins(0xE5)
    rom.ld_hl_label("ProtocolPrefix")
    rom.call("SendStringSerial")
    rom.ins(0xE1)
    rom.ld_rr(0x11, 0x9800)
    rom.ins(0x06, 20)
    rom.ins(0xAF)
    clear_loop = rom.unique("progress_clear")
    rom.label(clear_loop)
    rom.ins(0x12)
    rom.ins(0x13)
    rom.ins(0x05)
    rom.rel_branch(0x20, clear_loop)
    rom.ld_rr(0x11, 0x9800)
    print_loop = rom.unique("progress_print")
    print_done = rom.unique("progress_done")
    rom.label(print_loop)
    rom.ins(0x2A)
    rom.ins(0xA7)
    rom.rel_branch(0x28, print_done)
    rom.ins(0x47)
    rom.ins(0xD6, 32)
    rom.ins(0x12)
    rom.ins(0x13)
    rom.ins(0x78)
    rom.call("SendChar")
    rom.rel_branch(0x18, print_loop)
    rom.label(print_done)
    rom.ld_a(10)
    rom.call("SendChar")
    rom.ins(0xC9)

    rom.label("SendStringSerial")
    string_loop = rom.unique("serial_string")
    rom.label(string_loop)
    rom.ins(0x2A)
    rom.ins(0xA7)
    rom.ins(0xC8)
    rom.call("SendChar")
    rom.rel_branch(0x18, string_loop)

    rom.label("SendChar")
    rom.ldh_write(0x01)
    rom.ld_a(0x81)
    rom.ldh_write(0x02)
    serial_loop = rom.unique("send_wait")
    rom.label(serial_loop)
    rom.ldh_read(0x02)
    rom.ins(0xE6, 0x80)
    rom.rel_branch(0x20, serial_loop)
    rom.ins(0xC9)

    rom.label("SendHex")
    rom.ins(0xF5)
    rom.cb(0x37)
    rom.ins(0xE6, 0x0F)
    rom.call("SendNibble")
    rom.ins(0xF1)
    rom.ins(0xE6, 0x0F)
    rom.call("SendNibble")
    rom.ins(0xC9)

    rom.label("SendNibble")
    decimal = rom.unique("nibble_decimal")
    rom.ins(0xFE, 10)
    rom.rel_branch(0x38, decimal)
    rom.ins(0xC6, 7)
    rom.label(decimal)
    rom.ins(0xC6, ord("0"))
    rom.call("SendChar")
    rom.ins(0xC9)


def add_rom_data(rom: Rom) -> None:
    rom.label("ProtocolPrefix")
    rom.raw(*b"PYGB/1 ", 0)
    for label, message in rom.progress_messages:
        rom.label(label)
        rom.raw(*message, 0)
    rom.label("FontData")
    rom.raw(*font_data())


def add_serial_result_handlers(rom: Rom) -> None:
    """Result handlers for controller ROMs that cannot reserve cartridge RAM."""
    rom.label("Pass")
    rom.announce("PASS")
    pass_loop = rom.unique("pass")
    rom.label(pass_loop)
    rom.ins(0xF3)
    rom.ins(0x76)
    rom.rel_branch(0x18, pass_loop)

    rom.label("Fail")
    rom.ins(0xF3)
    rom.announce("ERROR")
    for value in b"AT ":
        rom.ld_a(value)
        rom.call("SendChar")
    rom.ldh_read(0x80)
    rom.call("SendHex")
    rom.ld_a(ord(":"))
    rom.call("SendChar")
    rom.ldh_read(0x81)
    rom.call("SendHex")
    rom.ld_a(10)
    rom.call("SendChar")
    rom.announce("FAIL")
    fail_loop = rom.unique("fail")
    rom.label(fail_loop)
    rom.ins(0x76)
    rom.rel_branch(0x18, fail_loop)


def finish_controller_rom(rom: Rom) -> bytes:
    rom.jp("Pass")
    add_serial_result_handlers(rom)
    add_display_and_serial_routines(rom)
    add_rom_data(rom)
    return rom.finish()


def controller_rom(
    title: str,
    *,
    cartridge_type: int,
    size: int,
    rom_size_code: int,
    ram_size_code: int,
    group: int,
    progress: str,
) -> Rom:
    rom = Rom(
        title,
        cartridge_type=cartridge_type,
        size=size,
        rom_size_code=rom_size_code,
        ram_size_code=ram_size_code,
    )
    rom.label("Main")
    rom.ins(0xF3)
    rom.ld_rr(0x31, 0xDFF0)
    rom.call("InitDisplay")
    rom.set_case(group, 0)
    rom.announce(progress)
    return rom


def build_mbc1_rom() -> bytes:
    rom = controller_rom(
        "PYGB-MBC1",
        cartridge_type=0x03,
        size=64 * 1024,
        rom_size_code=0x01,
        ram_size_code=0x03,
        group=0x41,
        progress="41 MBC1",
    )
    rom.image[0x4000] = 0x41
    rom.image[0x8000] = 0x42
    rom.set_opcode(1)
    rom.write_value(0x2000, 2)
    rom.expect_memory(0x4000, 0x42)
    rom.write_value(0x2000, 0)
    rom.expect_memory(0x4000, 0x41)

    rom.set_opcode(2)
    rom.write_value(0x0000, 0x0A)
    rom.write_value(0x6000, 1)
    rom.write_value(0x4000, 1)
    rom.write_value(0xA123, 0x11)
    rom.write_value(0x4000, 2)
    rom.write_value(0xA123, 0x22)
    rom.write_value(0x4000, 1)
    rom.expect_memory(0xA123, 0x11)
    rom.write_value(0x4000, 2)
    rom.expect_memory(0xA123, 0x22)
    rom.write_value(0x0000, 0)
    rom.expect_memory(0xA123, 0xFF)
    return finish_controller_rom(rom)


def build_mbc2_rom() -> bytes:
    rom = controller_rom(
        "PYGB-MBC2",
        cartridge_type=0x06,
        size=64 * 1024,
        rom_size_code=0x01,
        ram_size_code=0,
        group=0x42,
        progress="42 MBC2",
    )
    rom.image[0x4000] = 0x41
    rom.image[0x8000] = 0x42
    rom.set_opcode(1)
    rom.write_value(0x2100, 2)
    rom.expect_memory(0x4000, 0x42)
    rom.write_value(0x2100, 0)
    rom.expect_memory(0x4000, 0x41)

    rom.set_opcode(2)
    rom.write_value(0x0000, 0x0A)
    rom.write_value(0xA123, 0xAB)
    rom.expect_memory(0xA123, 0xFB)
    rom.expect_memory(0xA323, 0xFB)
    rom.write_value(0x0000, 0)
    rom.expect_memory(0xA123, 0xFF)
    return finish_controller_rom(rom)


def build_mbc3_rom() -> bytes:
    rom = controller_rom(
        "PYGB-MBC3",
        cartridge_type=0x10,
        size=64 * 1024,
        rom_size_code=0x01,
        ram_size_code=0x03,
        group=0x43,
        progress="43 MBC3 RTC",
    )
    rom.image[0x4000] = 0x41
    rom.image[0x8000] = 0x42
    rom.set_opcode(1)
    rom.write_value(0x2000, 2)
    rom.expect_memory(0x4000, 0x42)
    rom.write_value(0x2000, 0)
    rom.expect_memory(0x4000, 0x41)

    rom.set_opcode(2)
    rom.write_value(0x0000, 0x0A)
    rom.write_value(0x4000, 1)
    rom.write_value(0xA234, 0x31)
    rom.write_value(0x4000, 2)
    rom.write_value(0xA234, 0x32)
    rom.write_value(0x4000, 1)
    rom.expect_memory(0xA234, 0x31)
    rom.write_value(0x4000, 2)
    rom.expect_memory(0xA234, 0x32)

    rom.set_opcode(3)
    rom.write_value(0x4000, 0x08)
    rom.write_value(0xA000, 0x2A)
    rom.write_value(0x6000, 0)
    rom.write_value(0x6000, 1)
    rom.expect_memory(0xA000, 0x2A)
    return finish_controller_rom(rom)


def build_mbc5_rom() -> bytes:
    rom = controller_rom(
        "PYGB-MBC5",
        cartridge_type=0x1B,
        size=8 * 1024 * 1024,
        rom_size_code=0x08,
        ram_size_code=0x04,
        group=0x45,
        progress="45 MBC5 9 BIT",
    )
    rom.image[0x0000] = 0x40
    rom.image[0x4000] = 0x41
    rom.image[257 * 0x4000] = 0xE1
    rom.set_opcode(1)
    rom.write_value(0x2000, 1)
    rom.write_value(0x3000, 1)
    rom.expect_memory(0x4000, 0xE1)
    rom.write_value(0x3000, 0)
    rom.expect_memory(0x4000, 0x41)
    rom.write_value(0x2000, 0)
    rom.expect_memory(0x4000, 0x40)

    rom.set_opcode(2)
    rom.write_value(0x0000, 0x0A)
    rom.write_value(0x4000, 1)
    rom.write_value(0xA345, 0x51)
    rom.write_value(0x4000, 15)
    rom.write_value(0xA345, 0x5F)
    rom.write_value(0x4000, 1)
    rom.expect_memory(0xA345, 0x51)
    rom.write_value(0x4000, 15)
    rom.expect_memory(0xA345, 0x5F)
    return finish_controller_rom(rom)


def build_controller_roms() -> dict[str, bytes]:
    return {
        "mbc1.gb": build_mbc1_rom(),
        "mbc2.gb": build_mbc2_rom(),
        "mbc3.gb": build_mbc3_rom(),
        "mbc5.gb": build_mbc5_rom(),
    }


def build_core_rom() -> tuple[bytes, dict]:
    rom = Rom("PYGB-CONFORM")
    add_vectors(rom)
    rom.label("Main")
    rom.ins(0xF3)
    rom.ld_rr(0x31, 0xDFF0)
    rom.call("InitDisplay")
    rom.write_value(0xA000, ord("P"))
    rom.write_value(0xA001, ord("Y"))
    rom.write_value(0xA002, ord("G"))
    rom.write_value(0xA003, ord("B"))
    rom.write_value(0xA004, 0x7E)
    rom.write_value(0xA005, 0)
    rom.write_value(0xA006, 0)
    emit_result_text(rom, b"Running")

    add_load_matrix_tests(rom)
    add_inc_dec_tests(rom)
    add_16_bit_tests(rom)
    add_misc_cpu_tests(rom)
    add_flow_tests(rom)
    add_alu_tests(rom)
    add_cb_tests(rom)
    add_subsystem_tests(rom)
    rom.jp("Pass")
    add_result_handlers(rom)
    add_display_and_serial_routines(rom)
    add_rom_data(rom)

    missing_base = LEGAL_BASE_OPCODES - rom.base_coverage - {0x10}
    if missing_base:
        values = " ".join(f"{value:02X}" for value in sorted(missing_base))
        raise ValueError(f"core ROM is missing legal base opcodes: {values}")
    missing_cb = set(range(0x100)) - rom.cb_coverage
    if missing_cb:
        values = " ".join(f"{value:02X}" for value in sorted(missing_cb))
        raise ValueError(f"core ROM is missing CB opcodes: {values}")

    image = rom.finish()
    manifest = {
        "schema_version": 1,
        "rom": "pygameboy-test-rom.gb",
        "sha256": hashlib.sha256(image).hexdigest(),
        "size": len(image),
        "code_end": rom.pc,
        "protocol": "PYGB/1",
        "base_opcodes": [f"{value:02X}" for value in sorted(LEGAL_BASE_OPCODES)],
        "core_base_opcodes": [
            f"{value:02X}" for value in sorted(rom.base_coverage)
        ],
        "cb_opcodes": [f"CB {value:02X}" for value in sorted(rom.cb_coverage)],
        "dedicated_probes": {
            "10": "STOP probe",
            **{
                f"{value:02X}": "illegal-opcode lockup probe"
                for value in sorted(ILLEGAL_BASE_OPCODES)
            },
        },
        "subsystems": [
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
        ],
    }
    return image, manifest


def build_probe_rom(opcode: int) -> bytes:
    rom = Rom(f"PYGB-PROBE-{opcode:02X}", cartridge_type=0x00)
    rom.label("Main")
    rom.ins(opcode, 0, cover=False)
    loop = rom.unique("probe")
    rom.label(loop)
    rom.jp(loop)
    return rom.finish()


def bundle() -> tuple[bytes, dict, dict[str, bytes], dict[str, bytes]]:
    image, manifest = build_core_rom()
    controllers = build_controller_roms()
    probes = {
        f"{opcode:02x}.gb": build_probe_rom(opcode)
        for opcode in (0x10, *sorted(ILLEGAL_BASE_OPCODES))
    }
    manifest = {
        **manifest,
        "controller_roms": {
            name: {
                "size": len(controller),
                "sha256": hashlib.sha256(controller).hexdigest(),
            }
            for name, controller in controllers.items()
        },
        "probe_roms": {
            name: hashlib.sha256(probe).hexdigest()
            for name, probe in probes.items()
        },
    }
    return image, manifest, controllers, probes


def write_outputs(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    image, manifest, controllers, probes = bundle()
    (output / "pygameboy-test-rom.gb").write_bytes(image)
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    controller_dir = output / "controllers"
    controller_dir.mkdir(exist_ok=True)
    for name, controller in controllers.items():
        (controller_dir / name).write_bytes(controller)
    probe_dir = output / "probes"
    probe_dir.mkdir(exist_ok=True)
    for name, probe in probes.items():
        (probe_dir / name).write_bytes(probe)
    return manifest


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).parent / "dist",
    )
    parser.add_argument("--check", action="store_true")
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    if args.check:
        expected_image, expected_manifest, controllers, probes = bundle()
        image_path = args.output / "pygameboy-test-rom.gb"
        manifest_path = args.output / "manifest.json"
        if not image_path.is_file() or not manifest_path.is_file():
            print("generated first-party ROM artifacts are missing")
            return 1
        actual_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if image_path.read_bytes() != expected_image:
            print(f"{image_path} is stale; regenerate it")
            return 1
        if actual_manifest != expected_manifest:
            print(f"{manifest_path} is stale; regenerate it")
            return 1
        for directory, artifacts in (
            ("controllers", controllers),
            ("probes", probes),
        ):
            for name, expected in artifacts.items():
                path = args.output / directory / name
                if not path.is_file() or path.read_bytes() != expected:
                    print(f"{path} is missing or stale; regenerate it")
                    return 1
        return 0
    manifest = write_outputs(args.output)
    print(
        f"wrote {manifest['rom']} ({manifest['size']} bytes, "
        f"sha256 {manifest['sha256']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
