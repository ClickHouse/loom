class FakeStream:
    def write(self, s):
        return len(s)
