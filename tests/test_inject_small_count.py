"""Regression test for injecting JSONs with a SMALL shape count (< ~250).

Bug: locate_livery_group scanned heap for the layer-count as a 2-byte pattern.
Small counts (e.g. 50/100/200) are extremely common byte values, so the old
code did a ReadProcessMemory per match and counted every raw match toward the
200k candidate cap — it ran out of budget (or time) before reaching the real
template, so small-count injection failed. The fix reads the table pointer from
the already-fetched region buffer and only counts plausible candidates.

This builds a fake process region containing one valid LiveryGroup with a small
count and asserts the locator finds it.
"""

import struct

import fd6.inject.fh6_injector as inj


class _FakeRegion:
    def __init__(self, base, size):
        self.base = base
        self.size = size
        self.readable = True
        self.writable = True
        self.is_image = False


class _FakeProc:
    """Backs a single contiguous bytearray at `base`; serves enumerate_regions
    + try_read like ProcessHandle does."""
    def __init__(self, base, buf, blocks=None):
        self._base = base
        self._buf = buf
        # blocks: list of (start_addr, bytes) standalone memory blocks (the layer
        # structs). Reads anywhere inside a block are served from it, so
        # lptr+field_offset works (not just the exact base address).
        self._blocks = blocks or []

    def enumerate_regions(self):
        return [_FakeRegion(self._base, len(self._buf))]

    def try_read(self, addr, size):
        if self._base <= addr and addr + size <= self._base + len(self._buf):
            off = addr - self._base
            return bytes(self._buf[off:off + size])
        for bstart, bbytes in self._blocks:
            if bstart <= addr and addr + size <= bstart + len(bbytes):
                off = addr - bstart
                return bytes(bbytes[off:off + size])
        return None


def _valid_layer_bytes() -> bytes:
    """A layer struct that scores 5/5 in _score_layer."""
    b = bytearray(0x80)
    struct.pack_into('<2f', b, inj.LAYER_POS_OFF, 100.0, 100.0)       # pos in range
    struct.pack_into('<2f', b, inj.LAYER_SCALE_OFF, 32.0, 32.0)       # scale 0<v<=64
    b[inj.LAYER_COLOR_OFF:inj.LAYER_COLOR_OFF + 4] = bytes([10, 20, 30, 255])
    b[inj.LAYER_SHAPE_ID_OFF] = 102                                    # ellipse
    b[inj.LAYER_MASK_OFF] = 0                                          # mask 0/1
    return bytes(b)


def test_locate_small_count_group():
    inj._seed_module_offsets(inj.default_profile()) if hasattr(inj, "_seed_module_offsets") else None
    count = 50  # the kind of small count that used to fail

    region_base = 0x10_000_000
    # Layout inside the region buffer:
    #   group struct (count at +COUNT_OFF, table ptr at +TABLE_OFF)
    #   followed by the layer-pointer table (count * 8 bytes).
    group_off = 0x1000
    table_addr = region_base + 0x4000
    buf = bytearray(0x8000)
    struct.pack_into('<H', buf, group_off + inj.COUNT_OFF, count)
    struct.pack_into('<Q', buf, group_off + inj.TABLE_OFF, table_addr)

    # Each layer pointer → a distinct standalone memory block (so reads at
    # lptr + field_offset resolve correctly).
    layer_bytes = _valid_layer_bytes()
    blocks = []
    for k in range(count):
        lptr = 0x20_000_000 + k * 0x100
        struct.pack_into('<Q', buf, (table_addr - region_base) + k * 8, lptr)
        blocks.append((lptr, layer_bytes))

    proc = _FakeProc(region_base, buf, blocks)
    result = inj.locate_livery_group(proc, count)
    assert result is not None, "small-count group must be located"
    group_addr, found_table = result
    assert found_table == table_addr
    assert group_addr == region_base + group_off
