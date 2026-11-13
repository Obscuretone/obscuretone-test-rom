/* Run OTR against SameBoy's public library API, independently of PyGameBoy.
 * Build against the pinned SameBoy checkout used by CI; see README.md.
 * The tiny original boot stub deliberately skips Nintendo's logo validation.
 */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "Core/gb.h"

static uint32_t pixels[160 * 144];

static uint32_t encode_rgb(GB_gameboy_t *gb, uint8_t r, uint8_t g, uint8_t b)
{
    (void)gb;
    return ((uint32_t)r << 16) | ((uint32_t)g << 8) | b;
}

static int run_rom(const char *path, bool color)
{
    GB_gameboy_t *gb = GB_init(GB_alloc(), color ? GB_MODEL_CGB_E : GB_MODEL_DMG_B);
    if (!gb || GB_load_rom(gb, path)) {
        fprintf(stderr, "Cannot load %s\n", path);
        return 2;
    }
    uint8_t boot[0x100] = {0};
    /* LD A,model; JP $00FC; LDH ($50),A; cartridge starts at $0100. */
    boot[0] = 0x3E;
    boot[1] = color ? 0x11 : 0x01;
    boot[2] = 0xC3;
    boot[3] = 0xFC;
    boot[0xFC] = 0xE0;
    boot[0xFD] = 0x50;
    GB_load_boot_rom_from_buffer(gb, boot, sizeof(boot));
    GB_set_rgb_encode_callback(gb, encode_rgb);
    GB_set_pixels_output(gb, pixels);
    int result = 1;
    bool started = false;
    bool finished = false;
    /* 20 million instructions: bound hangs and leave the machine time to
     * produce its serial/display result after committing the mailbox. */
    for (unsigned i = 0; i < 20000000; i++) {
        GB_run(gb);
        if (GB_read_memory(gb, 0xA000) != 'O' ||
            GB_read_memory(gb, 0xA001) != 'T' ||
            GB_read_memory(gb, 0xA002) != 'R' ||
            GB_read_memory(gb, 0xA003) != '1') continue;
        uint8_t state = GB_read_memory(gb, 0xA004);
        if (state == 0x7E) {
            started = true;
            continue;
        }
        if (!started) continue;
        finished = true;
        result = state == 0 ? 0 : 1;
        printf("%s %s at %02X:%02X\n", result ? "FAIL" : "PASS", path,
               GB_read_memory(gb, 0xA005), GB_read_memory(gb, 0xA006));
        /* Finish display/serial reporting, then independently check that the
         * success or failure path settles at a HALT instruction. */
        for (unsigned settle = 0; settle < 100000; settle++) GB_run(gb);
        uint16_t pc = GB_get_registers(gb)->pc;
        for (unsigned settle = 0; settle < 1000; settle++) GB_run(gb);
        if (GB_get_registers(gb)->pc != pc || GB_read_memory(gb, pc - 1) != 0x76) {
            fprintf(stderr, "%s did not halt after reporting its result\n", path);
            result = 1;
        }
        break;
    }
    if (!finished) {
        fprintf(stderr, "TIMEOUT %s at PC=%04X group=%02X:%02X\n", path,
                GB_get_registers(gb)->pc, GB_read_memory(gb, 0xFF80),
                GB_read_memory(gb, 0xFF81));
    }
    GB_free(gb);
    GB_dealloc(gb);
    return result;
}

int main(int argc, char **argv)
{
    if (argc != 3 || (strcmp(argv[1], "dmg") && strcmp(argv[1], "cgb"))) {
        fprintf(stderr, "usage: otr-reference dmg|cgb ROM.gb\n");
        return 2;
    }
    return run_rom(argv[2], !strcmp(argv[1], "cgb"));
}
