class Reldep:
    name: str
    relation: str
    version: str
    def __str__(self) -> str: ...

def chksum_type(name: str) -> int: ...
