def helper():
    return 1


class Store:
    def save(self):
        return 2

    @classmethod
    def make(cls):
        return cls()
