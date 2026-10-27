"""A small seeded generator that gives the same numbers on every platform and Python version.

SplitMix64 (Steele, Lea and Flood, 2014): a 64-bit state advanced by a fixed odd constant and
mixed by two multiply-xorshift steps. Python's random module is not used: its shuffle and its
bounded draws have changed between versions, and the frozen order must not.
"""

MASK64 = (1 << 64) - 1


class SplitMix64:
    def __init__(self, seed):
        self.state = seed & MASK64

    def next_u64(self):
        self.state = (self.state + 0x9E3779B97F4A7C15) & MASK64
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
        return z ^ (z >> 31)

    def below(self, n):
        """A whole number uniform in [0, n), by rejection, so no value is favoured."""
        if n <= 0:
            raise ValueError("below() needs n > 0")
        limit = ((1 << 64) // n) * n
        while True:
            x = self.next_u64()
            if x < limit:
                return x % n

    def uniform(self):
        """A double uniform in [0, 1) from the top 53 bits."""
        return (self.next_u64() >> 11) * (1.0 / 9007199254740992.0)


def shuffled(items, seed):
    """A new list: Fisher-Yates from the last position down, drawing from SplitMix64(seed)."""
    out = list(items)
    rng = SplitMix64(seed)
    for i in range(len(out) - 1, 0, -1):
        j = rng.below(i + 1)
        out[i], out[j] = out[j], out[i]
    return out
