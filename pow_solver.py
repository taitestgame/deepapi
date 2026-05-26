
import struct
import base64
import json

# ===== Keccak constants =====
RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]

MASK64 = 0xFFFFFFFFFFFFFFFF

def rotl64(v, k):
    return ((v << k) | (v >> (64 - k))) & MASK64

def keccak_f23(s):
    """Keccak-f[1600] chạy rounds 1..23 (bỏ round 0)"""
    a = list(s)
    for r in range(1, 24):
        # Theta
        c = [a[x] ^ a[x+5] ^ a[x+10] ^ a[x+15] ^ a[x+20] for x in range(5)]
        d = [c[(x-1)%5] ^ rotl64(c[(x+1)%5], 1) for x in range(5)]
        a = [a[i] ^ d[i % 5] for i in range(25)]

        # Rho + Pi
        OFFSETS = [
            0,  1, 62, 28, 27,
            36, 44,  6, 55, 20,
             3, 10, 43, 25, 39,
            41, 45, 15, 21,  8,
            18,  2, 61, 56, 14,
        ]
        PI_MAP = [
            0, 10, 20,  5, 15,
            16,  1, 11, 21,  6,
             7, 17,  2, 12, 22,
            23,  8, 18,  3, 13,
            14, 24,  9, 19,  4,
        ]
        b = [0] * 25
        for i in range(25):
            b[PI_MAP[i]] = rotl64(a[i], OFFSETS[i])

        # Chi
        for x in range(5):
            for y in range(5):
                i = x + 5*y
                a[i] = b[i] ^ ((~b[(x+1)%5 + 5*y]) & b[(x+2)%5 + 5*y]) & MASK64

        # Iota
        a[0] ^= RC[r]
        a[0] &= MASK64

    return a

def deepseek_hash_v1(data: bytes) -> bytes:
    """
    DeepSeekHashV1: SHA3-256 variant, skip round 0
    Rate = 136 bytes (SHA3-256 rate)
    """
    rate = 136
    s = [0] * 25

    off = 0
    while off + rate <= len(data):
        block = data[off:off+rate]
        for i in range(rate // 8):
            word = struct.unpack_from('<Q', block, i*8)[0]
            s[i] ^= word
        s = keccak_f23(s)
        off += rate

    # Padding
    final = bytearray(rate)
    tail = data[off:]
    final[:len(tail)] = tail
    final[len(tail)] = 0x06
    final[rate-1] |= 0x80

    for i in range(rate // 8):
        word = struct.unpack_from('<Q', bytes(final), i*8)[0]
        s[i] ^= word
    s = keccak_f23(s)

    # Output 32 bytes
    out = bytearray(32)
    struct.pack_into('<Q', out, 0,  s[0])
    struct.pack_into('<Q', out, 8,  s[1])
    struct.pack_into('<Q', out, 16, s[2])
    struct.pack_into('<Q', out, 24, s[3])
    return bytes(out)


def solve_pow(challenge_hex: str, salt: str, expire_at: int, difficulty: int) -> int:
    """
    Tìm nonce n trong [0, difficulty) sao cho:
    DeepSeekHashV1(f"{salt}_{expire_at}_{n}") == bytes.fromhex(challenge_hex)
    """
    if len(challenge_hex) != 64:
        raise ValueError("challenge phải là 64 ký tự hex")

    target = bytes.fromhex(challenge_hex)
    prefix = f"{salt}_{expire_at}_".encode()

    for n in range(difficulty):
        data = prefix + str(n).encode()
        result = deepseek_hash_v1(data)
        if result == target:
            return n

    raise ValueError(f"Không tìm thấy solution trong {difficulty} bước")


def build_pow_header(challenge: dict, answer: int) -> str:
    """
    Tạo header x-ds-pow-response = base64(JSON({...}))
    """
    payload = {
        "algorithm":   challenge["algorithm"],
        "challenge":   challenge["challenge"],
        "salt":        challenge["salt"],
        "answer":      answer,
        "signature":   challenge["signature"],
        "target_path": challenge["target_path"],
    }
    json_bytes = json.dumps(payload, separators=(',', ':')).encode()
    return base64.b64encode(json_bytes).decode()


def solve_challenge(challenge: dict) -> str:
    """
    End-to-end: nhận challenge dict → trả về x-ds-pow-response header value
    """
    algo = challenge.get("algorithm", "")
    if algo != "DeepSeekHashV1":
        raise ValueError(f"Unsupported algorithm: {algo}")

    difficulty = int(challenge.get("difficulty", 144000))
    answer = solve_pow(
        challenge["challenge"],
        challenge["salt"],
        int(challenge["expire_at"]),
        difficulty
    )
    return build_pow_header(challenge, answer)
