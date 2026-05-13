# test_escape.py
a = b'\\n'  # single backslash + n in source
print(f'a len={len(a)} repr={a!r} hex={a.hex()}')

b = b'\n'   # newline escape in source
print(f'b len={len(b)} repr={b!r} hex={b.hex()}')

c = b'\\x5c\\x6e'  # explicit hex bytes
print(f'c len={len(c)} repr={c!r} hex={c.hex()}')

# What we need: bytes 0x5C 0x6E (backslash + n)
d = b'\x5c\x6e'  # direct hex
print(f'd len={len(d)} repr={d!r} hex={d.hex()}')
