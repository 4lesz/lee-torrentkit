import unittest

from torrentkit import bencode


class BencodeTests(unittest.TestCase):
    def test_roundtrip(self):
        value = {
            b"announce": b"http://tracker/announce",
            b"info": {b"length": 0, b"name": b"x", b"pieces": b"", b"piece length": 16384},
            b"list": [1, -2, b"three", [b"nested"]],
        }
        encoded = bencode.encode(value)
        self.assertEqual(bencode.decode(encoded), value)

    def test_dictionary_keys_are_sorted(self):
        encoded = bencode.encode({b"b": 1, b"a": 2})
        self.assertEqual(encoded, b"d1:ai2e1:bi1ee")

    def test_keys_sort_by_raw_bytes_not_length_prefix(self):
        # "6:length" < "4:name" would be wrong; BEP 3 sorts the raw strings.
        encoded = bencode.encode({b"name": b"x", b"length": 1, b"piece length": 2})
        self.assertEqual(encoded, b"d6:lengthi1e4:name1:x12:piece lengthi2ee")

    def test_raw_value_span(self):
        payload = b"d8:announce3:url4:infod4:name1:xe3:zzzi1ee"
        start, end = bencode.raw_value_span(payload, b"info")
        self.assertEqual(payload[start:end], b"d4:name1:xe")
        self.assertIsNone(bencode.raw_value_span(payload, b"missing"))

    def test_strings_are_length_prefixed(self):
        self.assertEqual(bencode.encode(b"spam"), b"4:spam")
        self.assertEqual(bencode.encode(""), b"0:")

    def test_integer_edges(self):
        self.assertEqual(bencode.encode(0), b"i0e")
        self.assertEqual(bencode.encode(-17), b"i-17e")
        self.assertEqual(bencode.decode(b"i-17e"), -17)

    def test_decode_prefix_returns_end_offset(self):
        payload = b"4:spamRAW_BYTES"
        value, offset = bencode.decode_prefix(payload)
        self.assertEqual(value, b"spam")
        self.assertEqual(payload[offset:], b"RAW_BYTES")

    def test_rejects_trailing_data_in_strict_mode(self):
        self.assertEqual(bencode.decode(b"i1e", strict=False), 1)
        with self.assertRaises(bencode.BencodeError):
            bencode.decode(b"i1eX")

    def test_rejects_malformed(self):
        bad = [
            b"i01e",
            b"i-0e",
            b"ie",
            b"4:spa",
            b"l1:a",
            b"d1:ai1e1:ai2ee",
            b"d1:ai1e",
            b"i1e extra",
            b"",
            b"x",
        ]
        for payload in bad:
            with self.subTest(payload=payload):
                with self.assertRaises(bencode.BencodeError):
                    bencode.decode(payload)

    def test_rejects_booleans(self):
        with self.assertRaises(TypeError):
            bencode.encode(True)


if __name__ == "__main__":
    unittest.main()
