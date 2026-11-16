"""Read/rebuild the user's PyInstaller archive without executing launcher code."""
import pathlib
import struct
import zlib

MAGIC = b"MEI\x0c\x0b\x0a\x0b\x0e"


class LauncherArchive:
    def __init__(self, path):
        self.data = pathlib.Path(path).read_bytes()
        self.cookie_position = self.data.rfind(MAGIC)
        if self.cookie_position < 0:
            raise ValueError("PyInstaller cookie missing")
        cookie = struct.unpack_from("!8sIIII64s", self.data, self.cookie_position)
        _, size, toc_offset, toc_size, self.python_version, self.python_library = cookie
        self.start = self.cookie_position + 88 - size
        self.entries = []
        offset = self.start + toc_offset
        while offset < self.start + toc_offset + toc_size:
            length, position, compressed_size, size, compressed, kind = struct.unpack_from("!IIIIBc", self.data, offset)
            name = self.data[offset + 18:offset + length].split(b"\x00", 1)[0].decode("utf-8")
            self.entries.append({"name": name, "position": position, "compressed_size": compressed_size,
                                 "size": size, "compressed": compressed, "kind": kind})
            offset += length

    def raw(self, entry):
        start = self.start + entry["position"]
        return self.data[start:start + entry["compressed_size"]]

    def read(self, name):
        entry = next(e for e in self.entries if e["name"] == name)
        raw = self.raw(entry)
        return zlib.decompress(raw) if entry["compressed"] else raw

    def rebuild(self, target, replacements):
        archive = bytearray()
        toc = bytearray()
        for entry in self.entries:
            if entry["name"] in replacements:
                unpacked = replacements[entry["name"]]
                raw = zlib.compress(unpacked, 9) if entry["compressed"] else unpacked
                size = len(unpacked)
            else:
                raw, size = self.raw(entry), entry["size"]
            encoded = entry["name"].encode("utf-8") + b"\x00"
            length = (18 + len(encoded) + 15) & ~15
            toc.extend(struct.pack("!IIIIBc", length, len(archive), len(raw), size,
                                   entry["compressed"], entry["kind"]))
            toc.extend(encoded.ljust(length - 18, b"\x00"))
            archive.extend(raw)
        toc_offset = len(archive)
        archive.extend(toc)
        archive.extend(struct.pack("!8sIIII64s", MAGIC, len(archive) + 88, toc_offset,
                                   len(toc), self.python_version, self.python_library))
        pathlib.Path(target).write_bytes(self.data[:self.start] + archive)
